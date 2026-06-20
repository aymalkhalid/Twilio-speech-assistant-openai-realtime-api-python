"""
Services package for the Speech Assistant application.

This package contains all service modules organized following Single Responsibility Principle:

- audio_service: Handles all audio processing operations
- twilio_service: Manages Twilio Voice API integration  
- openai_service: Handles OpenAI Realtime API operations
- connection_manager: Manages WebSocket connections

Each service is designed to be independent, testable, and reusable.
"""

from .audio_service import AudioService, AudioMetadata, AudioFormatConverter, AudioTimingManager, AudioBufferManager
from .twilio_service import TwilioService, TwilioAudioProcessor
from .openai_service import OpenAIService, OpenAIEventHandler, OpenAISessionManager, OpenAIConversationManager
from .connection_manager import WebSocketConnectionManager, ConnectionState

__all__ = [
    # Audio Service
    'AudioService',
    'AudioMetadata', 
    'AudioFormatConverter',
    'AudioTimingManager',
    'AudioBufferManager',
    
    # Twilio Service
    'TwilioService',
    'TwilioAudioProcessor',
    
    # OpenAI Service
    'OpenAIService',
    'OpenAIEventHandler', 
    'OpenAISessionManager',
    'OpenAIConversationManager',
    
    # Connection Management
    'WebSocketConnectionManager',
    'ConnectionState',
]
