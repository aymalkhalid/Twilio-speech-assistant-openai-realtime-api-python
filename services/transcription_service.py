"""
Transcription of call recordings using open-source Whisper (faster-whisper).
Optional: post-process with OpenAI mini model to fix errors, format as Agent/Caller dialogue,
and produce a summary + issues for sales & service.
"""
import tempfile
from pathlib import Path
from typing import Optional

from config import Config
from services.log_utils import Log

# Delimiters for structured enhancement output (must match parser below)
ENHANCEMENT_DELIM_TRANSCRIPT = "---TRANSCRIPT---"
ENHANCEMENT_DELIM_SUMMARY = "---SUMMARY---"
ENHANCEMENT_DELIM_ISSUES = "---ISSUES---"

ENHANCEMENT_SYSTEM_PROMPT = """You are given a raw call transcript from speech-to-text. Produce a structured response with three parts. Use ONLY these exact section headers (no numbering before them):

---TRANSCRIPT---
Fix mistranscriptions, typos, and punctuation. Infer speaker turns and format as a clear dialogue. Use exactly "Agent:" for the AI/company side and "Caller:" for the caller. One turn per line. Output only the dialogue in this section.

---SUMMARY---
In 2-4 sentences, summarize the call for the sales or service team: what the caller needed, what was agreed (e.g. callback, booking), and any suggested next step. Be concise and actionable.

---ISSUES---
Bullet list of issues or important details the team should know, e.g.: "Customer requested live agent", "Callback requested", "Unresolved complaint", "Key contact: [name], [phone]", "Service address: [address]". If nothing notable, write "None". Use short bullet points.

Output ONLY the three sections with these exact headers (---TRANSCRIPT---, ---SUMMARY---, ---ISSUES---). No numbering, preamble, or explanation."""

ENHANCEMENT_USER_TEMPLATE = "Process this call transcript (format as dialogue, then add summary and issues):\n\n%s"

# Lazy-loaded model (one per process)
_whisper_model = None


def fetch_recording_to_path(recording_sid: str, path: str | Path) -> bool:
    """
    Fetch Twilio recording MP3 by SID and write to the given path.
    Uses TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN. Returns True on success.
    """
    recording_sid = (recording_sid or "").strip()
    if not recording_sid or not recording_sid.startswith("RE") or len(recording_sid) != 34:
        return False
    if not Config.has_twilio_credentials():
        Log.error("fetch_recording: Twilio not configured")
        return False
    import requests
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{Config.TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.mp3"
    )
    try:
        r = requests.get(
            url,
            auth=(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN or ""),
            timeout=120,
        )
        if r.status_code != 200:
            Log.error(f"fetch_recording: Twilio returned {r.status_code}")
            return False
        Path(path).write_bytes(r.content)
        return True
    except Exception as e:
        Log.error(f"fetch_recording error: {e}")
        return False


def transcribe_audio(audio_path: str | Path) -> Optional[str]:
    """
    Transcribe an audio file (e.g. MP3) to text using faster-whisper.
    Returns plain text or None on error. Model is loaded lazily.
    """
    if not Config.is_transcription_enabled():
        Log.error("transcribe_audio: TRANSCRIPTION_MODEL not set")
        return None
    path = Path(audio_path)
    if not path.is_file():
        Log.error(f"transcribe_audio: file not found {path}")
        return None
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            model_name = Config.TRANSCRIPTION_MODEL or "tiny"
            if Config.WHISPER_MODEL_PATH and Path(Config.WHISPER_MODEL_PATH).is_dir():
                load_path = Config.WHISPER_MODEL_PATH
                Log.info(f"Loading Whisper model from bundled path: {load_path}")
                _whisper_model = WhisperModel(load_path, device="cpu", compute_type="int8")
            else:
                Log.info(f"Loading Whisper model: {model_name}")
                _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
        except Exception as e:
            Log.error(f"Failed to load Whisper model: {e}")
            return None
    try:
        segments, _ = _whisper_model.transcribe(str(path), beam_size=5, language="en")
        parts = [s.text for s in segments if s.text]
        return " ".join(parts).strip() if parts else ""
    except Exception as e:
        Log.error(f"Whisper transcribe error: {e}")
        return None


def _strip_trailing_numbered_line(s: str) -> str:
    """Remove a single trailing line that is only a section number (e.g. '2.' or '3.')."""
    if not s or not s.strip():
        return s
    lines = s.rstrip().split("\n")
    while lines and lines[-1].strip() and lines[-1].strip().rstrip(".").isdigit():
        lines.pop()
    return "\n".join(lines).strip()


def _parse_enhanced_output(content: str) -> dict[str, str]:
    """
    Parse structured enhancement output into transcript, summary, issues.
    Expects sections ---TRANSCRIPT---, ---SUMMARY---, ---ISSUES--- in that order.
    Returns dict with keys transcript, summary, issues. Missing sections are empty string.
    Strips trailing "2." / "3." lines that the model may emit before the next section header.
    """
    out: dict[str, str] = {"transcript": "", "summary": "", "issues": ""}
    if not content or not content.strip():
        return out
    text = content.strip()
    t_parts = text.split(ENHANCEMENT_DELIM_TRANSCRIPT, 1)
    if len(t_parts) != 2:
        out["transcript"] = text
        return out
    after_t = t_parts[1]
    s_parts = after_t.split(ENHANCEMENT_DELIM_SUMMARY, 1)
    out["transcript"] = _strip_trailing_numbered_line(s_parts[0].strip())
    after_s = s_parts[1].strip() if len(s_parts) > 1 else ""
    i_parts = after_s.split(ENHANCEMENT_DELIM_ISSUES, 1)
    out["summary"] = _strip_trailing_numbered_line(i_parts[0].strip() if i_parts else "")
    out["issues"] = i_parts[1].strip() if len(i_parts) > 1 else ""
    if not out["transcript"]:
        out["transcript"] = text
    return out


def enhance_transcript_with_summary(raw: str) -> Optional[dict[str, str]]:
    """
    Post-process raw Whisper transcript with OpenAI: formatted dialogue + summary + issues.
    Returns dict with keys transcript, summary, issues, or None on failure.
    """
    if not raw or not (raw := raw.strip()):
        return None
    if not Config.is_transcript_enhancement_enabled():
        return {"transcript": raw, "summary": "", "issues": ""}
    model = (Config.TRANSCRIPT_ENHANCEMENT_MODEL or "").strip() or "gpt-4o-mini"
    Log.info(f"Enhancing transcript with OpenAI ({model})")
    import requests
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {Config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": Config.TRANSCRIPT_ENHANCEMENT_MODEL.strip(),
        "messages": [
            {"role": "system", "content": ENHANCEMENT_SYSTEM_PROMPT},
            {"role": "user", "content": ENHANCEMENT_USER_TEMPLATE % raw},
        ],
        "temperature": Config.TRANSCRIPT_ENHANCEMENT_TEMPERATURE,
        "max_tokens": Config.TRANSCRIPT_ENHANCEMENT_MAX_TOKENS,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            Log.error(f"Transcript enhancement API error: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        content = (choice.get("message") or {}).get("content")
        if content is None:
            return None
        parsed = _parse_enhanced_output(content.strip())
        if not parsed["transcript"]:
            parsed["transcript"] = raw
        return parsed
    except Exception as e:
        Log.error(f"Transcript enhancement error: {e}")
        return None


def enhance_transcript(raw: str) -> Optional[str]:
    """
    Post-process raw Whisper transcript with OpenAI: fix errors, format as Agent/Caller dialogue.
    Returns enhanced transcript text only (for auto-enhance path). Uses same structured API
    and returns the transcript part.
    """
    if not raw or not (raw := raw.strip()):
        return raw
    result = enhance_transcript_with_summary(raw)
    if result is None:
        return raw
    return result.get("transcript") or raw


def transcribe_recording(recording_sid: str) -> Optional[str]:
    """
    Fetch Twilio recording by SID, transcribe with Whisper, optionally enhance with OpenAI mini; return plain text.
    Returns None on fetch or transcription failure.
    """
    recording_sid = (recording_sid or "").strip()
    if not recording_sid or not recording_sid.startswith("RE") or len(recording_sid) != 34:
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
        if not fetch_recording_to_path(recording_sid, tmp_path):
            return None
        raw = transcribe_audio(tmp_path)
        if raw is None:
            return None
        if not Config.is_transcript_enhancement_auto():
            return raw
        enhanced = enhance_transcript(raw)
        return enhanced if enhanced is not None else raw
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
