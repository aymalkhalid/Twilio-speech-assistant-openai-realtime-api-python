import html as html_module
import json
import os
import base64
import asyncio
from contextlib import asynccontextmanager
import hmac
import hashlib
import time
import websockets
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, Request, Query, Header, HTTPException, Body, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream

# Import our centralized configuration and organized services
from config import Config
from services import (
    WebSocketConnectionManager,
    TwilioService,
    TwilioAudioProcessor,
    OpenAIService,
    AudioService
)
from services.log_utils import Log
from services.call_records_service import (
    list_call_records_sync,
    has_call_record_backend_configured,
    get_call_record_by_id_sync,
    update_call_record_by_id_sync,
    delete_call_record_by_id_sync,
    update_call_record_by_call_sid_sync,
    CallRecordUpdateSchemaError,
)
from services.transcription_service import transcribe_recording, enhance_transcript, enhance_transcript_with_summary

@asynccontextmanager
async def lifespan(_: FastAPI):
    from services.call_record_events import set_main_loop

    set_main_loop(asyncio.get_running_loop())
    yield


app = FastAPI(lifespan=lifespan)


# Dashboard session cookie (login page auth when DASHBOARD_USERS is set)
DASHBOARD_SESSION_COOKIE = "dashboard_session"
DASHBOARD_SESSION_MAX_AGE_SEC = 24 * 3600  # 24 hours


def _dashboard_session_create(signing_key: str, username: str) -> str:
    """Create a signed session value: expiry.username.sig (always includes username)."""
    expiry = str(int(time.time()) + DASHBOARD_SESSION_MAX_AGE_SEC)
    msg = expiry + "." + username
    sig = hmac.new(signing_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{expiry}.{username}.{sig}".encode("utf-8")).decode("ascii")


def _dashboard_session_verify(signing_key: str, value: str) -> tuple[bool, str | None]:
    """Verify signed session. Returns (valid, username or None)."""
    if not value or not signing_key:
        return False, None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        parts = raw.split(".")
        if len(parts) == 2:
            expiry_str, sig = parts
            username = None
            msg = expiry_str
        elif len(parts) == 3:
            expiry_str, username, sig = parts
            msg = expiry_str + "." + username
        else:
            return False, None
        expiry = int(expiry_str)
        if expiry < time.time():
            return False, None
        expected = hmac.new(signing_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False, None
        return True, username if username else None
    except Exception:
        return False, None


def _check_dashboard_credentials(username: str | None, password: str, auth: dict) -> bool:
    """Return True if username+password matches a user in DASHBOARD_USERS."""
    users = auth.get("users") or []
    if not users or not (username or "").strip():
        return False
    for u, p in users:
        if u == (username or "").strip() and p == (password or ""):
            return True
    return False


class RedirectToLoginException(Exception):
    """Raised by _require_dashboard_key when browser should be sent to /login."""
    def __init__(self, next_path: str):
        self.next_path = next_path


def _require_dashboard_key(
    request: Request,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
) -> None:
    """Require dashboard auth when DASHBOARD_USERS is set. Accepts session cookie or key/header (any user's password). Redirects browser to /login if missing."""
    auth = Config.get_dashboard_auth()
    signing_key = auth.get("signing_key")
    valid_keys = auth.get("valid_keys") or set()
    if not signing_key and not valid_keys:
        return
    # 1) Check session cookie (after login)
    cookie_val = request.cookies.get(DASHBOARD_SESSION_COOKIE)
    if cookie_val and signing_key:
        valid, cookie_username = _dashboard_session_verify(signing_key, cookie_val)
        if valid:
            if not hasattr(request.state, "dashboard_user"):
                request.state.dashboard_user = cookie_username
            return
    # 2) Check query key or header (API / deep link)
    provided = (key and key.strip()) or (x_dashboard_key and x_dashboard_key.strip())
    if provided and provided in valid_keys:
        return
    # 3) Unauthorized: redirect browser to login, 401 for API
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept and request.url.path != "/login":
        next_path = request.url.path
        if request.url.query:
            next_path = next_path + "?" + request.url.query
        raise RedirectToLoginException(next_path=next_path)
    raise HTTPException(status_code=401, detail="Dashboard key or login required (query key=, header X-Dashboard-Key, or log in at /login)")


@app.exception_handler(RedirectToLoginException)
async def _redirect_to_login_handler(request: Request, exc: RedirectToLoginException):
    return RedirectResponse(url=f"/login?next={exc.next_path}", status_code=302)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve a minimal favicon to avoid 404s when browsers request it."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" fill="#6366f1" rx="6"/>'
        '<path fill="none" stroke="white" stroke-width="2" stroke-linecap="round" '
        'd="M10 16h4l2 6 2-6h4M12 11v10"/>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/")
async def index_page(request: Request):
    """Redirect root to the dashboard, preserving query string (e.g. ?key=...)."""
    path = "/dashboard"
    if request.url.query:
        path = path + "?" + request.url.query
    return RedirectResponse(url=path, status_code=302)


def _login_html(next_val: str, error_msg: str = "") -> str:
    path = Path(__file__).resolve().parent / "static" / "login.html"
    html = path.read_text(encoding="utf-8")
    next_esc = next_val.replace("&", "&amp;").replace('"', "&quot;")
    html = html.replace("__NEXT_PLACEHOLDER__", next_esc)
    html = html.replace("__ERROR_PLACEHOLDER__", error_msg.replace("<", "&lt;").replace(">", "&gt;"))
    html = html.replace("__ERROR_DISPLAY__", "block" if error_msg else "none")
    return html


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next_path: str | None = Query(None, alias="next")):
    """Serve login page when DASHBOARD_USERS is set. No auth required."""
    next_val = (next_path or "/dashboard").strip()
    return HTMLResponse(_login_html(next_val, ""))


@app.post("/login", response_class=RedirectResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(...),
    next_path: str = Form("/dashboard"),
):
    """Check username+password against DASHBOARD_USERS; set session cookie and redirect."""
    auth = Config.get_dashboard_auth()
    signing_key = auth.get("signing_key")
    if not signing_key:
        raise HTTPException(status_code=400, detail="Login not configured (set DASHBOARD_USERS in .env)")
    next_val = (next_path or "/dashboard").strip()
    session_username = (username or "").strip()
    if not _check_dashboard_credentials(session_username or None, password or "", auth):
        return HTMLResponse(_login_html(next_val, "Invalid username or password."), status_code=401)
    value = _dashboard_session_create(signing_key, session_username)
    redirect_to = next_val if next_val.startswith("/") else "/dashboard"
    response = RedirectResponse(url=redirect_to, status_code=302)
    response.set_cookie(
        key=DASHBOARD_SESSION_COOKIE,
        value=value,
        max_age=DASHBOARD_SESSION_MAX_AGE_SEC,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request):
    """Clear dashboard session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=DASHBOARD_SESSION_COOKIE, path="/")
    return response



@app.get("/calls", response_class=JSONResponse)
async def get_call_records(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    priority: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    has_booking: bool | None = Query(None),
    has_address: bool | None = Query(None),
    is_spam: bool | None = Query(None),
    status: str | None = Query(None),
    address: str | None = Query(None),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """List call records from Supabase when CALL_RECORD_BACKEND=supabase."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    if not has_call_record_backend_configured():
        return JSONResponse(
            status_code=503,
            content={"detail": "Call-record backend not configured; set CALL_RECORD_BACKEND=supabase and SUPABASE_*."},
        )
    backend = (Config.CALL_RECORD_BACKEND or "webhook").strip().lower()
    if backend != "supabase":
        return JSONResponse(
            status_code=503,
            content={"detail": "Call records API is available when CALL_RECORD_BACKEND=supabase."},
        )
    records, total = list_call_records_sync(
        limit=limit,
        offset=offset,
        priority=priority,
        date_from=date_from,
        date_to=date_to,
        has_booking=has_booking,
        has_address=has_address,
        is_spam=is_spam,
        status=status,
        address=address,
    )
    return {"call_records": records, "count": len(records), "total": total}


@app.get("/calls/events")
async def call_records_event_stream(
    request: Request,
    key: str | None = Query(None, alias="key"),
):
    """
    Server-Sent Events for call-record changes on this server instance.
    Auth: session cookie or ?key= because browser EventSource does not support custom headers.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=None)
    if not has_call_record_backend_configured() or (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() != "supabase":
        raise HTTPException(status_code=503, detail="Call-record events require CALL_RECORD_BACKEND=supabase.")

    from services.call_record_events import register_subscriber, unregister_subscriber

    async def gen():
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        register_subscriber(queue)
        try:
            yield f"data: {json.dumps({'type': 'ready'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps({'type': 'call_records_changed'})}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            unregister_subscriber(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


CALL_RECORD_STATUS_VALUES = frozenset({"pending", "in_progress", "booked", "cancelled", "completed", "finalized", "spam"})


def _dashboard_call_record_updates(body: dict) -> dict:
    updates = {}
    if "notes" in body:
        notes = body["notes"]
        if not isinstance(notes, list):
            raise HTTPException(status_code=400, detail="notes must be an array of strings")
        if not all(isinstance(item, str) for item in notes):
            raise HTTPException(status_code=400, detail="notes must be an array of strings")
        updates["notes"] = notes

    status_value = body.get("status", body.get("lead_status", None))
    if status_value is not None:
        status = (status_value or "").strip().lower()
        if status and status not in CALL_RECORD_STATUS_VALUES:
            raise HTTPException(
                status_code=400,
                detail="status must be one of: " + ", ".join(sorted(CALL_RECORD_STATUS_VALUES)),
            )
        updates["lead_status"] = status if status else "pending"

    contact_name = body.get("contact_name", body.get("lead_name", None))
    if contact_name is not None:
        updates["lead_name"] = (contact_name or "").strip() or None

    location = body.get("location", body.get("service_address", None))
    if location is not None:
        updates["service_address"] = (location or "").strip() or None

    if "transcript" in body:
        updates["transcript"] = (body["transcript"] or "").strip() or None

    if not updates:
        raise HTTPException(status_code=400, detail="Provide at least one of: notes, status, contact_name, location, transcript")
    return updates


@app.patch("/calls/{record_id}", response_class=JSONResponse)
async def patch_call_record(
    request: Request,
    record_id: str,
    body: dict = Body(...),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Update dashboard-editable call-record fields."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    if not has_call_record_backend_configured() or (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() != "supabase":
        raise HTTPException(status_code=503, detail="Call-record updates require CALL_RECORD_BACKEND=supabase.")
    updates = _dashboard_call_record_updates(body)
    try:
        ok = update_call_record_by_id_sync(record_id, updates)
    except CallRecordUpdateSchemaError as e:
        raise HTTPException(status_code=503, detail=e.message)
    if not ok:
        raise HTTPException(status_code=404, detail="Call record not found or update failed")
    from services.call_record_events import notify_call_records_changed_async

    await notify_call_records_changed_async()
    return {"ok": True, "updated": updates}


@app.delete("/calls/{record_id}", response_class=JSONResponse)
async def delete_call_record(
    request: Request,
    record_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Permanently delete a call record by id."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    if not has_call_record_backend_configured() or (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() != "supabase":
        raise HTTPException(status_code=503, detail="Call-record delete requires CALL_RECORD_BACKEND=supabase.")
    ok = delete_call_record_by_id_sync(record_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Call record not found or delete failed")
    from services.call_record_events import notify_call_records_changed_async

    await notify_call_records_changed_async()
    return {"ok": True, "deleted": record_id}


@app.post("/calls/{record_id}/enhance-transcript", response_class=JSONResponse)
async def enhance_call_record_transcript(
    request: Request,
    record_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Run OpenAI enhancement on the call record's current transcript and save it.
    Manual step when TRANSCRIPT_ENHANCEMENT_MODE=manual.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    if not Config.is_transcript_enhancement_enabled():
        raise HTTPException(status_code=503, detail="Transcript enhancement disabled or not configured.")
    if not has_call_record_backend_configured() or (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() != "supabase":
        raise HTTPException(status_code=503, detail="Enhance transcript requires CALL_RECORD_BACKEND=supabase.")
    record = get_call_record_by_id_sync(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Call record not found")
    transcript = (record.get("transcript") or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Call record has no transcript to enhance. Generate a transcript first.")
    result = await asyncio.to_thread(enhance_transcript_with_summary, transcript)
    if not result:
        raise HTTPException(status_code=502, detail="Enhancement failed")
    enhanced_transcript = result.get("transcript") or transcript
    summary = (result.get("summary") or "").strip()
    issues = (result.get("issues") or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = {
        "transcript": enhanced_transcript,
        "transcript_enhanced_at": now_iso,
        "transcript_summary": summary or None,
        "transcript_issues": issues or None,
    }
    from services.call_record_events import notify_call_records_changed_async

    try:
        ok = update_call_record_by_id_sync(record_id, updates)
    except CallRecordUpdateSchemaError:
        ok = update_call_record_by_id_sync(record_id, {"transcript": enhanced_transcript, "transcript_enhanced_at": now_iso})
        if not ok:
            raise HTTPException(status_code=500, detail="Update failed")
        await notify_call_records_changed_async()
        return JSONResponse(
            status_code=200,
            content={
                "transcript": enhanced_transcript,
                "transcript_enhanced_at": now_iso,
                "transcript_summary": summary or None,
                "transcript_issues": issues or None,
                "message": "Transcript saved. Add transcript_summary and transcript_issues columns for full output by running the Supabase schema migration.",
            },
        )
    if not ok:
        raise HTTPException(status_code=500, detail="Update failed")
    await notify_call_records_changed_async()
    return {
        "transcript": enhanced_transcript,
        "transcript_enhanced_at": now_iso,
        "transcript_summary": summary or None,
        "transcript_issues": issues or None,
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    key: str | None = Query(None, alias="key"),
):
    """Serve the call-record dashboard UI. When DASHBOARD_USERS is set, auth required (cookie or ?key=<password>)."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=None)
    dashboard_path = Path(__file__).resolve().parent / "static" / "dashboard.html"
    if not dashboard_path.is_file():
        raise HTTPException(status_code=404, detail="Dashboard template not found")
    html = dashboard_path.read_text(encoding="utf-8")
    # Inject key into page so dashboard fetches can send it when opened with ?key=... (cookie auth sends no key)
    auth = Config.get_dashboard_auth()
    inject = (key or "") if auth.get("signing_key") else ""
    html = html.replace("__DASHBOARD_KEY_PLACEHOLDER__", inject)
    # Inject business timezone (from .env TIMEZONE) for date/time display
    tz = getattr(Config, "TIMEZONE", "America/Los_Angeles") or "America/Los_Angeles"
    html = html.replace("__DASHBOARD_TIMEZONE_PLACEHOLDER__", tz.replace("\\", "\\\\").replace("'", "\\'"))
    company = getattr(Config, "COMPANY_NAME", None) or ""
    html = html.replace("__DASHBOARD_COMPANY_PLACEHOLDER__", html_module.escape(company))
    return HTMLResponse(html)




@app.get("/settings", response_class=JSONResponse)
async def get_settings(
    request: Request,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Return current effective app settings. Re-loads from Supabase so modal always shows latest. Requires dashboard key."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    try:
        from services.dynamic_settings import (
            load_overrides_sync,
            apply_overrides_to_config,
            get_effective_settings,
        )
        overrides = load_overrides_sync()
        apply_overrides_to_config(overrides)
        return get_effective_settings()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/settings", response_class=JSONResponse)
async def patch_settings(
    request: Request,
    body: dict = Body(...),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Update app settings (overrides stored in Supabase). Requires dashboard key. Only keys in OVERRIDABLE_KEYS are accepted."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    try:
        from services.dynamic_settings import (
            OVERRIDABLE_KEYS,
            save_overrides_sync,
            load_overrides_sync,
            apply_overrides_to_config,
        )
        allowed = {k: v for k, v in body.items() if k in OVERRIDABLE_KEYS}
        if not allowed:
            raise HTTPException(status_code=400, detail="No valid setting keys to update")
        if not save_overrides_sync(allowed):
            raise HTTPException(status_code=503, detail="Settings store unavailable (Supabase required)")
        apply_overrides_to_config(load_overrides_sync())
        return {"ok": True, "updated": list(allowed.keys())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.api_route("/twiml/transfer-to-agent", methods=["GET", "POST"], include_in_schema=False)
async def twiml_transfer_to_agent():
    """
    Return TwiML to connect the caller to an agent. Used when HUMAN_TRANSFER_URL points here
    (e.g. https://your-host/twiml/transfer-to-agent). Dial number from HUMAN_TRANSFER_DIAL_NUMBER.
    """
    number = (Config.HUMAN_TRANSFER_DIAL_NUMBER or "+15551234567").strip()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Say>Connecting you to an agent.</Say>\n"
        "  <Dial timeout=\"30\">\n"
        f"    <Number>{number}</Number>\n"
        "  </Dial>\n"
        "  <Say>No one is available. Goodbye.</Say>\n"
        "  <Hangup/>\n"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    return await TwilioService.create_incoming_call_response(request)


# ---------------------------------------------------------------------------
# Outbound calling: TwiML for answered outbound calls + status callback
# ---------------------------------------------------------------------------

@app.api_route("/outbound-call-twiml/{campaign_id}", methods=["GET", "POST"])
async def outbound_call_twiml(request: Request, campaign_id: str, contact_id: str = Query("")):
    """
    Return TwiML for an answered outbound call. Twilio fetches this URL when
    the callee picks up. Connects to the same /media-stream WebSocket with
    outbound context params so the handler builds a campaign-specific prompt.
    """
    host = request.url.hostname
    stream_url = (
        f"wss://{host}/media-stream"
        f"?direction=outbound"
        f"&campaign_id={quote(campaign_id, safe='')}"
        f"&contact_id={quote(contact_id, safe='')}"
    )
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.api_route("/outbound-call-status", methods=["POST"])
async def outbound_call_status(request: Request):
    """
    Twilio status callback for outbound calls. Updates the contact row in
    Supabase when the call completes (answered, no-answer, busy, failed).
    """
    try:
        body = await request.body()
        params = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
        call_sid = (params.get("CallSid") or params.get("callsid") or [""])[0]
        call_status = (params.get("CallStatus") or params.get("callstatus") or [""])[0]
        if not call_sid:
            return JSONResponse({"ok": True})

        from services.outbound_service import update_contact_status_sync
        terminal_statuses = {"completed", "busy", "no-answer", "failed", "canceled"}
        if call_status.lower() in terminal_statuses:
            is_completed = call_status.lower() == "completed"
            final_status = "completed" if is_completed else "failed"
            error_msg = "" if is_completed else call_status
            await asyncio.to_thread(
                update_contact_status_sync,
                contact_id="",
                status=final_status,
                call_sid=call_sid,
                error=error_msg,
            )
            # Missed-call-callback provenance: if this outbound call was born from a
            # missed call, mark the original as handled (on completed) and append a
            # note to the new lead row linking back to the missed CallSid.
            from services.missed_calls_service import finalize_callback_if_missed_sync
            await asyncio.to_thread(finalize_callback_if_missed_sync, call_sid, is_completed)
    except Exception as e:
        Log.error(f"Outbound call status callback error: {e}")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Outbound campaigns: CRUD + execution endpoints (dashboard-facing)
# ---------------------------------------------------------------------------

def _require_outbound_enabled():
    """Guard: raise 403 if outbound calling is not enabled."""
    if not Config.is_outbound_enabled():
        raise HTTPException(status_code=403, detail="Outbound calling not enabled (set OUTBOUND_ENABLED=true with Twilio + Supabase)")


@app.get("/outbound/campaign-types", response_class=JSONResponse)
async def get_outbound_campaign_types(
    request: Request,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Return built-in campaign type definitions for the dashboard dropdown and template prefill.
    Does NOT require outbound enabled; presets are available before full outbound config."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    from services.outbound_service import get_campaign_types
    return {"campaign_types": get_campaign_types()}


@app.get("/outbound/campaigns", response_class=JSONResponse)
async def list_outbound_campaigns(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """List outbound campaigns. Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import list_campaigns_sync
    campaigns, total = await asyncio.to_thread(list_campaigns_sync, limit, offset, status)
    return {"campaigns": campaigns, "count": len(campaigns), "total": total}


@app.post("/outbound/campaigns", response_class=JSONResponse)
async def create_outbound_campaign(
    request: Request,
    body: dict = Body(...),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Create a new outbound campaign (draft). Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Campaign name is required")
    campaign_type = (body.get("campaign_type") or "general").strip()
    message_template = (body.get("message_template") or "").strip()
    concurrency = max(1, min(int(body.get("concurrency") or 1), Config.OUTBOUND_MAX_CONCURRENCY))
    from services.outbound_service import create_campaign_sync
    campaign = await asyncio.to_thread(create_campaign_sync, name, campaign_type, message_template, concurrency)
    if not campaign:
        raise HTTPException(status_code=500, detail="Failed to create campaign")
    return {"campaign": campaign}


@app.get("/outbound/campaigns/{campaign_id}", response_class=JSONResponse)
async def get_outbound_campaign(
    request: Request,
    campaign_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Get a campaign with its contacts. Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_campaign_sync
    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"campaign": campaign}


@app.patch("/outbound/campaigns/{campaign_id}", response_class=JSONResponse)
async def update_outbound_campaign(
    request: Request,
    campaign_id: str,
    body: dict = Body(...),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Update a draft campaign. Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import update_campaign_sync
    ok = await asyncio.to_thread(update_campaign_sync, campaign_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found or no valid fields to update")
    return {"ok": True}


@app.delete("/outbound/campaigns/{campaign_id}", response_class=JSONResponse)
async def delete_outbound_campaign(
    request: Request,
    campaign_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Delete a campaign and all its contacts (cascade). Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import delete_campaign_sync
    ok = await asyncio.to_thread(delete_campaign_sync, campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found or delete failed")
    return {"ok": True}


@app.post("/outbound/campaigns/{campaign_id}/contacts", response_class=JSONResponse)
async def add_outbound_contacts(
    request: Request,
    campaign_id: str,
    body: dict = Body(...),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Add contacts to a campaign. Body: { "contacts": [ { "name": "...", "phone": "+1...", "email": "...", "custom_fields": {...} }, ... ] }
    Requires dashboard auth.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    contacts = body.get("contacts")
    if not contacts or not isinstance(contacts, list):
        raise HTTPException(status_code=400, detail="contacts must be a non-empty array")
    for i, c in enumerate(contacts):
        if not (c.get("phone") or "").strip():
            raise HTTPException(status_code=400, detail=f"Contact at index {i} is missing a phone number")
    from services.outbound_service import add_contacts_sync
    inserted = await asyncio.to_thread(add_contacts_sync, campaign_id, contacts)
    return {"contacts": inserted, "count": len(inserted)}


@app.delete("/outbound/campaigns/{campaign_id}/contacts/{contact_id}", response_class=JSONResponse)
async def delete_outbound_contact(
    request: Request,
    campaign_id: str,
    contact_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Remove a single contact from a campaign. Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import delete_contact_sync
    ok = await asyncio.to_thread(delete_contact_sync, contact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Contact not found or delete failed")
    return {"ok": True}


@app.get("/outbound/campaigns/{campaign_id}/status", response_class=JSONResponse)
async def get_outbound_campaign_status(
    request: Request,
    campaign_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Return campaign progress: contact counts grouped by status. Used for dashboard polling."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_campaign_progress_sync, get_campaign_sync
    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    progress = await asyncio.to_thread(get_campaign_progress_sync, campaign_id)
    return {
        "campaign_id": campaign_id,
        "campaign_status": campaign.get("status", "draft"),
        "progress": progress,
    }


@app.post("/outbound/campaigns/{campaign_id}/start", response_class=JSONResponse)
async def start_outbound_campaign(
    request: Request,
    campaign_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Start dialing contacts in a campaign. Launches background task. Requires dashboard auth."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_campaign_sync, update_campaign_sync, run_campaign, reset_failed_to_pending_sync
    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") == "running":
        raise HTTPException(status_code=409, detail="Campaign is already running")
    if campaign.get("status") == "completed":
        await asyncio.to_thread(reset_failed_to_pending_sync, campaign_id)
        campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    contacts = campaign.get("contacts") or []
    pending = [c for c in contacts if c.get("status") == "pending"]
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="No pending contacts to dial (retry resets failed contacts to pending)",
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    base_url = Config.OUTBOUND_BASE_URL or str(request.base_url).rstrip("/")
    if not base_url or any(x in base_url.lower() for x in ("localhost", "127.0.0.1")):
        raise HTTPException(
            status_code=400,
            detail="Outbound TwiML URL is not reachable by Twilio (localhost/127.0.0.1). Set OUTBOUND_BASE_URL in .env to your public URL (e.g. https://your-ngrok-subdomain.ngrok.io).",
        )
    await asyncio.to_thread(update_campaign_sync, campaign_id, {"status": "running", "started_at": now_iso})
    asyncio.create_task(run_campaign(campaign_id, base_url))
    return {"ok": True, "message": f"Campaign started with {len(pending)} pending contacts"}


@app.post("/outbound/campaigns/{campaign_id}/contacts/{contact_id}/call", response_class=JSONResponse)
async def call_single_outbound_contact(
    request: Request,
    campaign_id: str,
    contact_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Dial a single contact manually. Does not change campaign status — just initiates one call."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_campaign_sync, get_contact_sync, update_contact_status_sync
    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    contact = await asyncio.to_thread(get_contact_sync, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    phone = (contact.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Contact has no phone number")
    if contact.get("status") == "calling":
        raise HTTPException(status_code=409, detail="Contact is already being called")
    base_url = Config.OUTBOUND_BASE_URL or str(request.base_url).rstrip("/")
    # Twilio must be able to reach the TwiML URL when the callee answers (localhost is not reachable)
    if not base_url or any(x in base_url.lower() for x in ("localhost", "127.0.0.1")):
        raise HTTPException(
            status_code=400,
            detail="Outbound TwiML URL is not reachable by Twilio (localhost/127.0.0.1). Set OUTBOUND_BASE_URL in .env to your public URL (e.g. https://your-ngrok-subdomain.ngrok.io).",
        )
    twiml_url = f"{base_url}/outbound-call-twiml/{campaign_id}?contact_id={quote(contact_id, safe='')}"
    status_callback = f"{base_url}/outbound-call-status"
    await asyncio.to_thread(update_contact_status_sync, contact_id, "calling")
    try:
        call = await TwilioService.create_outbound_call(
            to=phone,
            twiml_url=twiml_url,
            status_callback=status_callback,
        )
        TwilioService.register_outbound_context(call.sid, campaign_id, contact_id)
        await asyncio.to_thread(update_contact_status_sync, contact_id, "calling", call_sid=call.sid)
        return {"ok": True, "call_sid": call.sid, "message": f"Calling {phone}"}
    except Exception as e:
        await asyncio.to_thread(update_contact_status_sync, contact_id, "failed", error=str(e)[:500])
        raise HTTPException(status_code=502, detail=f"Dial failed: {e}")


@app.post("/outbound/campaigns/{campaign_id}/contacts/{contact_id}/reset", response_class=JSONResponse)
async def reset_outbound_contact(
    request: Request,
    campaign_id: str,
    contact_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Reset a contact to pending so they can be called again. Use when stuck in 'calling' (e.g. after a failed attempt)."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_contact_sync, reset_contact_to_pending_sync
    contact = await asyncio.to_thread(get_contact_sync, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if contact.get("campaign_id") != campaign_id:
        raise HTTPException(status_code=404, detail="Contact not in this campaign")
    ok = await asyncio.to_thread(reset_contact_to_pending_sync, contact_id)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Contact can only be reset when status is 'calling' or 'failed'",
        )
    return {"ok": True, "message": "Contact reset to pending; you can call again."}


@app.post("/outbound/campaigns/{campaign_id}/contacts/{contact_id}/stop", response_class=JSONResponse)
async def stop_outbound_contact(
    request: Request,
    campaign_id: str,
    contact_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """End the active call for this contact (hang up). Contact must be in 'calling' state with a call_sid."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_contact_sync
    contact = await asyncio.to_thread(get_contact_sync, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if contact.get("campaign_id") != campaign_id:
        raise HTTPException(status_code=404, detail="Contact not in this campaign")
    if contact.get("status") != "calling":
        raise HTTPException(status_code=400, detail="Contact is not on an active call (status is not 'calling')")
    call_sid = (contact.get("call_sid") or "").strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="No active call SID for this contact")
    await TwilioService.end_call_async(call_sid)
    return {"ok": True, "message": "Call ended. Contact status will update when Twilio sends the callback."}


@app.post("/outbound/campaigns/{campaign_id}/pause", response_class=JSONResponse)
async def pause_outbound_campaign(
    request: Request,
    campaign_id: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """Pause a running campaign. Contacts already being called will complete; remaining pending contacts are not dialed."""
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_outbound_enabled()
    from services.outbound_service import get_campaign_sync, update_campaign_sync
    campaign = await asyncio.to_thread(get_campaign_sync, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") != "running":
        raise HTTPException(status_code=409, detail="Campaign is not currently running")
    await asyncio.to_thread(update_campaign_sync, campaign_id, {"status": "paused"})
    return {"ok": True, "message": "Campaign paused"}


# ---------------------------------------------------------------------------
# Missed calls: list recent missed inbound calls, callback with AI, mark handled
# ---------------------------------------------------------------------------

def _require_missed_calls_enabled():
    """Guard: raise 503 if Twilio credentials are not configured (needed to fetch call history)."""
    if not Config.has_twilio_credentials():
        raise HTTPException(status_code=503, detail="Missed calls unavailable (Twilio credentials not configured)")


@app.get("/missed-calls", response_class=JSONResponse)
async def list_missed_calls(
    request: Request,
    hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(50, ge=1, le=200),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Return recent inbound calls that either got a missed Twilio status
    (no-answer / busy / failed / canceled) or were completed but produced
    no Supabase lead row. Already-handled calls are filtered out. Requires dashboard auth.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_missed_calls_enabled()
    from services.missed_calls_service import list_missed_calls_sync
    items = await asyncio.to_thread(list_missed_calls_sync, hours, limit)
    return {
        "missed_calls": items,
        "count": len(items),
        "twilio_number": Config.TWILIO_OUTBOUND_NUMBER or "",
    }


@app.post("/missed-calls/{call_sid}/callback-ai", response_class=JSONResponse)
async def missed_call_callback_ai(
    request: Request,
    call_sid: str,
    body: dict = Body(default={}),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Dial the missed caller back with the AI. Reuses the existing outbound
    pipeline: ensures a singleton callback campaign, inserts a contact row
    for this phone, then fires Twilio via TwilioService.create_outbound_call.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_missed_calls_enabled()
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        raise HTTPException(status_code=503, detail="Callback requires Supabase (reuses outbound campaign pipeline)")

    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required (the missed caller's number)")

    base_url = Config.OUTBOUND_BASE_URL or str(request.base_url).rstrip("/")
    if not base_url or any(x in base_url.lower() for x in ("localhost", "127.0.0.1")):
        raise HTTPException(
            status_code=400,
            detail="Outbound TwiML URL is not reachable by Twilio (localhost/127.0.0.1). Set OUTBOUND_BASE_URL in .env to your public URL.",
        )

    from services.missed_calls_service import (
        get_or_create_callback_campaign_sync,
        add_callback_contact_sync,
    )
    from services.outbound_service import update_contact_status_sync

    campaign = await asyncio.to_thread(get_or_create_callback_campaign_sync)
    if not campaign or not campaign.get("id"):
        raise HTTPException(status_code=500, detail="Failed to prepare missed-call callback campaign")
    campaign_id = campaign["id"]

    contact = await asyncio.to_thread(add_callback_contact_sync, campaign_id, call_sid, phone)
    if not contact or not contact.get("id"):
        raise HTTPException(status_code=500, detail="Failed to create callback contact")
    contact_id = contact["id"]

    twiml_url = f"{base_url}/outbound-call-twiml/{campaign_id}?contact_id={quote(contact_id, safe='')}"
    status_callback = f"{base_url}/outbound-call-status"
    await asyncio.to_thread(update_contact_status_sync, contact_id, "calling")
    try:
        call = await TwilioService.create_outbound_call(
            to=phone,
            twiml_url=twiml_url,
            status_callback=status_callback,
        )
        TwilioService.register_outbound_context(call.sid, campaign_id, contact_id)
        await asyncio.to_thread(update_contact_status_sync, contact_id, "calling", call_sid=call.sid)
        return {
            "ok": True,
            "call_sid": call.sid,
            "campaign_id": campaign_id,
            "contact_id": contact_id,
            "message": f"Calling {phone} with AI",
        }
    except Exception as e:
        await asyncio.to_thread(update_contact_status_sync, contact_id, "failed", error=str(e)[:500])
        raise HTTPException(status_code=502, detail=f"Dial failed: {e}")


@app.post("/missed-calls/{call_sid}/handled", response_class=JSONResponse)
async def missed_call_mark_handled(
    request: Request,
    call_sid: str,
    body: dict = Body(default={}),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Mark a missed call as handled. Upserts a Supabase call-record row for this call_sid
    with status='missed_handled' so it is hidden from the list next time.
    Only effective when CALL_RECORD_BACKEND=supabase.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    _require_missed_calls_enabled()
    phone = (body.get("phone") or "").strip() or None
    from services.missed_calls_service import mark_handled_sync
    ok = await asyncio.to_thread(mark_handled_sync, call_sid, phone)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Unable to persist handled state (CALL_RECORD_BACKEND must be supabase)",
        )
    return {"ok": True, "message": "Marked handled"}


def _validate_twilio_signature(request: Request, body: bytes, signature: str) -> bool:
    """Return True if X-Twilio-Signature is valid for the given URL and body."""
    if not signature or not Config.TWILIO_AUTH_TOKEN:
        return False
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(Config.TWILIO_AUTH_TOKEN)
        url = Config.get_recording_status_callback_url() or str(request.url)
        raw = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
        params = {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}
        return validator.validate(url, params, signature)
    except Exception:
        return False


@app.api_route("/recording-status", methods=["POST"])
async def handle_recording_status(request: Request):
    """
    Twilio RecordingStatusCallback webhook. When a recording is completed,
    updates the call record for that call_sid with recording_link.
    """
    body = await request.body()
    signature = (request.headers.get("X-Twilio-Signature") or "").strip()
    if not _validate_twilio_signature(request, body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")
    params = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
    def _first(key: str) -> str:
        v = params.get(key)
        if v is None:
            return ""
        return (v[0] if isinstance(v, list) and v else v).strip() if v else ""

    call_sid = _first("CallSid")
    recording_url = _first("RecordingUrl")
    recording_status = _first("RecordingStatus")

    if recording_status == "completed" and call_sid and recording_url:
        ok = update_call_record_by_call_sid_sync(call_sid, {"recording_link": recording_url})
        Log.event("Recording completed, call record updated", {
            "call_sid": call_sid,
            "recording_link": recording_url,
            "updated": ok,
        })
        if ok and (Config.CALL_RECORD_BACKEND or "webhook").strip().lower() == "supabase":
            from services.call_record_events import notify_call_records_changed_threadsafe

            notify_call_records_changed_threadsafe()
    return JSONResponse(content={}, status_code=200)


def _extract_recording_sid_from_twilio_url(recording_link: str) -> str | None:
    """Extract Recording SID (RE...) from a Twilio Recordings URL, or None if not found."""
    if not recording_link or not isinstance(recording_link, str):
        return None
    s = recording_link.strip()
    if "/Recordings/" not in s:
        return None
    part = s.split("/Recordings/")[-1].split("/")[0].split(".")[0].strip()
    if len(part) == 34 and part.startswith("RE") and all(c in "0123456789abcdefABCDEF" for c in part[2:]):
        return part
    return None


@app.get("/recordings/{recording_sid}/media")
async def get_recording_media(
    request: Request,
    recording_sid: str,
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
    download: bool = Query(False),
):
    """
    Stream Twilio recording media so the browser can play/download without Twilio sign-in.
    Uses server-side Basic Auth. Requires dashboard key when DASHBOARD_USERS is set.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    recording_sid = (recording_sid or "").strip()
    if not recording_sid or not recording_sid.startswith("RE") or len(recording_sid) != 34:
        raise HTTPException(status_code=400, detail="Invalid recording SID")
    if not Config.has_twilio_credentials():
        raise HTTPException(status_code=503, detail="Twilio not configured")
    media_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{Config.TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.mp3"
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                media_url,
                auth=(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN or ""),
                timeout=60.0,
            )
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Twilio returned {response.status_code}",
            )
        content = response.content
        headers = {
            "Content-Type": "audio/mpeg",
            "Content-Length": str(len(content)),
        }
        if download:
            headers["Content-Disposition"] = f'attachment; filename="recording-{recording_sid}.mp3"'
        else:
            headers["Content-Disposition"] = "inline"
        return Response(content=content, media_type="audio/mpeg", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="Failed to fetch recording") from e


@app.post("/recordings/{recording_sid}/transcribe", response_class=JSONResponse)
async def transcribe_recording_endpoint(
    request: Request,
    recording_sid: str,
    record_id: str | None = Query(None),
    key: str | None = Query(None, alias="key"),
    x_dashboard_key: str | None = Header(None, alias="X-Dashboard-Key"),
):
    """
    Transcribe a Twilio recording using Whisper (faster-whisper).
    Returns { "transcript": "..." }. If record_id is provided, also updates the call record transcript.
    Requires dashboard key when DASHBOARD_USERS is set. Requires TRANSCRIPTION_MODEL and Twilio credentials.
    """
    _require_dashboard_key(request=request, key=key, x_dashboard_key=x_dashboard_key)
    recording_sid = (recording_sid or "").strip()
    if not recording_sid or not recording_sid.startswith("RE") or len(recording_sid) != 34:
        raise HTTPException(status_code=400, detail="Invalid recording SID")
    if not Config.is_transcription_enabled():
        raise HTTPException(status_code=503, detail="Transcription disabled; set TRANSCRIPTION_MODEL (e.g. tiny).")
    if not Config.has_twilio_credentials():
        raise HTTPException(status_code=503, detail="Twilio not configured")
    transcript = await asyncio.to_thread(transcribe_recording, recording_sid)
    if transcript is None:
        raise HTTPException(status_code=502, detail="Transcription failed (fetch or Whisper error)")
    record_id = (record_id or "").strip() if record_id else ""
    if record_id and has_call_record_backend_configured() and (Config.CALL_RECORD_BACKEND or "").strip().lower() == "supabase":
        try:
            updated = update_call_record_by_id_sync(record_id, {"transcript": transcript})
        except CallRecordUpdateSchemaError:
            updated = False
        if updated:
            from services.call_record_events import notify_call_records_changed_async

            await notify_call_records_changed_async()
    return {"transcript": transcript}


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    Log.header("Client connected")
    await websocket.accept()

    # Apply latest Supabase app_settings so this worker has current overrides (e.g. BOOKING_DAYS_ENABLED) for this call.
    # Without this, a different worker or an earlier cache can serve stale settings and wrong availability.
    try:
        from services.dynamic_settings import load_overrides_sync, apply_overrides_to_config
        from config import Config
        overrides = load_overrides_sync()
        if overrides:
            apply_overrides_to_config(overrides)
            Log.event("Applied overrides from Supabase", {"count": len(overrides), "BOOKING_DAYS_ENABLED": overrides.get("BOOKING_DAYS_ENABLED", "(not in overrides)")})
        else:
            # No Supabase or empty: ensure booking days from .env/Config is in os.environ for this process
            booking_days = getattr(Config, "BOOKING_DAYS_ENABLED", None) or os.environ.get("BOOKING_DAYS_ENABLED", "")
            if booking_days is not None:
                os.environ["BOOKING_DAYS_ENABLED"] = str(booking_days).strip()
    except Exception as e:
        Log.error(f"Apply overrides on connect: {e}")

    # Create connection manager and services
    connection_manager = WebSocketConnectionManager(websocket)
    # Event set when OpenAI sends session.updated; wait for it before first response so custom instructions are in effect
    connection_manager.state.session_updated_event = asyncio.Event()
    # Parse query params (caller number for inbound; direction/campaign/contact for outbound)
    outbound_system_message: str | None = None
    try:
        qs = parse_qs(websocket.scope.get("query_string", b"").decode())
        caller_number = (qs.get("caller_number") or [None])[0]
        direction = (qs.get("direction") or ["inbound"])[0]
        campaign_id = (qs.get("campaign_id") or [None])[0]
        contact_id = (qs.get("contact_id") or [None])[0]

        if caller_number:
            connection_manager.state.caller_phone_number = caller_number
            Log.event("Incoming caller number (traceability)", {
                "incoming_caller_number": caller_number,
                "source": "media-stream query (caller_number)",
            })

        if direction == "outbound" and campaign_id and contact_id:
            connection_manager.state.is_outbound_call = True
            Log.event("Outbound call stream connected", {
                "campaign_id": campaign_id,
                "contact_id": contact_id,
            })
            from services.outbound_service import build_outbound_system_message, get_contact_sync
            outbound_system_message = await asyncio.to_thread(
                build_outbound_system_message, campaign_id, contact_id
            )
            if not outbound_system_message:
                Log.info("Outbound system message could not be built; falling back to default")
            contact = await asyncio.to_thread(get_contact_sync, contact_id)
            if contact and (contact.get("phone") or "").strip():
                connection_manager.state.caller_phone_number = (contact.get("phone") or "").strip()
    except Exception:
        pass
    # Defer session init when query string was empty so we can resolve outbound context from CallSid when "start" arrives
    defer_session_init = (outbound_system_message is None) and (not caller_number)
    openai_service = OpenAIService()
    audio_service = AudioService()
    
    try:
        await connection_manager.connect_to_openai()
        if not defer_session_init:
            connection_manager.state.session_updated_event.clear()
            await openai_service.initialize_session(connection_manager, system_message_override=outbound_system_message)

        # Define event handlers for cleaner separation of concerns
        async def handle_media_event(data: dict) -> None:
            """Handle incoming media data from Twilio."""
            if connection_manager.is_openai_connected():
                audio_message = audio_service.process_incoming_audio(data)
                if audio_message:
                    await connection_manager.send_to_openai(audio_message)

        async def handle_stream_start(stream_sid: str) -> None:
            """Handle stream start: we now have stream_sid so audio can be sent to Twilio. Trigger AI greeting."""
            nonlocal outbound_system_message
            Log.event("Twilio stream started", {"streamSid": stream_sid})
            if defer_session_init:
                call_sid = getattr(connection_manager.state, "call_sid", None)
                outbound_ctx = TwilioService.get_outbound_context(call_sid) if call_sid else None
                if outbound_ctx:
                    ob_campaign_id, ob_contact_id = outbound_ctx
                    connection_manager.state.is_outbound_call = True  # so send_initial_greeting uses minimal item even if build fails
                    Log.event("Outbound call stream (from CallSid cache)", {"campaign_id": ob_campaign_id, "contact_id": ob_contact_id})
                    from services.outbound_service import build_outbound_system_message, get_contact_sync
                    outbound_system_message = await asyncio.to_thread(build_outbound_system_message, ob_campaign_id, ob_contact_id)
                    if not outbound_system_message:
                        Log.info("Outbound system message could not be built; falling back to default")
                    contact = await asyncio.to_thread(get_contact_sync, ob_contact_id)
                    if contact and (contact.get("phone") or "").strip():
                        connection_manager.state.caller_phone_number = (contact.get("phone") or "").strip()
                connection_manager.state.session_updated_event.clear()
                await openai_service.initialize_session(connection_manager, system_message_override=outbound_system_message)
                if not outbound_ctx:
                    await openai_service.send_caller_phone_session_update(connection_manager)
            else:
                await openai_service.send_caller_phone_session_update(connection_manager)
            try:
                await asyncio.wait_for(connection_manager.state.session_updated_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                Log.info("session.updated not received within 5s; proceeding with initial greeting")
            # Outbound: use minimal "begin now" item so first response follows session (campaign) instructions, not inbound greeting
            is_outbound = outbound_system_message is not None or connection_manager.state.is_outbound_call
            await openai_service.send_initial_greeting(connection_manager, is_outbound=is_outbound)
            # Pre-warm availability cache so first get_availability in this call is instant
            openai_service.prewarm_availability_cache()
            # Start call recording if enabled (fire-and-forget)
            if Config.is_call_recording_enabled() and getattr(connection_manager.state, "call_sid", None):
                asyncio.create_task(TwilioService.start_call_recording_async(connection_manager.state.call_sid))

        async def handle_mark_event() -> None:
            """Handle mark event from Twilio."""
            audio_service.handle_mark_event()

        async def handle_audio_delta(response: dict) -> None:
            """Handle audio delta from OpenAI."""
            if openai_service.should_suppress_assistant_audio():
                return
            audio_data = openai_service.extract_audio_response_data(response)
            if audio_data and connection_manager.state.stream_sid:
                # If we're in a goodbye flow, mark that farewell audio has started and capture its item_id
                if openai_service.is_goodbye_pending():
                    openai_service.mark_goodbye_audio_heard(audio_data.get('item_id'))
                audio_message = audio_service.process_outgoing_audio(
                    response, 
                    connection_manager.state.stream_sid
                )
                if audio_message:
                    await connection_manager.send_to_twilio(audio_message)
                    connection_manager.state.last_outgoing_audio_at = time.time()
                    # Send mark for synchronization
                    mark_message = audio_service.create_mark_message(connection_manager.state.stream_sid)
                    await connection_manager.send_to_twilio(mark_message)

        async def handle_speech_started() -> None:
            """Handle speech started event (interruption)."""
            Log.info("Speech started detected.")
            # Debounce: ignore if we just sent assistant audio (reduces echo / own-voice triggers)
            debounce_ms = getattr(Config, "VAD_DEBOUNCE_AFTER_OUTGOING_MS", 1200)
            last_at = getattr(connection_manager.state, "last_outgoing_audio_at", None)
            if last_at is not None and debounce_ms > 0:
                if (time.time() - last_at) * 1000 < debounce_ms:
                    Log.info("Ignoring speech_started (debounce: assistant audio recently sent)")
                    return
            # Do not interrupt the assistant's final goodbye
            if openai_service.is_goodbye_pending():
                Log.info("Ignoring interruption during goodbye flow")
                return
            current_item_id = audio_service.get_current_item_id()
            if not current_item_id:
                return
            confirm_ms = getattr(Config, "VAD_INTERRUPTION_CONFIRM_MS", 0) or 0
            if confirm_ms > 0:
                # Wait for confirm_ms; if speech_stopped arrives before then we cancel (treat as brief noise/cough)
                existing = getattr(connection_manager.state, "pending_interruption_confirm_task", None)
                if existing and not existing.done():
                    existing.cancel()
                async def delayed_interruption() -> None:
                    try:
                        await asyncio.sleep(confirm_ms / 1000.0)
                        connection_manager.state.pending_interruption_confirm_task = None
                        await handle_speech_started_event(connection_manager, openai_service, audio_service)
                    except asyncio.CancelledError:
                        Log.info("Interruption cancelled (speech stopped before confirm window)")
                connection_manager.state.pending_interruption_confirm_task = asyncio.create_task(delayed_interruption())
                Log.info(f"Interruption confirm: will truncate in {confirm_ms}ms unless speech_stopped")
            else:
                Log.info(f"Interrupting response with id: {current_item_id}")
                await handle_speech_started_event(connection_manager, openai_service, audio_service)

        async def handle_other_openai_event(response: dict) -> None:
            """Handle other OpenAI events."""
            if response.get("type") == "session.updated":
                ev = getattr(connection_manager.state, "session_updated_event", None)
                if ev is not None:
                    ev.set()
            # Cancel pending interruption if speech_stopped (brief noise/cough filtered out)
            if response.get("type") == "input_audio_buffer.speech_stopped":
                pending = getattr(connection_manager.state, "pending_interruption_confirm_task", None)
                if pending and not pending.done():
                    pending.cancel()
                    connection_manager.state.pending_interruption_confirm_task = None
            # Log events
            openai_service.process_event_for_logging(response)
            # Handle tool calls (e.g., end_call)
            if openai_service.is_tool_call(response):
                tool_call = openai_service.accumulate_tool_call(response)
                if tool_call:
                    handled = await openai_service.maybe_handle_tool_call(connection_manager, tool_call)
                    if handled and tool_call.get("name") == "wait_for_user":
                        await openai_service.finalize_wait_for_user(connection_manager, audio_service)
                    if handled:
                        return
            if response.get("type") == "response.done":
                openai_service.clear_assistant_audio_suppression()
            # If a goodbye was queued and we've heard its audio, finalize after the response completes
            if openai_service.should_finalize_on_event(response):
                await openai_service.finalize_goodbye(connection_manager)

        # Run Twilio receiver and OpenAI receiver; plus a renewal loop for OpenAI session
        async def openai_receiver():
            await connection_manager.receive_from_openai(
                handle_audio_delta, handle_speech_started, handle_other_openai_event
            )

        async def renew_openai_session():
            # Preemptively reconnect before the 60-minute cap to avoid session_expired drops
            while True:
                await asyncio.sleep(Config.REALTIME_SESSION_RENEW_SECONDS)
                try:
                    print("Preemptive OpenAI session renewal starting…")
                    await connection_manager.close_openai_connection()
                    await connection_manager.connect_to_openai()
                    await openai_service.initialize_session(connection_manager, system_message_override=outbound_system_message)
                    print("OpenAI session renewed.")
                except Exception as e:
                    print(f"OpenAI session renewal failed: {e}")

        await asyncio.gather(
            connection_manager.receive_from_twilio(handle_media_event, handle_stream_start, handle_mark_event),
            openai_receiver(),
            renew_openai_session(),
        )

    except WebSocketDisconnect:
        Log.info("Client disconnected")
    except Exception as e:
        err_msg = str(e)
        # Expected when call ended and OpenAI closed after keepalive timeout (1011)
        if "1011" in err_msg or "keepalive" in err_msg.lower() or "ping timeout" in err_msg.lower():
            Log.info("Media stream ended (OpenAI connection closed)")
        else:
            Log.error(f"Error in media stream handler: {e}")
    finally:
        await connection_manager.close_openai_connection()


async def handle_speech_started_event(
    connection_manager: WebSocketConnectionManager, 
    openai_service: OpenAIService,
    audio_service: AudioService
):
    """Handle interruption when the caller's speech starts."""
    Log.subheader("Handling speech started event")
    
    if audio_service.should_handle_interruption():
        elapsed_time = audio_service.calculate_interruption_timing()
        current_item_id = audio_service.get_current_item_id()
        
        if elapsed_time is not None and current_item_id:
            await openai_service.handle_interruption(
                connection_manager,
                elapsed_time,  # capped to actual sent audio (consistent with what we passed)
                current_item_id
            )
            
            # Clear audio and reset state
            clear_message = audio_service.create_clear_message(connection_manager.state.stream_sid)
            await connection_manager.send_to_twilio(clear_message)
            audio_service.reset_interruption_state()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)
