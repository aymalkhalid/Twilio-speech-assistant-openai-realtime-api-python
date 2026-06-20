import asyncio
import base64
import time
from urllib.parse import quote, parse_qs
from typing import Optional
from fastapi import Request
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect

from config import Config
from services.log_utils import Log

# CallSid -> (From, timestamp); used when media-stream WebSocket has no query string (Twilio may not forward it).
_CALLER_CACHE: dict[str, tuple[str, float]] = {}
_CALLER_CACHE_TTL_SEC = 300

# CallSid -> (campaign_id, contact_id, timestamp); used when outbound stream connects with empty query (Twilio may not forward it).
_OUTBOUND_CONTEXT_CACHE: dict[str, tuple[str, str, float]] = {}


class TwilioService:
    """
    Provides all Twilio integration logic for the application.
    
    - Generates TwiML responses for incoming calls, connecting callers to the media stream.
    - Creates Twilio-compatible messages (media, mark, clear) for the WebSocket Media Streams API.
    - Converts audio data formats between OpenAI and Twilio.
    - Extracts and interprets Twilio event payloads.
    
    This class is the main entry point for all Twilio-related operations and is used by higher-level services to interact with Twilio Voice and Media Streams.
    """
    
    # Twilio voice configuration
    TWILIO_VOICE = "Google.en-US-Chirp3-HD-Aoede"
    
    @classmethod
    async def create_incoming_call_response(cls, request: Request) -> HTMLResponse:
        """
        Create TwiML response for incoming calls to connect to Media Stream.
        Passes caller phone number (From) as a query param so the stream handler can give it to OpenAI.
        
        Args:
            request: FastAPI request object (hostname, form for From)
        
        Returns:
            HTMLResponse containing TwiML XML
        """
        response = VoiceResponse()
        # No Twilio TTS — the voice agent will greet the caller once the stream is connected.

        host = request.url.hostname
        stream_url = f"wss://{host}/media-stream"
        # Twilio always sends caller info (From) on the voice webhook; capture from body, form, or query.
        from_number = ""
        source_used = "none"

        def _first(d: dict, *keys: str) -> str:
            for k in keys:
                v = d.get(k)
                if v is not None and (isinstance(v, list) and len(v) > 0 or not isinstance(v, list)):
                    val = v[0] if isinstance(v, list) else v
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="replace")
                    return (str(val).strip() or "")
            return ""

        # 1) POST: read raw body first (Twilio sends application/x-www-form-urlencoded)
        #    We parse it ourselves because request.form() can be empty in some deployments.
        if request.method == "POST":
            try:
                body = await request.body()
                if body:
                    params = parse_qs(body.decode("utf-8", errors="replace"))
                    # Log raw form (parsed) for debugging; redact sensitive keys
                    _redact_keys = frozenset({"AccountSid", "AuthToken", "ApiVersion"})
                    raw_form_log = {
                        k: (v[0] if isinstance(v, list) and v else v) for k, v in params.items()
                    }
                    for k in list(raw_form_log.keys()):
                        if k in _redact_keys or "token" in k.lower() or "secret" in k.lower():
                            raw_form_log[k] = "(redacted)"
                        elif isinstance(raw_form_log[k], list):
                            raw_form_log[k] = raw_form_log[k][0] if raw_form_log[k] else ""
                    Log.event("Twilio webhook raw form (body)", {
                        "body_len": len(body),
                        "params": raw_form_log,
                    })
                    from_number = _first(params, "From", "from")
                    call_sid = _first(params, "CallSid", "CallSid")
                    if from_number and call_sid:
                        _CALLER_CACHE[call_sid] = (from_number, time.time())
                    if from_number:
                        source_used = "body"
                    else:
                        Log.event("Twilio webhook body (no From)", {"param_keys": list(params.keys())[:20]})
                else:
                    # Body empty — try form() (will re-read; some ASGI servers allow this)
                    form = await request.form()
                    from_number = (form.get("From") or form.get("from") or "").strip()
                    if isinstance(from_number, list):
                        from_number = (from_number[0] or "").strip() if from_number else ""
                    if from_number:
                        source_used = "form"
                    elif list(form.keys()):
                        Log.event("Twilio webhook form (no From)", {"form_keys": list(form.keys())[:20]})
            except Exception as e:
                Log.event("Twilio webhook body read failed", {"error": str(e)})
                try:
                    form = await request.form()
                    from_number = (form.get("From") or form.get("from") or "").strip()
                    if isinstance(from_number, list):
                        from_number = (from_number[0] or "").strip() if from_number else ""
                    if from_number:
                        source_used = "form"
                except Exception:
                    pass
        # 2) Fallback: query params (GET or URL params)
        if not from_number:
            q = dict(request.query_params)
            from_number = (q.get("From") or q.get("from") or "").strip()
            if from_number:
                source_used = "query_params"
        # Log what Twilio sent so we can debug wrong-number issues (e.g. AI saying 512-555-3489 instead of real caller)
        Log.event("Incoming call — caller number (traceability)", {
            "incoming_caller_number": from_number or "(empty — webhook may be GET or From not sent)",
            "source": f"Twilio webhook (From), from {source_used}",
            "request_method": getattr(request, "method", "?"),
        })
        if not from_number:
            Log.event("Caller number missing — AI may invent a number when confirming", {
                "hint": "Set voice webhook to POST so Twilio sends From in body; or ensure URL includes From in query.",
            })
        if from_number:
            stream_url += "?caller_number=" + quote(from_number, safe="")

        connect = Connect()
        connect.stream(url=stream_url)
        response.append(connect)

        return HTMLResponse(content=str(response), media_type="application/xml")

    @classmethod
    def get_caller_for_call(cls, call_sid: Optional[str]) -> Optional[str]:
        """
        Return From (caller number) for this call_sid if we stored it on the voice webhook.
        Used when the media-stream WebSocket connects with an empty query string (Twilio may not forward it).
        """
        if not call_sid:
            return None
        now = time.time()
        entry = _CALLER_CACHE.get(call_sid)
        if not entry:
            return None
        from_number, ts = entry
        if now - ts > _CALLER_CACHE_TTL_SEC:
            del _CALLER_CACHE[call_sid]
            return None
        return from_number

    @classmethod
    def register_outbound_context(cls, call_sid: str, campaign_id: str, contact_id: str) -> None:
        """
        Store (campaign_id, contact_id) for this call_sid so the media-stream handler
        can apply the outbound agent when the WebSocket connects with an empty query string.
        """
        if call_sid and campaign_id and contact_id:
            _OUTBOUND_CONTEXT_CACHE[call_sid] = (campaign_id, contact_id, time.time())

    @classmethod
    def get_outbound_context(cls, call_sid: Optional[str]) -> Optional[tuple[str, str]]:
        """
        Return (campaign_id, contact_id) for this call_sid if we stored it when creating the outbound call.
        Used when the media-stream WebSocket connects with an empty query string.
        """
        if not call_sid:
            return None
        now = time.time()
        entry = _OUTBOUND_CONTEXT_CACHE.get(call_sid)
        if not entry:
            return None
        campaign_id, contact_id, ts = entry
        if now - ts > _CALLER_CACHE_TTL_SEC:
            del _OUTBOUND_CONTEXT_CACHE[call_sid]
            return None
        return (campaign_id, contact_id)

    @staticmethod
    def create_media_message(stream_sid: str, audio_payload: str) -> dict:
        """
        Create a Twilio media message with audio payload.
        
        Args:
            stream_sid: Twilio stream identifier
            audio_payload: Base64 encoded audio data
            
        Returns:
            Dictionary containing Twilio media message
        """
        return {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": audio_payload
            }
        }
    
    @staticmethod
    def create_mark_message(stream_sid: str, mark_name: str = "responsePart") -> dict:
        """
        Create a Twilio mark message for audio synchronization.
        
        Args:
            stream_sid: Twilio stream identifier
            mark_name: Name of the mark for identification
            
        Returns:
            Dictionary containing Twilio mark message
        """
        return {
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": mark_name}
        }
    
    @staticmethod
    def create_clear_message(stream_sid: str) -> dict:
        """
        Create a Twilio clear message to clear audio buffer.
        
        Args:
            stream_sid: Twilio stream identifier
            
        Returns:
            Dictionary containing Twilio clear message
        """
        return {
            "event": "clear",
            "streamSid": stream_sid
        }
    
    @staticmethod
    def convert_openai_audio_to_twilio(openai_audio_delta: str) -> str:
        """
        Convert OpenAI audio delta format to Twilio-compatible format.
        
        OpenAI provides base64 encoded audio, which we need to re-encode
        for Twilio's expected format.
        
        Args:
            openai_audio_delta: Base64 encoded audio from OpenAI
            
        Returns:
            Base64 encoded audio payload for Twilio
        """
        # Decode and re-encode to ensure proper format for Twilio
        return base64.b64encode(base64.b64decode(openai_audio_delta)).decode('utf-8')
    
    @staticmethod
    def extract_stream_id(start_event_data: dict) -> Optional[str]:
        """
        Extract stream ID from Twilio start event data.
        
        Args:
            start_event_data: Twilio start event data
            
        Returns:
            Stream ID if found, None otherwise
        """
        try:
            return start_event_data['start']['streamSid']
        except (KeyError, TypeError):
            return None
    
    @staticmethod
    def extract_media_payload(media_event_data: dict) -> Optional[str]:
        """
        Extract audio payload from Twilio media event data.
        
        Args:
            media_event_data: Twilio media event data
            
        Returns:
            Audio payload if found, None otherwise
        """
        try:
            return media_event_data['media']['payload']
        except (KeyError, TypeError):
            return None
    
    @staticmethod
    def extract_media_timestamp(media_event_data: dict) -> Optional[int]:
        """
        Extract timestamp from Twilio media event data.
        
        Args:
            media_event_data: Twilio media event data
            
        Returns:
            Timestamp if found, None otherwise
        """
        try:
            return int(media_event_data['media']['timestamp'])
        except (KeyError, TypeError, ValueError):
            return None
    
    @staticmethod
    def is_media_event(event_data: dict) -> bool:
        """Check if event data represents a Twilio media event."""
        return event_data.get('event') == 'media'
    
    @staticmethod
    def is_start_event(event_data: dict) -> bool:
        """Check if event data represents a Twilio start event."""
        return event_data.get('event') == 'start'
    
    @staticmethod
    def is_mark_event(event_data: dict) -> bool:
        """Check if event data represents a Twilio mark event."""
        return event_data.get('event') == 'mark'

    @classmethod
    async def start_call_recording_async(cls, call_sid: str) -> None:
        """
        Start recording an active call via Twilio REST and set RecordingStatusCallback.
        No-op if call recording is disabled or callback URL not set. Runs the sync Twilio
        call in a thread so the media stream is not blocked.
        """
        if not Config.is_call_recording_enabled():
            return
        callback_url = Config.get_recording_status_callback_url()
        if not callback_url:
            return
        if not call_sid or not call_sid.strip():
            return
        if not Config.has_twilio_credentials():
            Log.event("Call recording skipped: no Twilio credentials", {"call_sid": call_sid})
            return

        def _start_recording() -> None:
            from twilio.rest import Client
            client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
            client.calls(call_sid).recordings.create(
                recording_status_callback=callback_url,
                recording_status_callback_event=["completed"],
            )

        try:
            await asyncio.to_thread(_start_recording)
            Log.event("Call recording started", {"call_sid": call_sid})
        except Exception as e:
            Log.error(f"Call recording start failed: {e}")

    @classmethod
    async def create_outbound_call(
        cls,
        to: str,
        twiml_url: str,
        from_number: str = "",
        status_callback: str = "",
    ) -> object:
        """
        Initiate an outbound call via Twilio REST API.
        Runs the sync REST call in a thread so the event loop is not blocked.

        Args:
            to: Destination phone number (E.164)
            twiml_url: URL returning TwiML for the answered call
            from_number: Caller ID number (E.164); falls back to Config
            status_callback: URL for call status webhooks

        Returns:
            Twilio CallInstance object
        """
        if not Config.has_twilio_credentials():
            raise RuntimeError("Twilio credentials not configured")
        actual_from = (from_number or Config.get_outbound_from_number()).strip()
        if not actual_from:
            raise RuntimeError("No outbound From number configured (TWILIO_OUTBOUND_NUMBER)")

        def _create():
            from twilio.rest import Client
            client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
            kwargs = {
                "to": to,
                "from_": actual_from,
                "url": twiml_url,
            }
            if status_callback:
                kwargs["status_callback"] = status_callback
                kwargs["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]
            return client.calls.create(**kwargs)

        call = await asyncio.to_thread(_create)
        Log.event("Outbound call initiated", {"to": to, "from": actual_from, "call_sid": call.sid})
        return call

    @classmethod
    async def redirect_call_to_url_async(
        cls, call_sid: str, url: str, method: str = "POST"
    ) -> None:
        """
        Redirect an active call to a new TwiML URL via Twilio REST.
        The call will leave the media stream and execute the TwiML at the given URL.
        Runs the sync REST call in a thread so the event loop is not blocked.
        """
        call_sid = (call_sid or "").strip()
        if not call_sid:
            Log.event("Redirect skipped: no call_sid", {})
            return
        url_stripped = (url or "").strip()
        if not url_stripped:
            Log.event("Redirect skipped: no URL", {"call_sid": call_sid})
            return
        if not Config.has_twilio_credentials():
            Log.event("Redirect skipped: no Twilio credentials", {"call_sid": call_sid})
            return

        def _redirect() -> None:
            from twilio.rest import Client
            client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(url=url_stripped, method=method)

        try:
            await asyncio.to_thread(_redirect)
            Log.event("Call redirected to agent", {"call_sid": call_sid, "url": url_stripped})
        except Exception as e:
            Log.error(f"Call redirect failed: {e}")

    @classmethod
    async def end_call_async(cls, call_sid: str) -> None:
        """
        End an active call by setting its status to 'completed' via Twilio REST.
        Used when the user clicks Stop on an outbound call. Twilio will send the
        status callback and the contact row will be updated to completed.
        """
        call_sid = (call_sid or "").strip()
        if not call_sid:
            Log.event("End call skipped: no call_sid", {})
            return
        if not Config.has_twilio_credentials():
            Log.event("End call skipped: no Twilio credentials", {"call_sid": call_sid})
            return

        def _end():
            from twilio.rest import Client
            client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(status="completed")

        try:
            await asyncio.to_thread(_end)
            Log.event("Outbound call ended by user", {"call_sid": call_sid})
        except Exception as e:
            Log.error(f"End call failed: {e}")
            raise


class TwilioAudioProcessor:
    """
    Handles audio data preparation and conversion for Twilio and OpenAI.
    
    - Prepares Twilio audio payloads for OpenAI's Realtime API.
    - Converts OpenAI audio deltas into Twilio-compatible media messages.
    
    This class is typically used internally by the TwilioService or audio pipeline to ensure audio data is in the correct format for each service.
    """
    
    @staticmethod
    def prepare_audio_for_openai(twilio_payload: str) -> dict:
        """
        Prepare Twilio audio payload for OpenAI Realtime API.
        
        Args:
            twilio_payload: Audio payload from Twilio
            
        Returns:
            Dictionary formatted for OpenAI input_audio_buffer.append
        """
        return {
            "type": "input_audio_buffer.append",
            "audio": twilio_payload
        }
    
    @staticmethod
    def prepare_audio_for_twilio(openai_delta: str, stream_sid: str) -> dict:
        """
        Prepare OpenAI audio delta for Twilio media stream.
        
        Args:
            openai_delta: Audio delta from OpenAI
            stream_sid: Twilio stream identifier
            
        Returns:
            Dictionary formatted for Twilio media message
        """
        converted_payload = TwilioService.convert_openai_audio_to_twilio(openai_delta)
        return TwilioService.create_media_message(stream_sid, converted_payload)
