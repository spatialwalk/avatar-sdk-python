import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional, cast
from unittest.mock import patch

import aiohttp
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from avatarkit import (
    AvatarSDKError,
    AvatarSDKErrorCode,
    LiveKitEgressConfig,
    SessionTokenError,
    new_avatar_session,
)
from avatarkit.proto.generated import message_pb2


class _DummyTask:
    def __init__(self):
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def __await__(self):
        if False:  # pragma: no cover
            yield None
        return None


class _FakeWebSocket:
    def __init__(self, recv_messages: Optional[list[bytes]] = None):
        self.sent: list[bytes] = []
        self._recv_q: asyncio.Queue[bytes] = asyncio.Queue()
        for m in recv_messages or []:
            self._recv_q.put_nowait(m)
        self._iter_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def recv(self):
        return await self._recv_q.get()

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Block until cancelled by the session close().
        return await self._iter_q.get()


class _ClosingWebSocket:
    def __init__(self, exc: Exception):
        self._exc = exc
        self._closed = False

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc


class _FakeHTTPResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequestContext:
    def __init__(self, response: Optional[_FakeHTTPResponse] = None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClientSession:
    def __init__(self, response: Optional[_FakeHTTPResponse] = None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *_args, **_kwargs):
        return _FakeRequestContext(response=self._response, error=self._error)


def _mk_confirm(connection_id: str) -> bytes:
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_CONFIRM_SESSION
    m.server_confirm_session.connection_id = connection_id
    return m.SerializeToString()


def _mk_server_error(code: int = 123, message: str = "bad") -> bytes:
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_ERROR
    m.server_error.connection_id = "cid"
    m.server_error.req_id = "rid"
    m.server_error.code = code
    m.server_error.message = message
    return m.SerializeToString()


class TestAvatarSessionV2(unittest.IsolatedAsyncioTestCase):
    async def test_init_raises_structured_session_token_error(self):
        session = new_avatar_session(
            console_endpoint_url="https://console.example.com",
            api_key="api",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        body = (
            '{"errors":[{"id":"INVALID_ARGUMENT","status":400,'
            '"code":"INVALID_ARGUMENT","title":"Invalid Argument",'
            '"detail":"expire_at must be in the future"}]}'
        )

        with patch(
            "avatarkit.avatar_session.aiohttp.ClientSession",
            new=lambda: _FakeClientSession(_FakeHTTPResponse(400, body)),
        ):
            with self.assertRaises(SessionTokenError) as cm:
                await session.init()

        err = cm.exception
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.invalidRequest)
        self.assertEqual(err.phase, "session_token")
        self.assertEqual(err.http_status, 400)
        self.assertEqual(err.server_code, "INVALID_ARGUMENT")
        self.assertEqual(err.server_title, "Invalid Argument")
        self.assertEqual(err.server_detail, "expire_at must be in the future")
        self.assertIn("Failed to create session token (HTTP 400)", err.message)

    async def test_init_wraps_transport_errors(self):
        session = new_avatar_session(
            console_endpoint_url="https://console.example.com",
            api_key="api",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        with patch(
            "avatarkit.avatar_session.aiohttp.ClientSession",
            new=lambda: _FakeClientSession(
                error=aiohttp.ClientConnectionError("network down")
            ),
        ):
            with self.assertRaises(SessionTokenError) as cm:
                await session.init()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.connectionFailed)
        self.assertEqual(err.phase, "session_token")
        self.assertIn("Failed to create session token", err.message)

    async def test_start_header_auth_builds_url_and_headers_and_handshakes(self):
        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **_kwargs):
            captured["url"] = url
            captured["headers"] = dict(additional_headers or {})
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            # Don't run background loop in unit tests; also avoid "coroutine never awaited".
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            use_query_auth=False,
        )
        session._session_token = "tok-1"  # bypass init()

        with (
            patch("avatarkit.avatar_session.websockets.connect", new=fake_connect),
            patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            cid = await session.start()

        self.assertEqual(cid, "server-conn")
        self.assertIn("id=avatar-1", captured["url"])
        self.assertNotIn("appId=", captured["url"])
        self.assertEqual(
            captured["headers"],
            {"X-App-ID": "app-1", "X-Session-Key": "tok-1"},
        )

        # Verify first sent message is ClientConfigureSession
        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        self.assertIsNotNone(fake_ws)
        self.assertGreaterEqual(len(fake_ws.sent), 1)
        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])
        self.assertEqual(first.type, message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION)
        self.assertEqual(first.client_configure_session.sample_rate, 16000)

        await session.close()

    async def test_start_query_auth_builds_query_params(self):
        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **_kwargs):
            captured["url"] = url
            captured["headers"] = dict(additional_headers or {})
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            use_query_auth=True,
        )
        session._session_token = "tok-1"

        with (
            patch("avatarkit.avatar_session.websockets.connect", new=fake_connect),
            patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        self.assertIn("id=avatar-1", captured["url"])
        self.assertIn("appId=app-1", captured["url"])
        self.assertIn("sessionKey=tok-1", captured["url"])
        self.assertEqual(captured["headers"], {})

        await session.close()

    async def test_start_with_livekit_egress_sends_new_fields(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            livekit_egress=LiveKitEgressConfig(
                url="wss://livekit.example.com",
                api_key="lk-api-key",
                api_secret="lk-api-secret",
                room_name="lk-room",
                publisher_id="publisher-1",
                extra_attributes={"role": "avatar", "region": "us-west"},
                idle_timeout=120,
            ),
        )
        session._session_token = "tok-1"

        with (
            patch("avatarkit.avatar_session.websockets.connect", new=fake_connect),
            patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        self.assertIsNotNone(fake_ws)
        self.assertGreaterEqual(len(fake_ws.sent), 1)

        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])

        self.assertEqual(first.type, message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION)
        self.assertEqual(
            first.client_configure_session.egress_type, message_pb2.EGRESS_TYPE_LIVEKIT
        )
        self.assertEqual(
            first.client_configure_session.livekit_egress.extra_attributes,
            {"role": "avatar", "region": "us-west"},
        )
        self.assertEqual(
            first.client_configure_session.livekit_egress.idle_timeout, 120
        )

        await session.close()

    async def test_handle_server_response_animation_end_flag(self):
        got: list[tuple[bytes, bool]] = []

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            transport_frames=lambda data, last: got.append((bytes(data), bool(last))),
        )

        m = message_pb2.Message()
        m.type = message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION
        m.server_response_animation.connection_id = "cid"
        m.server_response_animation.req_id = "rid"
        m.server_response_animation.end = True

        payload = m.SerializeToString()
        await session._handle_binary_message(payload)

        self.assertEqual(len(got), 1)
        self.assertTrue(got[0][1])

    async def test_start_raises_on_server_error_during_handshake(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(
                recv_messages=[_mk_server_error(code=400, message="bad params")]
            )

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("avatarkit.avatar_session.websockets.connect", new=fake_connect),
            patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.serverError)
        self.assertEqual(err.phase, "websocket_handshake")
        self.assertEqual(err.server_code, "400")
        self.assertEqual(err.connection_id, "cid")
        self.assertEqual(err.req_id, "rid")
        self.assertEqual(err.server_detail, "bad params")

    async def test_start_parses_websocket_http_rejection_body(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=400,
                    reason_phrase="Bad Request",
                    headers=Headers(),
                    body=b'{"message":"Bad Request"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.sessionTokenInvalid)
        self.assertEqual(err.phase, "websocket_connect")
        self.assertEqual(err.http_status, 400)
        self.assertEqual(err.server_detail, "Bad Request")
        self.assertEqual(err.raw_body, '{"message":"Bad Request"}\n')
        self.assertIn(
            "WebSocket connection rejected (HTTP 400): Bad Request", err.message
        )

    async def test_start_maps_avatar_not_found_http_rejection(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=404,
                    reason_phrase="Not Found",
                    headers=Headers(),
                    body=b'{"message":"Avatar not found: avatar-1"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.avatarNotFound)
        self.assertEqual(err.http_status, 404)
        self.assertEqual(err.server_detail, "Avatar not found: avatar-1")

    async def test_start_maps_billing_required_http_rejection(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=402,
                    reason_phrase="Payment Required",
                    headers=Headers(),
                    body=b'{"message":"session denied: credits exhausted"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("avatarkit.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.creditsExhausted)
        self.assertEqual(err.http_status, 402)
        self.assertIsNotNone(err.server_detail)
        self.assertIn("credits exhausted", cast(str, err.server_detail))

    async def test_runtime_server_error_callback_receives_avatar_sdk_error(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )

        await session._handle_binary_message(
            _mk_server_error(code=321, message="server boom")
        )

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.serverError)
        self.assertEqual(err.phase, "websocket_runtime")
        self.assertEqual(err.server_code, "321")
        self.assertEqual(err.connection_id, "cid")
        self.assertEqual(err.req_id, "rid")
        self.assertEqual(err.server_detail, "server boom")

    async def test_runtime_billing_error_code_is_mapped(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )

        await session._handle_binary_message(
            _mk_server_error(code=4001, message="Credits exhausted")
        )

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.creditsExhausted)
        self.assertEqual(err.server_code, "4001")

    async def test_handshake_server_error_maps_business_code_from_message(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(
                recv_messages=[
                    _mk_server_error(
                        code=0,
                        message="unsupported sample rate: 12345",
                    )
                ]
            )

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("avatarkit.avatar_session.websockets.connect", new=fake_connect),
            patch("avatarkit.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.unsupportedSampleRate)
        self.assertEqual(err.phase, "websocket_handshake")

    async def test_read_loop_reports_unexpected_connection_close(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )
        session._connection = _ClosingWebSocket(
            ConnectionClosedError(Close(1011, "server exploded"), None)
        )

        await session._read_loop()

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.connectionClosed)
        self.assertEqual(err.phase, "websocket_runtime")
        self.assertEqual(err.close_code, 1011)
        self.assertEqual(err.close_reason, "server exploded")
