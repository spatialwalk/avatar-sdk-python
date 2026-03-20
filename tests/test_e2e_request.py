import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from avatarkit import (
    AudioFormat,
    OggOpusEncoderConfig,
    SessionTokenError,
    new_avatar_session,
)
from avatarkit.proto.generated import message_pb2


def _require_env(*names: str) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise unittest.SkipTest("Missing required e2e env vars: " + ", ".join(missing))
    return values


class _AnimationCollector:
    def __init__(self):
        self.frames: list[tuple[message_pb2.Message, bool]] = []
        self.last = False
        self.error: Exception | None = None
        self._done = asyncio.Event()

    def on_frame(self, payload: bytes, is_last: bool) -> None:
        envelope = message_pb2.Message()
        envelope.ParseFromString(payload)
        print(f"animation frame payload length: {len(payload)} bytes", flush=True)
        self.frames.append((envelope, is_last))
        if is_last:
            self.last = True
            self.finish(None)

    def on_error(self, error: Exception) -> None:
        self.finish(error)

    def on_close(self) -> None:
        if not self.last and not self.error:
            self.finish(Exception("Session closed before final animation frame"))
        else:
            self.finish(None)

    def finish(self, error: Exception | None) -> None:
        if error is not None and self.error is None:
            self.error = error
        self._done.set()

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Timed out waiting for animation frames") from exc

        if self.error is not None:
            raise self.error


@unittest.skipUnless(
    os.getenv("AVATARKIT_RUN_E2E") == "1",
    "Set AVATARKIT_RUN_E2E=1 to run end-to-end network tests",
)
class TestE2ERequest(unittest.IsolatedAsyncioTestCase):
    async def test_send_audio_receives_animation_frames(self):
        env = _require_env(
            "AVATARKIT_E2E_API_KEY",
            "AVATARKIT_E2E_APP_ID",
            "AVATARKIT_E2E_CONSOLE_ENDPOINT",
            "AVATARKIT_E2E_INGRESS_ENDPOINT",
            "AVATARKIT_E2E_AVATAR_ID",
        )

        audio_format = AudioFormat(
            os.getenv("AVATARKIT_E2E_AUDIO_FORMAT", AudioFormat.PCM_S16LE.value)
            .strip()
            .lower()
        )
        use_internal_ogg_opus_encoder = _env_flag(
            "AVATARKIT_E2E_USE_INTERNAL_OGG_OPUS_ENCODER"
        )
        sample_rate = int(
            os.getenv(
                "AVATARKIT_E2E_SAMPLE_RATE",
                (
                    "16000"
                    if audio_format == AudioFormat.OGG_OPUS
                    and use_internal_ogg_opus_encoder
                    else "24000"
                    if audio_format == AudioFormat.OGG_OPUS
                    else "16000"
                ),
            )
        )
        bitrate = int(os.getenv("AVATARKIT_E2E_BITRATE", "32000"))
        timeout_seconds = float(os.getenv("AVATARKIT_E2E_TIMEOUT_SECONDS", "45"))
        chunk_size = int(os.getenv("AVATARKIT_E2E_CHUNK_SIZE", "4096"))
        audio_path = Path(
            os.getenv(
                "AVATARKIT_E2E_AUDIO_PATH",
                "audio_16000.pcm"
                if audio_format == AudioFormat.PCM_S16LE
                or use_internal_ogg_opus_encoder
                else "audio.ogg",
            )
        )
        if not audio_path.is_absolute():
            audio_path = Path(__file__).resolve().parents[1] / audio_path

        if not audio_path.exists():
            raise unittest.SkipTest(f"E2E audio file not found: {audio_path}")

        audio_bytes = audio_path.read_bytes()
        collector = _AnimationCollector()
        session = new_avatar_session(
            api_key=env["AVATARKIT_E2E_API_KEY"],
            app_id=env["AVATARKIT_E2E_APP_ID"],
            console_endpoint_url=env["AVATARKIT_E2E_CONSOLE_ENDPOINT"],
            ingress_endpoint_url=env["AVATARKIT_E2E_INGRESS_ENDPOINT"],
            avatar_id=env["AVATARKIT_E2E_AVATAR_ID"],
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            sample_rate=sample_rate,
            bitrate=bitrate,
            audio_format=audio_format,
            ogg_opus_encoder=(
                OggOpusEncoderConfig()
                if audio_format == AudioFormat.OGG_OPUS
                and use_internal_ogg_opus_encoder
                else None
            ),
            transport_frames=collector.on_frame,
            on_error=collector.on_error,
            on_close=collector.on_close,
        )

        try:
            await session.init()
            await session.start()
        except SessionTokenError as exc:
            raise AssertionError(
                "Expected valid e2e credentials, but session token creation failed"
            ) from exc

        try:
            req_id = await self._send_audio_for_format(
                session,
                audio_bytes,
                audio_format,
                chunk_size,
                use_internal_ogg_opus_encoder,
            )
            await collector.wait(timeout=timeout_seconds)
        finally:
            await session.close()

        self.assertGreater(len(collector.frames), 0)
        self.assertTrue(collector.last)

        last_message, last_flag = collector.frames[-1]
        self.assertTrue(last_flag)
        self.assertEqual(
            last_message.type, message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION
        )
        self.assertEqual(last_message.server_response_animation.req_id, req_id)
        self.assertTrue(last_message.server_response_animation.end)

    async def _send_audio_for_format(
        self,
        session,
        audio_bytes: bytes,
        audio_format: AudioFormat,
        chunk_size: int,
        use_internal_ogg_opus_encoder: bool,
    ) -> str:
        if audio_format == AudioFormat.OGG_OPUS:
            for offset in range(0, len(audio_bytes), chunk_size):
                await session.send_audio(
                    audio_bytes[offset : offset + chunk_size], end=False
                )

            return await session.send_audio(b"", end=True)

        return await session.send_audio(audio_bytes, end=True)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}
