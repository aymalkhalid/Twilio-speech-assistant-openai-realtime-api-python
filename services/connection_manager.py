import json
import asyncio
import websockets
from typing import Optional, Callable, Awaitable, Any
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from config import Config
from services.log_utils import Log
from services.twilio_service import TwilioService


class ConnectionState:
    """
    Tracks the current Twilio stream session and manages connection state.
    
    - Holds the current stream SID for the active Twilio media stream session.
    - Provides methods to reset or clear state when a new stream starts or an interruption occurs.
    - Tracks call outcome for dynamic farewells: call_record_saved, appointment_booked, confirmed_slot_display, priority.
    
    Audio-related state is now managed by AudioService, so this class focuses only on connection/session state.
    """
    
    def __init__(self):
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.caller_phone_number: Optional[str] = None
        # Set in media-stream when outbound context resolved from CallSid cache; used so initial greeting uses minimal item
        self.is_outbound_call: bool = False
        # Timestamp (seconds) when we last sent assistant audio to Twilio (for echo debounce)
        self.last_outgoing_audio_at: Optional[float] = None
        # Call outcome for context-aware goodbye (set by save_call_record / book_appointment)
        self.call_record_saved: bool = False
        self.appointment_booked: bool = False
        self.confirmed_slot_display: Optional[str] = None  # e.g. "Tue Mar 4 at 09:00 AM"
        self.priority: Optional[str] = None  # e.g. "emergency", "same_day", "routine"
        # When a follow-up booking-management call resolves an existing call record,
        # later save_call_record should update that row instead of inserting a duplicate.
        self.resolved_call_record_id: Optional[str] = None
        # When VAD_INTERRUPTION_CONFIRM_MS > 0: task that will run truncation after delay; cancelled if speech_stopped first
        self.pending_interruption_confirm_task: Optional[Any] = None
    
    @property
    def lead_submitted(self) -> bool:
        """Legacy alias for call_record_saved."""
        return self.call_record_saved

    @lead_submitted.setter
    def lead_submitted(self, value: bool) -> None:
        self.call_record_saved = bool(value)

    @property
    def resolved_business_lead_id(self) -> Optional[str]:
        """Legacy alias for resolved_call_record_id."""
        return self.resolved_call_record_id

    @resolved_business_lead_id.setter
    def resolved_business_lead_id(self, value: Optional[str]) -> None:
        self.resolved_call_record_id = value

    def reset_stream_state(self) -> None:
        """Reset state when a new stream starts."""
        # Audio-related state is now managed by AudioService
        pass
    
    def clear_response_state(self) -> None:
        """Clear response-related state during interruptions."""
        # Audio-related state is now managed by AudioService
        pass


class WebSocketConnectionManager:
    """
    Orchestrates all WebSocket communication between Twilio and OpenAI for the application.
    
    - Establishes, maintains, and closes WebSocket connections to both Twilio (FastAPI) and OpenAI (websockets).
    - Routes incoming messages to appropriate event handlers for media, start, mark, audio delta, and speech events.
    - Sends and receives messages, manages connection state, and coordinates with AudioService for mark/clear events.
    
    This is the main interface for real-time, bidirectional communication and event-driven processing between the two services.
    """
    
    def __init__(self, twilio_ws: WebSocket):
        self.twilio_ws = twilio_ws
        self.openai_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.state = ConnectionState()
        self._is_connected = False
        self._twilio_closed = False  # Set when we close Twilio WS; send_to_twilio no-ops to avoid "not connected" after hangup
    
    async def connect_to_openai(self) -> None:
        """Establish connection to OpenAI WebSocket API."""
        try:
            self.openai_ws = await websockets.connect(
                Config.get_openai_websocket_url(),
                additional_headers=Config.get_openai_headers()
            )
            self._is_connected = True
            Log.event("OpenAI WebSocket Connected", {
                "url": Config.get_openai_websocket_url()
            })
        except Exception as e:
            Log.error(f"Failed to connect to OpenAI: {e}")
            raise
    
    async def close_openai_connection(self) -> None:
        """Close the OpenAI WebSocket connection."""
        if self.openai_ws and self._is_connected:
            await self.openai_ws.close()
            self._is_connected = False
            Log.info("Closed OpenAI WebSocket connection")
    
    async def send_to_openai(self, message: dict) -> None:
        """Send a message to OpenAI WebSocket."""
        if self.openai_ws and self._is_connected:
            await self.openai_ws.send(json.dumps(message))
        else:
            raise ConnectionError("OpenAI WebSocket is not connected")
    
    async def send_to_twilio(self, message: dict) -> None:
        """Send a message to Twilio WebSocket. No-op if we already closed the Twilio connection (avoids error after hangup)."""
        if self._twilio_closed:
            return
        try:
            await self.twilio_ws.send_json(message)
        except Exception as e:
            if self._twilio_closed:
                return
            # Twilio may have closed the stream (e.g. after REST hangup); treat as no-op
            msg = str(e).lower()
            if "accept" in msg or "not connected" in msg:
                return
            Log.error(f"Send to Twilio failed: {e}")
            raise
    
    async def receive_from_twilio(
        self, 
        on_media: Callable[[dict], Awaitable[None]],
        on_start: Callable[[str], Awaitable[None]],
        on_mark: Callable[[], Awaitable[None]]
    ) -> None:
        """
        Receive messages from Twilio and route them to appropriate handlers.
        
        Args:
            on_media: Handler for media events
            on_start: Handler for stream start events
            on_mark: Handler for mark events
        """
        try:
            async for message in self.twilio_ws.iter_text():
                data = json.loads(message)
                
                if data['event'] == 'media':
                    await on_media(data)
                elif data['event'] == 'start':
                    start_info = data.get('start', {})
                    stream_sid = start_info.get('streamSid')
                    call_sid = (
                        start_info.get('callSid')
                        or start_info.get('call_id')
                        or start_info.get('CallSid')  # Twilio webhook-style casing
                    )
                    self.state.stream_sid = stream_sid
                    self.state.call_sid = call_sid
                    # If we didn't get caller from media-stream URL (Twilio often sends empty query), use cache from POST webhook
                    if not getattr(self.state, "caller_phone_number", None) and call_sid:
                        from_cache = TwilioService.get_caller_for_call(call_sid)
                        if from_cache:
                            self.state.caller_phone_number = from_cache
                            Log.event("Caller number from cache (stream URL had no query)", {
                                "incoming_caller_number": from_cache,
                                "callSid": call_sid,
                            })
                    # Log the callSid if available, otherwise log the incoming start payload for debugging
                    if call_sid:
                        payload = {"streamSid": stream_sid, "callSid": call_sid}
                        if getattr(self.state, "caller_phone_number", None):
                            payload["incoming_caller_number"] = self.state.caller_phone_number
                        Log.event("Twilio Start", payload)
                    else:
                        try:
                            Log.event("Twilio Start (no callSid)", start_info)
                        except Exception:
                            Log.error("Twilio start payload (no callSid) and failed to serialize start_info.")
                    self.state.reset_stream_state()
                    await on_start(stream_sid)
                elif data['event'] == 'mark':
                    await on_mark()
            # Normal exit: Twilio closed the connection (e.g. user hung up) and iterator ended
            await self.close_openai_connection()
            raise WebSocketDisconnect("Twilio stream ended")
                    
        except WebSocketDisconnect:
            Log.info("Twilio WebSocket disconnected")
            await self.close_openai_connection()
            raise
        except Exception as e:
            # After REST hangup, Twilio closes the media stream; iter_text() can raise
            # "WebSocket is not connected. Need to call 'accept' first." Treat as normal disconnect.
            msg = str(e).lower()
            if "accept" in msg or "not connected" in msg:
                Log.info("Twilio WebSocket disconnected (connection closed)")
                await self.close_openai_connection()
                raise WebSocketDisconnect from e
            raise
    
    async def receive_from_openai(
        self,
        on_audio_delta: Callable[[dict], Awaitable[None]],
        on_speech_started: Callable[[], Awaitable[None]],
        on_other_event: Callable[[dict], Awaitable[None]]
    ) -> None:
        """
        Receive messages from OpenAI and route them to appropriate handlers.
        
        Args:
            on_audio_delta: Handler for audio delta events
            on_speech_started: Handler for speech started events  
            on_other_event: Handler for other events
        """
        if not self.openai_ws or not self._is_connected:
            raise ConnectionError("OpenAI WebSocket is not connected")
        
        try:
            # Import here to avoid circular imports
            from services.openai_service import OpenAIEventHandler
            
            async for openai_message in self.openai_ws:
                response = json.loads(openai_message)
                
                if OpenAIEventHandler.is_audio_delta_event(response):
                    await on_audio_delta(response)
                elif OpenAIEventHandler.is_speech_started_event(response):
                    await on_speech_started()
                else:
                    await on_other_event(response)
                    
        except Exception as e:
            err_msg = str(e)
            # Expected when call ended and OpenAI connection idles until keepalive timeout (1011)
            if "1011" in err_msg or "keepalive" in err_msg.lower() or "ping timeout" in err_msg.lower():
                Log.info("OpenAI WebSocket closed (keepalive timeout or connection ended)")
            else:
                Log.error(f"Error receiving from OpenAI: {e}")
            raise
    
    async def send_mark_to_twilio(self) -> None:
        """Send a mark event to Twilio using AudioService."""
        if self.state.stream_sid:
            # Import here to avoid circular imports
            from services.audio_service import AudioService
            audio_service = AudioService()
            mark_event = audio_service.create_mark_message(self.state.stream_sid)
            await self.send_to_twilio(mark_event)
    
    async def clear_twilio_audio(self) -> None:
        """Clear audio buffer in Twilio using AudioService."""
        if self.state.stream_sid:
            # Import here to avoid circular imports
            from services.audio_service import AudioService
            audio_service = AudioService()
            clear_event = audio_service.create_clear_message(self.state.stream_sid)
            await self.send_to_twilio(clear_event)
    
    def is_openai_connected(self) -> bool:
        """Check if OpenAI WebSocket is connected and ready."""
        return (self.openai_ws is not None and 
                self._is_connected and 
                self.openai_ws.state.name == 'OPEN')

    def mark_twilio_closed(self) -> None:
        """Mark the Twilio connection as closed so send_to_twilio() no-ops. Call at the start of
        goodbye flow to avoid late OpenAI events sending on a socket that may already be closed."""
        self._twilio_closed = True

    async def close_twilio_connection(self, code: int = 1000, reason: str | None = None) -> None:
        """Close the Twilio WebSocket connection gracefully.
        This ends the <Connect><Stream> on Twilio's side; with no more TwiML verbs,
        Twilio will conclude the call. Useful when REST hangup is not configured.
        After this, send_to_twilio() will no-op so late OpenAI events don't raise.
        """
        self._twilio_closed = True
        try:
            await self.twilio_ws.close(code=code, reason=reason or "normal closure")
            Log.info("Closed Twilio WebSocket connection")
        except Exception as e:
            Log.error(f"Failed to close Twilio WebSocket: {e}")
