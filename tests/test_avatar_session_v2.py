import asyncio
import unittest
from unittest.mock import patch

from avatar_sdk_python import new_avatar_session
from avatar_sdk_python.proto.generated import message_pb2


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
    def __init__(self, recv_messages: list[bytes] | None = None):
        self.sent: list[bytes] = []
        self._recv_q: asyncio.Queue[bytes] = asyncio.Queue()
        for m in (recv_messages or []):
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

        with patch("avatar_sdk_python.avatar_session.websockets.connect", new=fake_connect), patch(
            "avatar_sdk_python.avatar_session.asyncio.create_task", new=fake_create_task
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

        with patch("avatar_sdk_python.avatar_session.websockets.connect", new=fake_connect), patch(
            "avatar_sdk_python.avatar_session.asyncio.create_task", new=fake_create_task
        ):
            await session.start()

        self.assertIn("id=avatar-1", captured["url"])
        self.assertIn("appId=app-1", captured["url"])
        self.assertIn("sessionKey=tok-1", captured["url"])
        self.assertEqual(captured["headers"], {})

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
            return _FakeWebSocket(recv_messages=[_mk_server_error(code=400, message="bad params")])

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

        with patch("avatar_sdk_python.avatar_session.websockets.connect", new=fake_connect), patch(
            "avatar_sdk_python.avatar_session.asyncio.create_task", new=fake_create_task
        ):
            with self.assertRaises(ConnectionError):
                await session.start()


