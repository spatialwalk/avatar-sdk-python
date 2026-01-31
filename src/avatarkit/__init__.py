"""
Avatar SDK Python - WebSocket SDK for Avatar Services

This package provides a Python SDK for connecting to avatar services via WebSocket,
supporting audio streaming and receiving animation frames.
"""

from .avatar_session import AvatarSession, SessionTokenError, new_avatar_session
from .errors import AvatarSDKError, AvatarSDKErrorCode
from .session_config import (
    SessionConfig,
    SessionConfigBuilder,
    LiveKitEgressConfig,
    AgoraEgressConfig,
)
from .logid import generate_log_id

__version__ = "0.1.0"

__all__ = [
    "AvatarSession",
    "SessionTokenError",
    "AvatarSDKError",
    "AvatarSDKErrorCode",
    "new_avatar_session",
    "SessionConfig",
    "SessionConfigBuilder",
    "LiveKitEgressConfig",
    "AgoraEgressConfig",
    "generate_log_id",
]
