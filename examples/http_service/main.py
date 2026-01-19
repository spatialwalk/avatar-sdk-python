"""
Example: HTTP Service

This example exposes a simple HTTP API that:
- Accepts a POST request with a desired sample rate
- Returns a PCM audio clip at that sample rate (loaded from audio_{rate}.pcm)
- Uses the SDK to make a real request to the avatar service and returns:
  - The audio bytes (base64 in JSON)
  - The list of base64-encoded protobuf `message.Message` binaries received from the service
    (typically `MESSAGE_SERVER_RESPONSE_ANIMATION`, with `end=true` on the final frame).

Notes:
- This is a *server-side* helper example. It does not connect to the websocket ingress.
- The SDK does not need to unmarshal animation payloads; consumers receive the original
  Message binary data via the callback in the websocket flow. Here we return those same
  binary protobuf messages over HTTP (base64 in JSON) for convenience/testing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from avatar_sdk_python import AvatarSDKError, SessionTokenError, new_avatar_session


_AUDIO_RE = re.compile(r"^audio_(?P<rate>\d+)\.pcm$")

_DEFAULT_SESSION_TTL_MINUTES = 2
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class AudioAsset:
    sample_rate: int
    path: Path


def _find_audio_assets(repo_root: Path) -> dict[int, AudioAsset]:
    assets: dict[int, AudioAsset] = {}
    for p in repo_root.iterdir():
        if not p.is_file():
            continue
        m = _AUDIO_RE.match(p.name)
        if not m:
            continue
        rate = int(m.group("rate"))
        assets[rate] = AudioAsset(sample_rate=rate, path=p)
    return assets


def _load_audio_bytes(asset: AudioAsset) -> bytes:
    return asset.path.read_bytes()


@dataclass
class _Collector:
    frames: list[bytes]
    last: bool
    error: Optional[Exception]
    done: Any  # asyncio.Event (kept untyped to avoid importing asyncio at module level)


def _make_collector():
    import asyncio

    return _Collector(frames=[], last=False, error=None, done=asyncio.Event())


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_generate(request: web.Request) -> web.Response:
    app = request.app
    assets: dict[int, AudioAsset] = app["audio_assets"]
    sdk_cfg: dict[str, str] = app["sdk_config"]

    try:
        body: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    sample_rate = body.get("sample_rate")
    if sample_rate is None:
        return web.json_response({"error": "missing_sample_rate"}, status=400)

    try:
        sample_rate_int = int(sample_rate)
    except Exception:
        return web.json_response({"error": "invalid_sample_rate"}, status=400)

    asset = assets.get(sample_rate_int)
    if asset is None:
        return web.json_response(
            {
                "error": "unsupported_sample_rate",
                "supported_sample_rates": sorted(assets.keys()),
            },
            status=404,
        )

    audio = _load_audio_bytes(asset)
    collector = _make_collector()

    def transport_frames(frame: bytes, last: bool) -> None:
        collector.frames.append(bytes(frame))
        if last:
            collector.last = True
            collector.done.set()

    def on_error(err: Exception) -> None:
        if collector.error is None:
            collector.error = err
        collector.done.set()

    def on_close() -> None:
        # If the session closes before we saw the final frame, treat it as error.
        if not collector.last and collector.error is None:
            collector.error = Exception("session_closed_before_final_frame")
        collector.done.set()

    session = new_avatar_session(
        api_key=sdk_cfg["api_key"],
        app_id=sdk_cfg["app_id"],
        console_endpoint_url=sdk_cfg["console_endpoint_url"],
        ingress_endpoint_url=sdk_cfg["ingress_endpoint_url"],
        avatar_id=sdk_cfg["avatar_id"],
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=_DEFAULT_SESSION_TTL_MINUTES),
        sample_rate=sample_rate_int,
        bitrate=0,
        transport_frames=transport_frames,
        on_error=on_error,
        on_close=on_close,
    )

    connection_id: Optional[str] = None
    req_id: Optional[str] = None
    try:
        await session.init()
        connection_id = await session.start()
        req_id = await session.send_audio(audio, end=True)

        import asyncio

        await asyncio.wait_for(collector.done.wait(), timeout=_DEFAULT_REQUEST_TIMEOUT_SECONDS)
        if collector.error:
            raise collector.error
    except SessionTokenError as e:
        return web.json_response({"error": "session_token_error", "message": str(e)}, status=502)
    except AvatarSDKError as e:
        return web.json_response(
            {"error": "sdk_error", "code": getattr(e.code, "value", str(e.code)), "message": e.message},
            status=502,
        )
    except Exception as e:
        return web.json_response({"error": "request_failed", "message": str(e)}, status=502)
    finally:
        try:
            await session.close()
        except Exception:
            pass

    return web.json_response(
        {
            "sample_rate": sample_rate_int,
            "audio_format": "pcm_s16le_mono",
            "audio_base64": base64.b64encode(audio).decode("utf-8"),
            "connection_id": connection_id,
            "req_id": req_id,
            "animation_messages_base64": [base64.b64encode(m).decode("utf-8") for m in collector.frames],
        }
    )


def create_app(*, repo_root: Path) -> web.Application:
    assets = _find_audio_assets(repo_root)
    if not assets:
        raise RuntimeError(f"No audio_{{rate}}.pcm files found in {repo_root}")

    api_key = os.getenv("AVATAR_API_KEY", "").strip()
    app_id = os.getenv("AVATAR_APP_ID", "").strip()
    console_endpoint_url = os.getenv("AVATAR_CONSOLE_ENDPOINT", "").strip()
    ingress_endpoint_url = os.getenv("AVATAR_INGRESS_ENDPOINT", "").strip()
    avatar_id = os.getenv("AVATAR_SESSION_AVATAR_ID", "").strip()
    missing = [
        name
        for name, val in {
            "AVATAR_API_KEY": api_key,
            "AVATAR_APP_ID": app_id,
            "AVATAR_CONSOLE_ENDPOINT": console_endpoint_url,
            "AVATAR_INGRESS_ENDPOINT": ingress_endpoint_url,
            "AVATAR_SESSION_AVATAR_ID": avatar_id,
        }.items()
        if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    app = web.Application()
    app["audio_assets"] = assets
    app["sdk_config"] = {
        "api_key": api_key,
        "app_id": app_id,
        "console_endpoint_url": console_endpoint_url,
        "ingress_endpoint_url": ingress_endpoint_url,
        "avatar_id": avatar_id,
    }
    app.add_routes(
        [
            web.get("/healthz", handle_health),
            web.post("/generate", handle_generate),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP service example for avatar-sdk-python")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    # repo root is 2 levels up from examples/http_service/
    repo_root = Path(__file__).resolve().parents[2]
    app = create_app(repo_root=repo_root)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()


