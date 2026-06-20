import base64
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from config import Config


@dataclass
class AudioMetadata:
    """
    Represents metadata for a single audio chunk, including timing, item, and stream information.
    Used to track and annotate audio data as it flows through the processing pipeline.
    """
    timestamp: Optional[int] = None
    item_id: Optional[str] = None
    stream_id: Optional[str] = None
    payload: Optional[str] = None
    format_type: Optional[str] = None


class AudioFormatConverter:
    """
    Converts audio payloads between Twilio and OpenAI formats.
    Ensures compatibility and provides a single place to update format logic if requirements change.
    """
    
    # Audio format constants
    OPENAI_FORMAT = "audio/pcmu"
    TWILIO_FORMAT = "audio/pcmu"
    
    @staticmethod
    def twilio_to_openai(twilio_payload: str) -> str:
        """
        Convert Twilio audio payload to OpenAI-compatible format.
        
        Args:
            twilio_payload: Raw audio payload from Twilio
            
        Returns:
            Audio payload formatted for OpenAI
        """
        # Currently both use the same format, but this method provides
        # a clean interface for future format differences
        return twilio_payload
    
    @staticmethod
    def openai_to_twilio(openai_delta: str) -> str:
        """
        Convert OpenAI audio delta to Twilio-compatible format.
        
        Args:
            openai_delta: Base64 encoded audio from OpenAI
            
        Returns:
            Base64 encoded audio payload for Twilio
        """
        # Decode and re-encode to ensure proper format for Twilio
        return base64.b64encode(base64.b64decode(openai_delta)).decode('utf-8')
    
    @staticmethod
    def validate_audio_payload(payload: str) -> bool:
        """
        Validate that an audio payload is properly formatted base64.
        
        Args:
            payload: Audio payload to validate
            
        Returns:
            True if payload is valid base64, False otherwise
        """
        try:
            base64.b64decode(payload)
            return True
        except Exception:
            return False


class AudioTimingManager:
    """
    Tracks and manages audio timing for responses and interruptions.
    Responsible for calculating durations, tracking the start of responses, and supporting precise interruption logic.
    """
    
    def __init__(self):
        self.current_timestamp: int = 0
        self.response_start_timestamp: Optional[int] = None
        self.last_item_id: Optional[str] = None
        # Total ms of assistant audio sent for the current response (used to cap truncation)
        self.response_audio_sent_ms: int = 0

    def update_timestamp(self, timestamp: int) -> None:
        """Update the current audio timestamp."""
        self.current_timestamp = timestamp
    
    def start_response_tracking(self, item_id: str) -> None:
        """
        Start tracking a new response for timing calculations.
        
        Args:
            item_id: ID of the response item to track
        """
        self.response_start_timestamp = self.current_timestamp
        self.last_item_id = item_id
        self.response_audio_sent_ms = 0

        if Config.SHOW_TIMING_MATH:
            print(f"Starting response tracking for item {item_id} at {self.current_timestamp}ms")
    
    def calculate_response_duration(self) -> Optional[int]:
        """
        Calculate the duration of the current response.
        
        Returns:
            Duration in milliseconds, or None if no response is being tracked
        """
        if self.response_start_timestamp is None:
            return None
        
        duration = self.current_timestamp - self.response_start_timestamp
        
        if Config.SHOW_TIMING_MATH:
            print(f"Response duration: {self.current_timestamp} - {self.response_start_timestamp} = {duration}ms")
        
        return duration
    
    def add_response_audio_ms(self, duration_ms: int) -> None:
        """Accumulate duration of outgoing audio sent for the current response (for truncation cap)."""
        self.response_audio_sent_ms = (self.response_audio_sent_ms or 0) + duration_ms

    def reset_response_tracking(self) -> None:
        """Reset response tracking state."""
        self.response_start_timestamp = None
        self.last_item_id = None
        self.response_audio_sent_ms = 0
    
    def should_item_be_tracked(self, item_id: str) -> bool:
        """
        Determine if a new item should start being tracked.
        
        Args:
            item_id: ID of the item to check
            
        Returns:
            True if item should be tracked (is different from current)
        """
        return item_id != self.last_item_id
    
    def get_current_item_id(self) -> Optional[str]:
        """Get the ID of the currently tracked item."""
        return self.last_item_id


class AudioBufferManager:
    """
    Handles buffering of audio chunks and synchronization marks.
    Maintains queues for both audio data and marks, supporting smooth streaming and interruption handling.
    """
    
    def __init__(self):
        self.mark_queue: list = []
        self.audio_buffer: list = []
    
    def add_mark(self, mark_name: str = "responsePart") -> None:
        """
        Add a synchronization mark to the queue.
        
        Args:
            mark_name: Name of the mark for identification
        """
        self.mark_queue.append(mark_name)
    
    def remove_mark(self) -> Optional[str]:
        """
        Remove and return the oldest mark from the queue.
        
        Returns:
            The removed mark name, or None if queue is empty
        """
        return self.mark_queue.pop(0) if self.mark_queue else None
    
    def clear_marks(self) -> None:
        """Clear all marks from the queue."""
        self.mark_queue.clear()
    
    def has_pending_marks(self) -> bool:
        """Check if there are pending marks in the queue."""
        return len(self.mark_queue) > 0
    
    def add_audio_chunk(self, chunk: str, metadata: AudioMetadata) -> None:
        """
        Add an audio chunk to the buffer with metadata.
        
        Args:
            chunk: Audio data chunk
            metadata: Associated metadata
        """
        self.audio_buffer.append({
            'chunk': chunk,
            'metadata': metadata,
            'timestamp': metadata.timestamp
        })
    
    def clear_audio_buffer(self) -> None:
        """Clear the audio buffer."""
        self.audio_buffer.clear()
    
    def get_buffer_size(self) -> int:
        """Get the current size of the audio buffer."""
        return len(self.audio_buffer)


class AudioService:
    """
    Coordinates all audio processing operations for the application.
    Uses the format converter, timing manager, and buffer manager to process incoming and outgoing audio,
    manage synchronization, and handle interruptions between Twilio and OpenAI.
    """
    
    def __init__(self):
        self.format_converter = AudioFormatConverter()
        self.timing_manager = AudioTimingManager()
        self.buffer_manager = AudioBufferManager()
    
    def process_incoming_audio(self, twilio_data: dict) -> Optional[Dict[str, Any]]:
        """
        Process incoming audio data from Twilio.
        
        Args:
            twilio_data: Raw data from Twilio media event
            
        Returns:
            Processed audio message for OpenAI, or None if invalid
        """
        # Extract audio metadata
        payload = self._extract_twilio_payload(twilio_data)
        timestamp = self._extract_twilio_timestamp(twilio_data)
        
        if not payload or timestamp is None:
            return None
        
        # Update timing
        self.timing_manager.update_timestamp(timestamp)
        
        # Convert format
        converted_payload = self.format_converter.twilio_to_openai(payload)
        
        # Create metadata
        metadata = AudioMetadata(
            timestamp=timestamp,
            payload=converted_payload,
            format_type=self.format_converter.OPENAI_FORMAT
        )
        
        # Add to buffer
        self.buffer_manager.add_audio_chunk(converted_payload, metadata)
        
        # Return OpenAI message
        return {
            "type": "input_audio_buffer.append",
            "audio": converted_payload
        }
    
    def process_outgoing_audio(self, openai_data: dict, stream_id: str) -> Optional[Dict[str, Any]]:
        """
        Process outgoing audio data from OpenAI.
        
        Args:
            openai_data: Raw data from OpenAI audio delta event
            stream_id: Twilio stream identifier
            
        Returns:
            Processed audio message for Twilio, or None if invalid
        """
        # Extract audio data
        delta = openai_data.get('delta')
        item_id = openai_data.get('item_id')
        
        if not delta:
            return None
        
        # Handle timing for new responses
        if item_id and self.timing_manager.should_item_be_tracked(item_id):
            self.timing_manager.start_response_tracking(item_id)
        
        # Convert format
        converted_payload = self.format_converter.openai_to_twilio(delta)
        # Track sent duration so truncation never exceeds actual content (PCMU: 8 kHz 8-bit = 8 bytes/ms)
        try:
            decoded_len = len(base64.b64decode(converted_payload))
            self.timing_manager.add_response_audio_ms(decoded_len // 8)
        except Exception:
            pass

        # Create metadata
        metadata = AudioMetadata(
            timestamp=self.timing_manager.current_timestamp,
            item_id=item_id,
            stream_id=stream_id,
            payload=converted_payload,
            format_type=self.format_converter.TWILIO_FORMAT
        )
        
        # Add to buffer
        self.buffer_manager.add_audio_chunk(converted_payload, metadata)
        
        # Return Twilio message
        return {
            "event": "media",
            "streamSid": stream_id,
            "media": {
                "payload": converted_payload
            }
        }
    
    def create_mark_message(self, stream_id: str, mark_name: str = "responsePart") -> Dict[str, Any]:
        """
        Create a mark message for audio synchronization.
        
        Args:
            stream_id: Twilio stream identifier
            mark_name: Name of the mark
            
        Returns:
            Twilio mark message
        """
        self.buffer_manager.add_mark(mark_name)
        return {
            "event": "mark",
            "streamSid": stream_id,
            "mark": {"name": mark_name}
        }
    
    def create_clear_message(self, stream_id: str) -> Dict[str, Any]:
        """
        Create a clear message to clear audio buffer.
        
        Args:
            stream_id: Twilio stream identifier
            
        Returns:
            Twilio clear message
        """
        self.buffer_manager.clear_audio_buffer()
        return {
            "event": "clear",
            "streamSid": stream_id
        }
    
    def handle_mark_event(self) -> None:
        """Handle a mark event from Twilio."""
        removed_mark = self.buffer_manager.remove_mark()
        if Config.SHOW_TIMING_MATH and removed_mark:
            print(f"Processed mark: {removed_mark}")
    
    def calculate_interruption_timing(self) -> Optional[int]:
        """
        Calculate timing for audio interruption.
        Returns a value consistent with the audio we've passed: capped to
        (sent_ms - safety_margin) so truncation never exceeds the assistant's
        actual audio length and stays aligned with API's internal content.
        
        Returns:
            Elapsed time for truncation, or None if no response is tracked
        """
        elapsed = self.timing_manager.calculate_response_duration()
        if elapsed is None:
            return None
        sent_ms = self.timing_manager.response_audio_sent_ms or 0
        safety_ms = getattr(Config, "TRUNCATION_SAFETY_MS", 150)
        cap_ms = max(0, sent_ms - safety_ms) if sent_ms else 0
        return min(elapsed, cap_ms) if cap_ms else (elapsed if not sent_ms else 0)
    
    def should_handle_interruption(self) -> bool:
        """
        Determine if an interruption should be processed.
        
        Returns:
            True if interruption should be handled
        """
        return (self.timing_manager.last_item_id is not None and
                self.buffer_manager.has_pending_marks() and
                self.timing_manager.response_start_timestamp is not None)
    
    def reset_interruption_state(self) -> None:
        """Reset all interruption-related state."""
        self.timing_manager.reset_response_tracking()
        self.buffer_manager.clear_marks()
    
    def get_current_item_id(self) -> Optional[str]:
        """Get the ID of the currently tracked audio item."""
        return self.timing_manager.get_current_item_id()
    
    def _extract_twilio_payload(self, data: dict) -> Optional[str]:
        """Extract audio payload from Twilio data."""
        try:
            return data['media']['payload']
        except (KeyError, TypeError):
            return None
    
    def _extract_twilio_timestamp(self, data: dict) -> Optional[int]:
        """Extract timestamp from Twilio data."""
        try:
            return int(data['media']['timestamp'])
        except (KeyError, TypeError, ValueError):
            return None
