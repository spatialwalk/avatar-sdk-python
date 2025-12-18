"""
Example: Single Audio Clip

This example demonstrates how to:
1. Initialize an avatar session
2. Connect to the avatar service
3. Send audio data
4. Receive animation frames
5. Properly close the session
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from avatar_sdk_python import new_avatar_session, SessionTokenError


# Configuration
AUDIO_FILE_PATH = "../../audio.pcm"
REQUEST_TIMEOUT = 45  # seconds
SESSION_TTL = 2  # minutes


class AnimationCollector:
    """Collects animation frames from the avatar session."""
    
    def __init__(self):
        self.frames: List[bytes] = []
        self.last = False
        self.error: Exception | None = None
        self._done = asyncio.Event()
    
    def transport_frame(self, data: bytes, last: bool):
        """Callback for receiving animation frames."""
        frame_copy = bytes(data)
        self.frames.append(frame_copy)
        if last:
            self.last = True
            self.finish(None)
    
    def on_error(self, error: Exception):
        """Callback for handling errors."""
        if error:
            self.finish(Exception(f"Avatar session error: {error}"))
    
    def on_close(self):
        """Callback for handling session close."""
        if not self.last:
            self.finish(Exception("Avatar session closed before final animation frame"))
        else:
            self.finish(None)
    
    def finish(self, error: Exception | None):
        """Mark collection as finished."""
        if error and not self.error:
            self.error = error
        self._done.set()
    
    async def wait(self, timeout: float | None = None):
        """Wait for collection to complete."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError("Timed out waiting for animation frames") from e
        
        if self.error:
            raise self.error
    
    def get_frames_copy(self) -> List[bytes]:
        """Get a copy of collected frames."""
        return [bytes(frame) for frame in self.frames]


async def main():
    """Main entry point for the example."""
    # Load configuration from environment
    config = load_config()
    
    # Load audio file
    audio = load_audio(AUDIO_FILE_PATH)
    print(f"Loaded audio file: {len(audio)} bytes")
    
    # Create animation collector
    collector = AnimationCollector()
    
    # Create avatar session
    session = new_avatar_session(
        api_key=config["api_key"],
        app_id=config["app_id"],
        use_query_auth=config["use_query_auth"],
        console_endpoint_url=config["console_url"],
        ingress_endpoint_url=config["ingress_url"],
        avatar_id=config["avatar_id"],
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=SESSION_TTL),
        transport_frames=collector.transport_frame,
        on_error=collector.on_error,
        on_close=collector.on_close,
    )
    
    try:
        # Initialize session (get token)
        print("Initializing session...")
        await session.init()
        print("Session initialized")
        
        # Start WebSocket connection
        print("Starting WebSocket connection...")
        connection_id = await session.start()
        print(f"Connected with connection ID: {connection_id}")
        
        # Send audio
        print("Sending audio...")
        request_id = await session.send_audio(audio, end=True)
        print(f"Sent audio request: {request_id}")
        
        # Wait for animation frames
        print("Waiting for animation frames...")
        await collector.wait(timeout=REQUEST_TIMEOUT)
        
        # Get results
        animations = collector.get_frames_copy()
        print(f"Received {len(animations)} animation frames")
        
        # Create response (similar to the Go example)
        response = {
            "audio": list(audio[:100]),  # Just first 100 bytes for demo
            "animations_count": len(animations),
            "animations_sizes": [len(anim) for anim in animations]
        }
        
        print("\nResponse summary:")
        print(json.dumps(response, indent=2))
        
    except SessionTokenError as e:
        print(f"Session token error: {e}")
        return 1
    except TimeoutError as e:
        print(f"Timeout error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        # Close session
        print("\nClosing session...")
        await session.close()
        print("Session closed")
    
    return 0


def load_config() -> dict:
    """Load configuration from environment variables."""
    api_key = os.getenv("AVATAR_API_KEY", "").strip()
    app_id = os.getenv("AVATAR_APP_ID", "").strip()
    use_query_auth = os.getenv("AVATAR_USE_QUERY_AUTH", "").strip().lower() in ("1", "true", "yes", "y", "on")
    console_url = os.getenv("AVATAR_CONSOLE_ENDPOINT", "").strip()
    ingress_url = os.getenv("AVATAR_INGRESS_ENDPOINT", "").strip()
    avatar_id = os.getenv("AVATAR_SESSION_AVATAR_ID", "").strip()
    
    missing = []
    if not api_key:
        missing.append("AVATAR_API_KEY")
    if not app_id:
        missing.append("AVATAR_APP_ID")
    if not console_url:
        missing.append("AVATAR_CONSOLE_ENDPOINT")
    if not ingress_url:
        missing.append("AVATAR_INGRESS_ENDPOINT")
    if not avatar_id:
        missing.append("AVATAR_SESSION_AVATAR_ID")
    
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    return {
        "api_key": api_key,
        "app_id": app_id,
        "use_query_auth": use_query_auth,
        "console_url": console_url,
        "ingress_url": ingress_url,
        "avatar_id": avatar_id,
    }


def load_audio(path: str) -> bytes:
    """Load audio file from disk."""
    audio_path = Path(__file__).parent / path
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    with open(audio_path, "rb") as f:
        return f.read()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
