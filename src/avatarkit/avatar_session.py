"""Main AvatarSession implementation for managing avatar websocket sessions."""

import asyncio
import inspect
import json
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp
import websockets
from websockets.client import WebSocketClientProtocol

from .errors import AvatarSDKError, AvatarSDKErrorCode
from .logid import generate_log_id
from .proto.generated import message_pb2
from .session_config import SessionConfig, SessionConfigBuilder

SESSION_TOKEN_PATH = "/session-tokens"
INGRESS_WEBSOCKET_PATH = "/websocket"


class SessionTokenError(Exception):
    """Raised when session token request fails."""

    pass


class AvatarSession:
    """
    Manages an active avatar session with websocket communication.

    The session handles:
    - Session token acquisition from console API
    - WebSocket connection to ingress endpoint
    - Audio streaming to the server
    - Receiving animation frames from the server
    """

    def __init__(self, config: SessionConfig):
        """
        Initialize a new AvatarSession with the provided configuration.

        Args:
            config: SessionConfig instance with session parameters.
        """
        self._config = config
        self._session_token: Optional[str] = None
        self._connection: Optional[WebSocketClientProtocol] = None
        self._current_req_id: Optional[str] = None
        self._last_req_id: Optional[str] = (
            None  # tracks most recent request for interrupt
        )
        self._read_task: Optional[asyncio.Task] = None
        self._connection_id: Optional[str] = None

    @property
    def config(self) -> SessionConfig:
        """Get a copy of the session configuration."""
        return self._config

    async def init(self) -> None:
        """
        Exchange configuration credentials for a session token from the console API.

        Raises:
            ValueError: If configuration is missing required fields.
            SessionTokenError: If token request fails.
        """
        if not self._config.api_key:
            raise ValueError("Missing API key")
        if not self._config.console_endpoint_url:
            raise ValueError("Missing console endpoint URL")
        if not self._config.expire_at:
            raise ValueError("Missing expireAt")

        endpoint = self._config.console_endpoint_url.rstrip("/") + SESSION_TOKEN_PATH

        payload = {"expireAt": int(self._config.expire_at.timestamp())}

        headers = {
            "X-Api-Key": self._config.api_key,
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint, json=payload, headers=headers
            ) as response:
                response_text = await response.text()

                if response.status != 200:
                    try:
                        error_data = json.loads(response_text)
                        error_msg = self._format_session_token_error(
                            response.status, error_data
                        )
                    except json.JSONDecodeError:
                        error_msg = f"Request failed with status {response.status}"
                    raise SessionTokenError(error_msg)

                try:
                    response_data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    raise SessionTokenError(f"Failed to decode response: {e}")

                errors = response_data.get("errors", [])
                if errors:
                    error_msg = self._format_session_token_error(
                        response.status, response_data
                    )
                    raise SessionTokenError(error_msg)

                session_token = response_data.get("sessionToken")
                if not session_token:
                    raise SessionTokenError("Empty session token in response")

                self._session_token = session_token

    async def start(self) -> str:
        """
        Establish WebSocket connection to the ingress endpoint.

        Returns:
            Connection ID for tracking this session.

        Raises:
            ValueError: If configuration is invalid or session not initialized.
            ConnectionError: If WebSocket connection fails.
        """
        if self._connection is not None:
            raise ValueError("Session already started")
        if not self._session_token:
            raise ValueError("Session not initialized")
        if not self._config.ingress_endpoint_url:
            raise ValueError("Missing ingress endpoint URL")
        if not self._config.avatar_id:
            raise ValueError("Missing avatar ID")
        if not self._config.app_id:
            raise ValueError("Missing app ID")

        endpoint = (
            self._config.ingress_endpoint_url.rstrip("/") + INGRESS_WEBSOCKET_PATH
        )

        # Parse URL and convert to WebSocket scheme
        parsed = urlparse(endpoint)
        scheme = parsed.scheme.lower()

        if scheme == "http":
            ws_scheme = "ws"
        elif scheme == "https":
            ws_scheme = "wss"
        elif scheme in ("ws", "wss"):
            ws_scheme = scheme
        elif not scheme:
            raise ValueError("Ingress endpoint scheme missing")
        else:
            raise ValueError(f"Unsupported scheme: {scheme}")

        # Add avatar ID to query parameters
        query_params = parse_qs(parsed.query)
        query_params["id"] = [self._config.avatar_id]

        # v2 auth: mobile uses headers; web uses query params.
        session_key = self._session_token
        headers: dict[str, str] = {}
        if self._config.use_query_auth:
            query_params["appId"] = [self._config.app_id]
            query_params["sessionKey"] = [session_key]
        else:
            headers = {
                "X-App-ID": self._config.app_id,
                "X-Session-Key": session_key,
            }

        new_query = urlencode(query_params, doseq=True)

        ws_url = urlunparse(
            (
                ws_scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

        try:
            # websockets renamed `extra_headers` -> `additional_headers` in newer releases.
            # If we pass the wrong kwarg, it may get forwarded to asyncio's
            # BaseEventLoop.create_connection(), which then raises:
            #   "... got an unexpected keyword argument 'extra_headers'"
            connect_sig = inspect.signature(websockets.connect)
            if "additional_headers" in connect_sig.parameters:
                self._connection = await websockets.connect(
                    ws_url, additional_headers=headers
                )
            elif "extra_headers" in connect_sig.parameters:
                self._connection = await websockets.connect(
                    ws_url, extra_headers=headers
                )
            else:
                # Fallback: some variants accept `headers=...`
                self._connection = await websockets.connect(ws_url, headers=headers)  # type: ignore[call-arg]
        except Exception as e:
            code = self._map_ws_connect_error_to_code(e)
            if code is not None:
                raise AvatarSDKError(
                    code=code,
                    message=f"WebSocket auth failed (HTTP {getattr(e, 'status_code', getattr(e, 'status', 'unknown'))})",
                ) from e
            raise ConnectionError(f"Failed to connect to websocket: {e}") from e

        # v2 handshake:
        # 1) client sends ClientConfigureSession
        # 2) server responds with ServerConfirmSession (connection_id) OR ServerError
        await self._send_client_configure_session()
        server_connection_id = await self._await_server_confirm_session()
        self._connection_id = server_connection_id

        # Start read loop in background
        self._read_task = asyncio.create_task(self._read_loop())

        return server_connection_id

    async def _send_client_configure_session(self) -> None:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        # Validate that only one egress mode is configured
        if (
            self._config.livekit_egress is not None
            and self._config.agora_egress is not None
        ):
            raise ValueError(
                "Cannot configure both livekit_egress and agora_egress at the same time"
            )

        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION
        msg.client_configure_session.sample_rate = int(self._config.sample_rate)
        msg.client_configure_session.bitrate = int(self._config.bitrate)
        msg.client_configure_session.audio_format = message_pb2.AUDIO_FORMAT_PCM_S16LE
        msg.client_configure_session.transport_compression = (
            message_pb2.TRANSPORT_COMPRESSION_NONE
        )

        # Add LiveKit egress configuration if provided
        if self._config.livekit_egress is not None:
            msg.client_configure_session.egress_type = message_pb2.EGRESS_TYPE_LIVEKIT
            msg.client_configure_session.livekit_egress.url = (
                self._config.livekit_egress.url
            )
            msg.client_configure_session.livekit_egress.api_key = (
                self._config.livekit_egress.api_key
            )
            msg.client_configure_session.livekit_egress.api_secret = (
                self._config.livekit_egress.api_secret
            )
            msg.client_configure_session.livekit_egress.room_name = (
                self._config.livekit_egress.room_name
            )
            msg.client_configure_session.livekit_egress.publisher_id = (
                self._config.livekit_egress.publisher_id
            )

        # Add Agora egress configuration if provided
        if self._config.agora_egress is not None:
            msg.client_configure_session.egress_type = message_pb2.EGRESS_TYPE_AGORA
            msg.client_configure_session.agora_egress.channel_name = (
                self._config.agora_egress.channel_name
            )
            msg.client_configure_session.agora_egress.token = (
                self._config.agora_egress.token
            )
            msg.client_configure_session.agora_egress.uid = (
                self._config.agora_egress.uid
            )
            msg.client_configure_session.agora_egress.publisher_id = (
                self._config.agora_egress.publisher_id
            )

        await self._connection.send(msg.SerializeToString())

    async def _await_server_confirm_session(self) -> str:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        try:
            raw = await self._connection.recv()
        except Exception as e:
            raise ConnectionError(f"Failed during websocket handshake: {e}") from e

        if not isinstance(raw, (bytes, bytearray)):
            raise ConnectionError(
                "Failed during websocket handshake: expected binary protobuf message"
            )

        envelope = message_pb2.Message()
        try:
            envelope.ParseFromString(bytes(raw))
        except Exception as e:
            raise ConnectionError(
                f"Failed during websocket handshake: invalid protobuf payload ({e})"
            ) from e

        if envelope.type == message_pb2.MESSAGE_SERVER_CONFIRM_SESSION:
            cid = envelope.server_confirm_session.connection_id
            if not cid:
                raise ConnectionError(
                    "Handshake succeeded but server_confirm_session.connection_id is empty"
                )
            return cid

        if envelope.type == message_pb2.MESSAGE_SERVER_ERROR:
            err = envelope.server_error
            raise ConnectionError(
                f"ServerError during handshake (connection_id={err.connection_id}, req_id={err.req_id}, code={err.code}): {err.message}"
            )

        raise ConnectionError(
            f"Unexpected message during handshake: type={envelope.type}"
        )

    async def send_audio(self, audio: bytes, end: bool = False) -> str:
        """
        Send audio data to the server.

        Currently supports 16kHz mono 16-bit PCM audio only.

        Args:
            audio: Raw audio bytes to send.
            end: Whether this is the last audio chunk for the current request.

        Returns:
            Request ID for tracking this audio request.

        Raises:
            ValueError: If connection is not established.
        """
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        # Generate or reuse request ID
        if not self._current_req_id:
            self._current_req_id = generate_log_id()
            self._last_req_id = self._current_req_id

        req_id = self._current_req_id

        # Create protobuf message
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_AUDIO_INPUT
        msg.client_audio_input.req_id = req_id
        msg.client_audio_input.audio = audio
        msg.client_audio_input.end = end

        # Serialize and send
        data = msg.SerializeToString()
        await self._connection.send(data)

        if end:
            self._current_req_id = None

        return req_id

    async def interrupt(self) -> str:
        """
        Send an interrupt signal to stop the current audio processing.

        Returns:
            The request ID that was interrupted.

        Raises:
            ValueError: If connection is not established or no request to interrupt.
        """
        if self._connection is None:
            raise ValueError("interrupt: websocket connection is not established")

        # Use last_req_id which tracks the most recent request, even after end=True
        req_id = self._last_req_id
        if not req_id:
            raise ValueError("interrupt: no request to interrupt")

        # Create protobuf message
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_INTERRUPT
        msg.client_interrupt.req_id = req_id

        # Serialize and send
        data = msg.SerializeToString()
        await self._connection.send(data)

        # Clear current request ID so next send_audio creates a new one
        self._current_req_id = None

        return req_id

    async def close(self) -> None:
        """
        Close the WebSocket connection and clean up resources.
        """
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
            finally:
                self._connection = None

        if self._read_task is not None:
            # If we're calling close() from inside the read task itself (e.g. the
            # read loop's finally block), don't cancel/await ourselves.
            if asyncio.current_task() is not self._read_task:
                self._read_task.cancel()
                try:
                    await self._read_task
                except asyncio.CancelledError:
                    pass
            self._read_task = None

        # Call close callback
        if self._config.on_close:
            try:
                self._config.on_close()
            except Exception:
                # Don't let callback errors propagate
                pass

    async def _read_loop(self) -> None:
        """Background task that reads messages from the WebSocket."""
        try:
            async for message in self._connection:
                if isinstance(message, bytes):
                    await self._handle_binary_message(message)
        except websockets.exceptions.ConnectionClosed:
            # Normal closure
            pass
        except asyncio.CancelledError:
            # Task was cancelled
            raise
        except Exception as e:
            # Report error through callback
            if self._config.on_error:
                try:
                    self._config.on_error(Exception(f"Read loop error: {e}"))
                except Exception:
                    pass
        finally:
            # Ensure connection is closed
            await self.close()

    async def _handle_binary_message(self, payload: bytes) -> None:
        """Handle a binary message received from the server."""
        try:
            envelope = message_pb2.Message()
            envelope.ParseFromString(payload)
        except Exception as e:
            if self._config.on_error:
                try:
                    self._config.on_error(Exception(f"Failed to decode message: {e}"))
                except Exception:
                    pass
            return

        if envelope.type == message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION:
            if self._config.transport_frames:
                # Make a copy of the payload
                frame = bytes(payload)

                is_last = bool(envelope.server_response_animation.end)
                try:
                    self._config.transport_frames(frame, is_last)
                except Exception:
                    pass

        elif envelope.type == message_pb2.MESSAGE_SERVER_ERROR:
            if self._config.on_error:
                err = envelope.server_error
                error_msg = (
                    f"Avatar session error (connection_id={err.connection_id}, req_id={err.req_id}, "
                    f"code={err.code}): {err.message}"
                )
                try:
                    self._config.on_error(Exception(error_msg))
                except Exception:
                    pass

    @staticmethod
    def _map_ws_connect_error_to_code(exc: Exception) -> Optional[AvatarSDKErrorCode]:
        """
        Map websocket HTTP upgrade failures to stable SDK error codes.

        v2 spec mapping:
        - 401 -> sessionTokenExpired
        - 400 -> sessionTokenInvalid
        - 404 -> appIDUnrecognized
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "status", None)
        if status is None and hasattr(exc, "response"):
            status = getattr(getattr(exc, "response"), "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response"), "status", None)

        try:
            status_int = int(status) if status is not None else None
        except Exception:
            status_int = None

        if status_int == 401:
            return AvatarSDKErrorCode.sessionTokenExpired
        if status_int == 400:
            return AvatarSDKErrorCode.sessionTokenInvalid
        if status_int == 404:
            return AvatarSDKErrorCode.appIDUnrecognized
        return None

    @staticmethod
    def _format_session_token_error(status: int, response_data: dict) -> str:
        """Format session token error response into readable message."""
        errors = response_data.get("errors", [])
        if not errors:
            return f"Unknown error with status {status}"

        err = errors[0]
        return (
            f"Error {err.get('status', status)} ({err.get('code', 'unknown')}): "
            f"{err.get('title', 'Error')} - {err.get('detail', 'No details')}"
        )


def new_avatar_session(**kwargs) -> AvatarSession:
    """
    Create a new AvatarSession with the provided configuration options.

    Args:
        **kwargs: Configuration parameters matching SessionConfig fields.

    Returns:
        A new AvatarSession instance.

    Example:
        ```python
        session = new_avatar_session(
            avatar_id="my-avatar",
            api_key="my-api-key",
            console_endpoint_url="https://console.example.com",
            ingress_endpoint_url="https://ingress.example.com",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        ```
    """
    builder = SessionConfigBuilder()

    if "avatar_id" in kwargs:
        builder.with_avatar_id(kwargs["avatar_id"])
    if "api_key" in kwargs:
        builder.with_api_key(kwargs["api_key"])
    if "app_id" in kwargs:
        builder.with_app_id(kwargs["app_id"])
    if "use_query_auth" in kwargs:
        builder.with_use_query_auth(kwargs["use_query_auth"])
    if "expire_at" in kwargs:
        builder.with_expire_at(kwargs["expire_at"])
    if "sample_rate" in kwargs:
        builder.with_sample_rate(kwargs["sample_rate"])
    if "bitrate" in kwargs:
        builder.with_bitrate(kwargs["bitrate"])
    if "transport_frames" in kwargs:
        builder.with_transport_frames(kwargs["transport_frames"])
    if "on_error" in kwargs:
        builder.with_on_error(kwargs["on_error"])
    if "on_close" in kwargs:
        builder.with_on_close(kwargs["on_close"])
    if "console_endpoint_url" in kwargs:
        builder.with_console_endpoint_url(kwargs["console_endpoint_url"])
    if "ingress_endpoint_url" in kwargs:
        builder.with_ingress_endpoint_url(kwargs["ingress_endpoint_url"])
    if "livekit_egress" in kwargs:
        builder.with_livekit_egress(kwargs["livekit_egress"])
    if "agora_egress" in kwargs:
        builder.with_agora_egress(kwargs["agora_egress"])

    config = builder.build()
    return AvatarSession(config)
