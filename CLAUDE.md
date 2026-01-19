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

This is a Python SDK for WebSocket-based avatar services with audio streaming and animation frame reception.

### Core Components

- **`avatar_session.py`** - Main `AvatarSession` class managing WebSocket connections, audio streaming, and frame reception. Uses v2 protocol with HTTP-based session token acquisition followed by WebSocket handshake.

- **`session_config.py`** - `SessionConfig` dataclass and `SessionConfigBuilder` (fluent builder pattern) for session configuration.

- **`errors.py`** - `AvatarSDKError` exception with stable error codes (`AvatarSDKErrorCode` enum).

- **`proto/generated/`** - Auto-generated protobuf code from `proto/message.proto`. Message types: ClientConfigureSession, ServerConfirmSession, ClientAudioInput, ServerError, ServerResponseAnimation.

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
