# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Development Commands

```bash
# Install dependencies
uv sync

# Run tests
pytest

# Run a single test file
pytest tests/test_avatar_session_v2.py

# Run a specific test
pytest tests/test_avatar_session_v2.py::TestAvatarSessionV2::test_init_success

# Test across multiple Python versions locally
./test-local.sh all          # Test all Python versions (3.9-3.13) with all dependency combinations
./test-local.sh py39         # Test Python 3.9 only
./test-local.sh min          # Test minimum dependency versions on all Python versions
./test-local.sh latest       # Test latest dependency versions on all Python versions
./test-local.sh quick        # Quick test on current Python version

# Regenerate protobuf code (after modifying proto/message.proto)
cd proto && buf generate
```

## Architecture

This is a Python SDK for WebSocket-based avatar services with audio streaming and animation frame reception. Published as `avatarkit` on PyPI.

### Core Components

- **`avatar_session.py`** - Main `AvatarSession` class managing WebSocket connections, audio streaming, and frame reception. Uses v2 protocol with HTTP-based session token acquisition followed by WebSocket handshake. Exports `SessionTokenError` for token acquisition failures.

- **`session_config.py`** - `SessionConfig` dataclass, `LiveKitEgressConfig` dataclass, `AgoraEgressConfig` dataclass, and `SessionConfigBuilder` (fluent builder pattern) for session configuration.

- **`errors.py`** - `AvatarSDKError` exception with stable error codes (`AvatarSDKErrorCode` enum). Error codes: `sessionTokenExpired`, `sessionTokenInvalid`, `appIDUnrecognized`, `unknown`.

- **`logid.py`** - `generate_log_id()` utility for generating unique log IDs in format "YYYYMMDDHHMMSS_<nanoid>".

- **`proto/generated/`** - Auto-generated protobuf code from `proto/message.proto`. Message types: ClientConfigureSession, ServerConfirmSession, ClientAudioInput, ServerError, ServerResponseAnimation, ClientInterrupt.

### Session Flow

1. `new_avatar_session()` or `SessionConfigBuilder` creates configuration
2. `session.init()` - HTTP POST to console API for session token
3. `session.start()` - WebSocket connection + v2 handshake, returns connection_id
4. `session.send_audio()` - Send PCM audio via protobuf
5. Background read loop delivers animation frames via `transport_frames` callback
6. `session.close()` - Cleanup

### Audio Format

Mono 16-bit PCM (s16le) only. Supported sample rates: 8000, 16000, 22050, 24000, 32000, 44100, 48000 Hz.

### Authentication

Two modes controlled by `use_query_auth`:
- `False` (default): Headers-based auth (mobile pattern)
- `True`: Query params-based auth (web pattern)

### LiveKit Egress Mode

When configured with `livekit_egress`, audio and animation data are streamed to a LiveKit room via the egress service instead of being returned through the WebSocket connection. The egress configuration is sent via the `ClientConfigureSession` proto message.

To use LiveKit egress mode:
1. Configure the session with `livekit_egress=LiveKitEgressConfig(...)`
2. Provide LiveKit connection details: url, api_key, api_secret, room_name, and publisher_id
3. The server will create an egress connection and stream output to the LiveKit room
4. The `transport_frames` callback will not be invoked since data goes to LiveKit

```python
from avatarkit import new_avatar_session, LiveKitEgressConfig

session = new_avatar_session(
    livekit_egress=LiveKitEgressConfig(
        url="wss://livekit.example.com",
        api_key="your-api-key",
        api_secret="your-api-secret",
        room_name="room-name",
        publisher_id="publisher-id",
    ),
    # ... other options
)
```

### Agora Egress Mode

When configured with `agora_egress`, audio and animation data are streamed to an Agora channel via the egress service instead of being returned through the WebSocket connection. The egress configuration is sent via the `ClientConfigureSession` proto message.

To use Agora egress mode:
1. Configure the session with `agora_egress=AgoraEgressConfig(...)`
2. Provide Agora connection details: channel_name, token (optional for testing), uid (0 for auto-assign), and publisher_id
3. The server will create an egress connection and stream output to the Agora channel
4. The `transport_frames` callback will not be invoked since data goes to Agora

```python
from avatarkit import new_avatar_session, AgoraEgressConfig

session = new_avatar_session(
    agora_egress=AgoraEgressConfig(
        channel_name="channel-name",
        token="your-agora-token",  # optional for testing
        uid=0,  # 0 for auto-assign
        publisher_id="publisher-id",
    ),
    # ... other options
)
```

### Interrupt Functionality (Egress Mode Only)

The `interrupt()` method sends an interrupt signal to stop current audio processing. This is available when using egress mode (LiveKit or Agora).

```python
# Send some audio
req_id = await session.send_audio(audio_data, end=True)

# Interrupt if needed (e.g., user wants to stop)
interrupted_id = await session.interrupt()
```

The interrupt uses `last_req_id` which tracks the most recent request, even after `end=True` was sent. This allows interrupting requests that have finished sending audio but are still being processed.
