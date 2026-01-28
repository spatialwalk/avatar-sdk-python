# Avatar SDK Python

A Python SDK for connecting to avatar services via WebSocket, supporting audio streaming and receiving animation frames.

## Quick Start

```python
import asyncio
from datetime import datetime, timedelta, timezone
from avatarkit import new_avatar_session

async def main():
    # Create session
    session = new_avatar_session(
        api_key="your-api-key",
        app_id="your-app-id",
        console_endpoint_url="https://console.us-west.spatialwalk.cloud/v1/console",
        ingress_endpoint_url="https://api.us-west.spatialwalk.cloud/v2/driveningress",
        avatar_id="your-avatar-id",
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        transport_frames=lambda frame, last: print(f"Received frame: {len(frame)} bytes"),
        on_error=lambda err: print(f"Error: {err}"),
        on_close=lambda: print("Session closed")
    )

    # Initialize and connect
    await session.init()
    connection_id = await session.start()
    print(f"Connected: {connection_id}")

    # Send audio
    audio_data = b"..."  # Your PCM audio data
    request_id = await session.send_audio(audio_data, end=True)
    print(f"Sent audio: {request_id}")

    # Wait for frames...
    await asyncio.sleep(10)

    # Close
    await session.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Detailed Usage

### Session Configuration

The SDK provides two ways to configure a session:

#### Option 1: Using `new_avatar_session()` (Recommended)

```python
from avatarkit import new_avatar_session

session = new_avatar_session(
    avatar_id="avatar-123",
    api_key="your-api-key",
    app_id="your-app-id",
    # For web-style auth, set use_query_auth=True to put (appId, sessionKey)
    # in the websocket URL query params instead of headers.
    use_query_auth=False,
    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    console_endpoint_url="https://console.us-west.spatialwalk.cloud/v1/console",
    ingress_endpoint_url="https://api.us-west.spatialwalk.cloud/v2/driveningress",
    sample_rate=16000,  # Default: 16000 Hz
    transport_frames=on_frame_received,
    on_error=on_error,
    on_close=on_close
)
```

#### Option 2: Using Configuration Builder

```python
from avatarkit import SessionConfigBuilder, AvatarSession

config = (SessionConfigBuilder()
    .with_avatar_id("avatar-123")
    .with_api_key("your-api-key")
    .with_app_id("your-app-id")
    .with_console_endpoint_url("https://console.us-west.spatialwalk.cloud/v1/console")
    .with_ingress_endpoint_url("https://api.us-west.spatialwalk.cloud/v2/driveningress")
    .with_expire_at(datetime.now(timezone.utc) + timedelta(minutes=5))
    .with_transport_frames(on_frame_received)
    .build())

session = AvatarSession(config)
```

### Session Lifecycle

```python
# 1. Initialize (get session token)
await session.init()

# 2. Start WebSocket connection
connection_id = await session.start()

# 3. Send audio data
request_id = await session.send_audio(audio_bytes, end=True)

# 4. Receive frames via callback
# (automatically handled in background)

# 5. Close session
await session.close()
```

### Audio Format

The SDK currently supports **mono 16-bit PCM (s16le)** audio:

- Sample Rate: one of `[8000, 16000, 22050, 24000, 32000, 44100, 48000]`
- Channels: 1 (mono)
- Bit Depth: 16-bit
- Format: Raw PCM bytes

```python
# Example: Load PCM audio file
with open("audio.pcm", "rb") as f:
    audio_data = f.read()

# Send in chunks or all at once
await session.send_audio(audio_data, end=True)
```

### LiveKit Egress Mode

When configured with `livekit_egress`, audio and animation data are streamed to a LiveKit room via the egress service instead of being returned through the WebSocket connection.

```python
from avatarkit import new_avatar_session, LiveKitEgressConfig

session = new_avatar_session(
    avatar_id="avatar-123",
    api_key="your-api-key",
    app_id="your-app-id",
    console_endpoint_url="https://console.example.com/v1/console",
    ingress_endpoint_url="https://api.example.com/v2/driveningress",
    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    livekit_egress=LiveKitEgressConfig(
        url="wss://livekit.example.com",
        api_key="livekit-api-key",
        api_secret="livekit-api-secret",
        room_name="my-room",
        publisher_id="avatar-publisher",
    ),
)
```

When LiveKit egress is enabled:
- The server streams output to the specified LiveKit room
- The `transport_frames` callback will not be invoked
- Audio and animation data are published to the room under the specified publisher ID

#### Interrupt (LiveKit Egress Only)

The `interrupt()` method sends an interrupt signal to stop current audio processing. This is only available when using LiveKit egress mode.

```python
# Send audio
request_id = await session.send_audio(audio_data, end=True)

# Later, if you need to interrupt (e.g., user wants to stop playback)
interrupted_id = await session.interrupt()
print(f"Interrupted request: {interrupted_id}")
```

The interrupt uses the most recent request ID, even after `end=True` was sent. This allows interrupting requests that have finished sending audio but are still being processed by the server.

### Callbacks

#### Transport Frames Callback

Receives animation frames from the server:

```python
def on_frame_received(frame_data: bytes, is_last: bool):
    print(f"Received frame: {len(frame_data)} bytes")
    if is_last:
        print("This is the last frame")
    # Process frame_data (contains serialized Message protobuf)
```

#### Error Callback

Handles errors from the session:

```python
def on_error(error: Exception):
    print(f"Session error: {error}")
```

#### Close Callback

Called when the session closes:

```python
def on_close():
    print("Session has been closed")
```

## API Reference

### AvatarSession

Main class for managing avatar sessions.

#### Methods

- `async init()` - Initialize session and obtain token
- `async start() -> str` - Start WebSocket connection, returns connection ID
- `async send_audio(audio: bytes, end: bool = False) -> str` - Send audio data, returns request ID
- `async interrupt() -> str` - Interrupt current audio processing (LiveKit egress mode only), returns interrupted request ID
- `async close()` - Close the session and clean up resources
- `config -> SessionConfig` - Get session configuration (property)

### SessionConfig

Configuration dataclass for avatar sessions.

#### Fields

- `avatar_id: str` - Avatar identifier
- `api_key: str` - API key for authentication
- `app_id: str` - Application identifier
- `use_query_auth: bool` - Send websocket auth via query params (web) instead of headers (mobile)
- `expire_at: datetime` - Session expiration time
- `sample_rate: int` - Audio sample rate (default: 16000)
- `bitrate: int` - Audio bitrate (default: 0; PCM typically uses 0)
- `transport_frames: Callable[[bytes, bool], None]` - Frame callback
- `on_error: Callable[[Exception], None]` - Error callback
- `on_close: Callable[[], None]` - Close callback
- `console_endpoint_url: str` - Console API URL
- `ingress_endpoint_url: str` - Ingress WebSocket URL
- `livekit_egress: Optional[LiveKitEgressConfig]` - LiveKit egress configuration

### LiveKitEgressConfig

Configuration for streaming to a LiveKit room.

#### Fields

- `url: str` - LiveKit server URL (e.g., `wss://livekit.example.com`)
- `api_key: str` - LiveKit API key
- `api_secret: str` - LiveKit API secret
- `room_name: str` - LiveKit room name to join
- `publisher_id: str` - Publisher identity in the room

### SessionConfigBuilder

Builder for constructing SessionConfig with fluent interface.

#### Methods

All methods return `self` for chaining:

- `with_avatar_id(avatar_id: str)`
- `with_api_key(api_key: str)`
- `with_app_id(app_id: str)`
- `with_use_query_auth(use_query_auth: bool)`
- `with_expire_at(expire_at: datetime)`
- `with_sample_rate(sample_rate: int)`
- `with_bitrate(bitrate: int)`
- `with_transport_frames(handler: Callable)`
- `with_on_error(handler: Callable)`
- `with_on_close(handler: Callable)`
- `with_console_endpoint_url(url: str)`
- `with_ingress_endpoint_url(url: str)`
- `with_livekit_egress(config: LiveKitEgressConfig)`
- `build() -> SessionConfig` - Build the configuration

### Utility Functions

- `generate_log_id() -> str` - Generate unique log ID in format "YYYYMMDDHHMMSS_\<nanoid\>"

### Exceptions

- `SessionTokenError` - Raised when session token request fails

## Examples

See the [examples](./examples) directory for complete working examples:

- [single_audio_clip](./examples/single_audio_clip) - Basic usage with a single audio file
- [http_service](./examples/http_service) - Simple HTTP API that returns PCM audio (by sample rate) and generated animation Message binaries

## Protocol Buffers

The SDK uses Protocol Buffers for efficient serialization. The proto definitions are in `proto/message.proto`.

### Generating Proto Code

Proto code is generated using [buf](https://buf.build):

```bash
cd proto
buf generate
```

The generated Python code is placed in `src/avatarkit/proto/generated/`.

### Message Types

- `MESSAGE_CLIENT_CONFIGURE_SESSION` (1) - Client session negotiation parameters
- `MESSAGE_SERVER_CONFIRM_SESSION` (2) - Server confirms and returns `connection_id`
- `MESSAGE_CLIENT_AUDIO_INPUT` (3) - Client audio input
- `MESSAGE_SERVER_ERROR` (4) - Server-side error message
- `MESSAGE_SERVER_RESPONSE_ANIMATION` (5) - Server animation response (`end` indicates final)
- `MESSAGE_CLIENT_INTERRUPT` (7) - Client interrupt signal to stop processing

## Development

### Setup

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone <repository-url>
cd avatar-sdk-python
uv sync
```

## License

See [LICENSE](./LICENSE) for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
