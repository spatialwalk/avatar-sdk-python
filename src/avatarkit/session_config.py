"""Configuration options for avatar sessions."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Union


class AudioFormat(str, Enum):
    """Audio input encoding negotiated for a session."""

    PCM_S16LE = "pcm_s16le"
    OGG_OPUS = "ogg_opus"


@dataclass
class OggOpusEncoderConfig:
    """Optional client-side Ogg Opus encoder settings."""

    frame_duration_ms: int = 20
    application: str = "audio"


@dataclass
class LiveKitEgressConfig:
    """
    Configuration for streaming to a LiveKit room.

    When set on a SessionConfig, audio and animation data are streamed to a LiveKit room
    via the egress service instead of being returned through the WebSocket connection.

    Attributes:
        url: LiveKit server URL (e.g., wss://livekit.example.com).
        api_key: LiveKit API key.
        api_secret: LiveKit API secret.
        room_name: LiveKit room name to join.
        publisher_id: Publisher identity in the room.
        extra_attributes: Additional key-value attributes for the LiveKit participant.
        idle_timeout: Idle timeout in seconds for egress connection auto-close. 0 means
            use server defaults.
    """

    url: str = ""
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    room_name: str = ""
    publisher_id: str = ""
    extra_attributes: dict[str, str] = field(default_factory=dict)
    idle_timeout: int = 0


@dataclass
class AgoraEgressConfig:
    """
    Configuration for streaming to an Agora channel.

    When set on a SessionConfig, audio and animation data are streamed to an Agora channel
    via the egress service instead of being returned through the WebSocket connection.

    Attributes:
        channel_name: Agora channel name to join.
        token: Agora token for authentication (optional for testing).
        uid: Publisher UID in the channel (0 for auto-assign).
        publisher_id: Publisher identity/name.
    """

    channel_name: str = ""
    token: str = field(default="", repr=False)
    uid: int = 0
    publisher_id: str = ""


@dataclass
class SessionConfig:
    """
    Configuration for an AvatarSession.

    Attributes:
        avatar_id: The avatar identifier for the session.
        api_key: The API key for authentication.
        app_id: The application identifier.
        use_query_auth: If true, send app/session credentials as URL query params (web-style
            auth). If false (default), send them as headers (mobile-style auth).
        expire_at: Expiration time for the session.
        sample_rate: Audio sample rate in Hz (default: 16000).
        bitrate: Audio bitrate (if applicable to the selected audio_format). For PCM this
            may be 0.
        audio_format: Session audio input format. PCM remains the default for backward
            compatibility. Use OGG_OPUS when streaming one continuous Ogg Opus stream per
            request ID.
        ogg_opus_encoder: Optional client-side encoder settings. When set together with
            ``audio_format=AudioFormat.OGG_OPUS``, ``send_audio()`` accepts raw PCM input
            and the SDK encodes it to continuous Ogg Opus before sending.
        on_encoded_audio: Optional callback invoked when internal Ogg Opus encoding
            finishes for a request. Receives ``(req_id, encoded_audio_bytes)``.
        transport_frames: Callback for receiving animation frames (frame_data, is_last).
        on_error: Callback for error handling.
        on_close: Callback invoked when session closes.
        console_endpoint_url: URL for the console API endpoint.
        ingress_endpoint_url: URL for the ingress websocket endpoint.
        livekit_egress: If set, enables LiveKit egress mode - audio and animation are
            streamed to a LiveKit room via the egress service.
        agora_egress: If set, enables Agora egress mode - audio and animation are
            streamed to an Agora channel via the egress service.
    """

    avatar_id: str = ""
    api_key: str = field(default="", repr=False)
    app_id: str = ""
    use_query_auth: bool = False
    expire_at: Optional[datetime] = None
    sample_rate: int = 16000
    bitrate: int = 0
    audio_format: AudioFormat = AudioFormat.PCM_S16LE
    ogg_opus_encoder: Optional[OggOpusEncoderConfig] = None
    on_encoded_audio: Optional[Callable[[str, bytes], None]] = None
    transport_frames: Callable[[bytes, bool], None] = field(
        default=lambda data, last: None
    )
    on_error: Callable[[Exception], None] = field(default=lambda err: None)
    on_close: Callable[[], None] = field(default=lambda: None)
    console_endpoint_url: str = ""
    ingress_endpoint_url: str = ""
    livekit_egress: Optional[LiveKitEgressConfig] = None
    agora_egress: Optional[AgoraEgressConfig] = None

    def __post_init__(self) -> None:
        self.audio_format = AudioFormat(self.audio_format)


class SessionConfigBuilder:
    """Builder for constructing SessionConfig with fluent interface."""

    def __init__(self):
        """Initialize a new SessionConfigBuilder with default values."""
        self._config = SessionConfig()

    def with_avatar_id(self, avatar_id: str) -> "SessionConfigBuilder":
        """Set the avatar identifier."""
        self._config.avatar_id = avatar_id
        return self

    def with_api_key(self, api_key: str) -> "SessionConfigBuilder":
        """Set the API key."""
        self._config.api_key = api_key
        return self

    def with_app_id(self, app_id: str) -> "SessionConfigBuilder":
        """Set the application identifier."""
        self._config.app_id = app_id
        return self

    def with_use_query_auth(self, use_query_auth: bool) -> "SessionConfigBuilder":
        """
        Choose whether websocket auth is sent via URL query params (web) or headers (mobile).
        """
        self._config.use_query_auth = use_query_auth
        return self

    def with_expire_at(self, expire_at: datetime) -> "SessionConfigBuilder":
        """Set the session expiration time."""
        self._config.expire_at = expire_at
        return self

    def with_sample_rate(self, sample_rate: int) -> "SessionConfigBuilder":
        """Set the audio sample rate in Hz."""
        self._config.sample_rate = sample_rate
        return self

    def with_bitrate(self, bitrate: int) -> "SessionConfigBuilder":
        """Set the audio bitrate (if applicable)."""
        self._config.bitrate = bitrate
        return self

    def with_audio_format(
        self, audio_format: Union[AudioFormat, str]
    ) -> "SessionConfigBuilder":
        """Set the session audio input format."""
        self._config.audio_format = AudioFormat(audio_format)
        return self

    def with_ogg_opus_encoder(
        self, config: Optional[OggOpusEncoderConfig] = None
    ) -> "SessionConfigBuilder":
        """Enable client-side PCM-to-Ogg-Opus encoding for OGG_OPUS sessions."""
        self._config.ogg_opus_encoder = config or OggOpusEncoderConfig()
        return self

    def with_on_encoded_audio(
        self, handler: Callable[[str, bytes], None]
    ) -> "SessionConfigBuilder":
        """Set the callback invoked when internal Ogg Opus encoding finishes."""
        self._config.on_encoded_audio = handler
        return self

    def with_transport_frames(
        self, handler: Callable[[bytes, bool], None]
    ) -> "SessionConfigBuilder":
        """Set the callback for receiving animation frames."""
        self._config.transport_frames = handler
        return self

    def with_on_error(
        self, handler: Callable[[Exception], None]
    ) -> "SessionConfigBuilder":
        """Set the error handler callback."""
        self._config.on_error = handler
        return self

    def with_on_close(self, handler: Callable[[], None]) -> "SessionConfigBuilder":
        """Set the close handler callback."""
        self._config.on_close = handler
        return self

    def with_console_endpoint_url(self, url: str) -> "SessionConfigBuilder":
        """Set the console endpoint URL."""
        self._config.console_endpoint_url = url
        return self

    def with_ingress_endpoint_url(self, url: str) -> "SessionConfigBuilder":
        """Set the ingress endpoint URL."""
        self._config.ingress_endpoint_url = url
        return self

    def with_livekit_egress(
        self, config: LiveKitEgressConfig
    ) -> "SessionConfigBuilder":
        """
        Enable LiveKit egress mode for the session.

        When set, audio and animation data are streamed to a LiveKit room via the egress
        service instead of being returned through the WebSocket connection.
        """
        self._config.livekit_egress = config
        return self

    def with_agora_egress(self, config: AgoraEgressConfig) -> "SessionConfigBuilder":
        """
        Enable Agora egress mode for the session.

        When set, audio and animation data are streamed to an Agora channel via the egress
        service instead of being returned through the WebSocket connection.
        """
        self._config.agora_egress = config
        return self

    def build(self) -> SessionConfig:
        """Build and return the configured SessionConfig."""
        return self._config
