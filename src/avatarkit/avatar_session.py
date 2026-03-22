"""Main AvatarSession implementation for managing avatar websocket sessions."""

import asyncio
import inspect
import json
import logging
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp
import websockets

from .audio_encoder import OggOpusStreamEncoder
from .errors import AvatarSDKError, AvatarSDKErrorCode, SessionTokenError
from .logid import generate_log_id
from .proto.generated import message_pb2
from .session_config import (
    AudioFormat,
    OggOpusEncoderConfig,
    SessionConfig,
    SessionConfigBuilder,
)

SESSION_TOKEN_PATH = "/session-tokens"
INGRESS_WEBSOCKET_PATH = "/websocket"

logger = logging.getLogger(__name__)


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
        self._connection: Optional[Any] = None
        self._current_req_id: Optional[str] = None
        self._last_req_id: Optional[str] = (
            None  # tracks most recent request for interrupt
        )
        self._audio_encoder: Optional[OggOpusStreamEncoder] = None
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
            try:
                async with session.post(
                    endpoint, json=payload, headers=headers
                ) as response:
                    response_text = await response.text()
            except aiohttp.ClientError as e:
                raise SessionTokenError(
                    f"Failed to create session token: {e}",
                    code=AvatarSDKErrorCode.connectionFailed,
                ) from e
            except asyncio.TimeoutError as e:
                raise SessionTokenError(
                    "Timed out while creating session token",
                    code=AvatarSDKErrorCode.connectionFailed,
                ) from e

            response_data = self._try_parse_json(response_text)

            if response.status != 200:
                raise self._build_session_token_error(
                    response.status,
                    payload=response_data,
                    raw_body=response_text,
                )

            if not isinstance(response_data, dict):
                raise SessionTokenError(
                    "Failed to decode session token response",
                    code=AvatarSDKErrorCode.protocolError,
                    raw_body=response_text,
                )

            errors = response_data.get("errors", [])
            if errors:
                error_status = self._coerce_int(
                    self._extract_error_details(response_data).get("status")
                )
                raise self._build_session_token_error(
                    error_status if error_status is not None else response.status,
                    payload=response_data,
                    raw_body=response_text,
                )

            session_token = response_data.get("sessionToken")
            if not session_token:
                raise SessionTokenError(
                    "Empty session token in response",
                    code=AvatarSDKErrorCode.protocolError,
                    raw_body=response_text,
                )

            self._session_token = session_token

    async def start(self) -> str:
        """
        Establish WebSocket connection to the ingress endpoint.

        Returns:
            Connection ID for tracking this session.

        Raises:
            ValueError: If configuration is invalid or session not initialized.
            AvatarSDKError: If WebSocket connection, handshake, or runtime setup fails.
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
            raise self._build_websocket_connect_error(e) from e

        # v2 handshake:
        # 1) client sends ClientConfigureSession
        # 2) server responds with ServerConfirmSession (connection_id) OR ServerError
        try:
            await self._send_client_configure_session()
            server_connection_id = await self._await_server_confirm_session()
        except Exception:
            if self._connection is not None:
                try:
                    await self._connection.close()
                except Exception:
                    pass
                finally:
                    self._connection = None
            raise
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
        msg.client_configure_session.audio_format = self._proto_audio_format(
            self._config.audio_format
        )
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
            msg.client_configure_session.livekit_egress.api_token = (
                self._config.livekit_egress.api_token
            )
            msg.client_configure_session.livekit_egress.room_name = (
                self._config.livekit_egress.room_name
            )
            msg.client_configure_session.livekit_egress.publisher_id = (
                self._config.livekit_egress.publisher_id
            )
            if self._config.livekit_egress.extra_attributes:
                msg.client_configure_session.livekit_egress.extra_attributes.update(
                    self._config.livekit_egress.extra_attributes
                )
            msg.client_configure_session.livekit_egress.idle_timeout = int(
                self._config.livekit_egress.idle_timeout
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

        try:
            await self._connection.send(msg.SerializeToString())
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_handshake",
                action="send session configuration",
            ) from e

    async def _await_server_confirm_session(self) -> str:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        try:
            raw = await self._connection.recv()
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_handshake",
                action="receive handshake response",
            ) from e

        if not isinstance(raw, (bytes, bytearray)):
            raise AvatarSDKError(
                code=AvatarSDKErrorCode.protocolError,
                message="Failed during websocket handshake: expected binary protobuf message",
                phase="websocket_handshake",
            )

        envelope = message_pb2.Message()
        try:
            envelope.ParseFromString(bytes(raw))
        except Exception as e:
            raise AvatarSDKError(
                code=AvatarSDKErrorCode.protocolError,
                message=f"Failed during websocket handshake: invalid protobuf payload ({e})",
                phase="websocket_handshake",
            ) from e

        if envelope.type == message_pb2.MESSAGE_SERVER_CONFIRM_SESSION:
            cid = envelope.server_confirm_session.connection_id
            if not cid:
                raise AvatarSDKError(
                    code=AvatarSDKErrorCode.protocolError,
                    message="Handshake succeeded but server_confirm_session.connection_id is empty",
                    phase="websocket_handshake",
                )
            return cid

        if envelope.type == message_pb2.MESSAGE_SERVER_ERROR:
            err = envelope.server_error
            server_code = str(err.code)
            server_detail = err.message or None
            raise AvatarSDKError(
                code=self._classify_error_code(
                    phase="websocket_handshake",
                    server_code=server_code,
                    detail=server_detail,
                ),
                message=self._format_server_error_message(
                    "WebSocket handshake rejected by server",
                    code=server_code,
                    detail=err.message,
                ),
                phase="websocket_handshake",
                connection_id=err.connection_id or None,
                req_id=err.req_id or None,
                server_code=server_code,
                server_detail=server_detail,
            )

        raise AvatarSDKError(
            code=AvatarSDKErrorCode.protocolError,
            message=f"Unexpected message during handshake: type={envelope.type}",
            phase="websocket_handshake",
        )

    async def send_audio(self, audio: bytes, end: bool = False) -> str:
        """
        Send audio data to the server.

        Audio bytes must match the session-level ``audio_format`` negotiated during
        ``start()``. For ``AudioFormat.OGG_OPUS``, callers can either send pre-encoded
        Ogg Opus bytes directly, or enable ``ogg_opus_encoder`` on the session config so
        ``send_audio()`` accepts raw PCM input and the SDK encodes it internally.

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

        encoded_stream: Optional[bytes] = None
        payload = audio
        use_internal_encoder = self._uses_internal_ogg_opus_encoder()
        if use_internal_encoder:
            encoder = self._get_or_create_audio_encoder()
            encoded_chunk = encoder.encode(audio, end=end)
            payload = encoded_chunk.payload
            encoded_stream = encoded_chunk.completed_stream

        if use_internal_encoder and not payload and not end:
            return req_id

        # Create protobuf message
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_AUDIO_INPUT
        msg.client_audio_input.req_id = req_id
        msg.client_audio_input.audio = payload
        msg.client_audio_input.end = end

        # Serialize and send
        data = msg.SerializeToString()
        try:
            await self._connection.send(data)
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_send",
                action="send audio",
                req_id=req_id,
            ) from e

        if encoded_stream is not None:
            self._notify_encoded_audio(req_id, encoded_stream)

        if end:
            self._current_req_id = None
            self._audio_encoder = None

        return req_id

    def _uses_internal_ogg_opus_encoder(self) -> bool:
        return (
            self._config.audio_format == AudioFormat.OGG_OPUS
            and self._config.ogg_opus_encoder is not None
        )

    def _get_or_create_audio_encoder(self) -> OggOpusStreamEncoder:
        if self._audio_encoder is None:
            encoder_config = self._config.ogg_opus_encoder or OggOpusEncoderConfig()
            self._audio_encoder = OggOpusStreamEncoder(
                sample_rate=int(self._config.sample_rate),
                bitrate=int(self._config.bitrate),
                frame_duration_ms=int(encoder_config.frame_duration_ms),
                application=str(encoder_config.application),
                collect_encoded_output=self._config.on_encoded_audio is not None,
            )

        return self._audio_encoder

    def _notify_encoded_audio(self, req_id: str, encoded_audio: bytes) -> None:
        callback = self._config.on_encoded_audio
        if callback is None:
            return

        try:
            callback(req_id, encoded_audio)
        except Exception:
            logger.exception("on_encoded_audio callback raised an exception")

    @staticmethod
    def _proto_audio_format(audio_format: str) -> int:
        audio_format = AudioFormat(audio_format)

        if audio_format == AudioFormat.PCM_S16LE:
            return message_pb2.AUDIO_FORMAT_PCM_S16LE

        if audio_format == AudioFormat.OGG_OPUS:
            return message_pb2.AUDIO_FORMAT_OGG_OPUS

        raise ValueError(f"Unsupported audio format: {audio_format}")

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
        try:
            await self._connection.send(data)
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_send",
                action="send interrupt",
                req_id=req_id,
            ) from e

        # Clear current request ID so next send_audio creates a new one
        self._current_req_id = None
        self._audio_encoder = None

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

        self._audio_encoder = None

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
        connection = self._connection
        if connection is None:
            return

        try:
            async for message in connection:
                if isinstance(message, bytes):
                    await self._handle_binary_message(message)
        except websockets.exceptions.ConnectionClosedOK:
            # Normal closure.
            pass
        except websockets.exceptions.ConnectionClosed as e:
            self._notify_error(self._build_connection_closed_error(e))
        except asyncio.CancelledError:
            # Task was cancelled
            raise
        except Exception as e:
            self._notify_error(
                self._coerce_avatar_error(
                    e,
                    code=AvatarSDKErrorCode.connectionFailed,
                    phase="websocket_runtime",
                    message=f"Read loop error: {e}",
                )
            )
        finally:
            # Ensure connection is closed
            await self.close()

    async def _handle_binary_message(self, payload: bytes) -> None:
        """Handle a binary message received from the server."""
        try:
            envelope = message_pb2.Message()
            envelope.ParseFromString(payload)
        except Exception as e:
            self._notify_error(
                AvatarSDKError(
                    code=AvatarSDKErrorCode.protocolError,
                    message=f"Failed to decode message: {e}",
                    phase="websocket_runtime",
                )
            )
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
            err = envelope.server_error
            server_code = str(err.code)
            server_detail = err.message or None
            self._notify_error(
                AvatarSDKError(
                    code=self._classify_error_code(
                        phase="websocket_runtime",
                        server_code=server_code,
                        detail=server_detail,
                    ),
                    message=self._format_server_error_message(
                        "Avatar session error",
                        code=server_code,
                        detail=err.message,
                    ),
                    phase="websocket_runtime",
                    connection_id=err.connection_id or None,
                    req_id=err.req_id or None,
                    server_code=server_code,
                    server_detail=server_detail,
                )
            )

    @staticmethod
    def _try_parse_json(body: str) -> Optional[Any]:
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stringify(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value)

    @classmethod
    def _extract_error_details(cls, payload: Any) -> dict[str, Optional[str]]:
        if isinstance(payload, str):
            parsed = cls._try_parse_json(payload)
            if parsed is None:
                text = payload.strip()
                return {"message": text or None}
            payload = parsed

        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first_error = errors[0]
                if isinstance(first_error, dict):
                    return {
                        "status": cls._stringify(first_error.get("status")),
                        "code": cls._stringify(
                            first_error.get("code") or first_error.get("id")
                        ),
                        "title": cls._stringify(first_error.get("title")),
                        "detail": cls._stringify(first_error.get("detail")),
                        "message": cls._stringify(first_error.get("message")),
                    }

            return {
                "status": cls._stringify(payload.get("status")),
                "code": cls._stringify(payload.get("code") or payload.get("error")),
                "title": cls._stringify(payload.get("title")),
                "detail": cls._stringify(payload.get("detail")),
                "message": cls._stringify(payload.get("message")),
            }

        return {}

    @classmethod
    def _compose_error_message(
        cls,
        prefix: str,
        *,
        status: Optional[int],
        details: dict[str, Optional[str]],
    ) -> str:
        message = prefix
        if status is not None:
            message += f" (HTTP {status})"

        detail_parts = [
            part
            for part in (
                details.get("title"),
                details.get("detail") or details.get("message"),
            )
            if part
        ]

        if detail_parts:
            return f"{message}: {' - '.join(detail_parts)}"

        if details.get("code"):
            return f"{message}: {details['code']}"

        return message

    @classmethod
    def _format_server_error_message(
        cls,
        prefix: str,
        *,
        code: Optional[str],
        detail: Optional[str],
    ) -> str:
        if code and detail:
            return f"{prefix}: {detail} (server code {code})"
        if detail:
            return f"{prefix}: {detail}"
        if code:
            return f"{prefix}: server code {code}"
        return prefix

    @staticmethod
    def _normalize_error_text(*values: Optional[str]) -> str:
        parts = [value.strip().lower() for value in values if value and value.strip()]
        return " | ".join(parts)

    @classmethod
    def _classify_error_code(
        cls,
        *,
        phase: str,
        http_status: Optional[int] = None,
        server_code: Optional[str] = None,
        title: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> AvatarSDKErrorCode:
        detail_text = cls._normalize_error_text(server_code, title, detail)

        if server_code in ("3", "INVALID_ARGUMENT"):
            if (
                "livekit" in detail_text
                or "agora" in detail_text
                or "egress" in detail_text
            ):
                return AvatarSDKErrorCode.invalidEgressConfig
            return AvatarSDKErrorCode.invalidRequest
        if server_code in ("16", "UNAUTHENTICATED"):
            return AvatarSDKErrorCode.invalidEgressConfig
        if server_code in ("14", "UNAVAILABLE"):
            return AvatarSDKErrorCode.egressUnavailable

        if server_code == "4001":
            return AvatarSDKErrorCode.creditsExhausted
        if server_code == "4002":
            return AvatarSDKErrorCode.sessionDurationExceeded

        if "credits exhausted" in detail_text:
            return AvatarSDKErrorCode.creditsExhausted
        if (
            "session time limit reached" in detail_text
            or "maximum session duration" in detail_text
        ):
            return AvatarSDKErrorCode.sessionDurationExceeded
        if "session denied" in detail_text or http_status == 402:
            return AvatarSDKErrorCode.billingRequired
        if "invalid session token" in detail_text or "empty token" in detail_text:
            return AvatarSDKErrorCode.sessionTokenInvalid
        if "token is expired" in detail_text or "session token expired" in detail_text:
            return AvatarSDKErrorCode.sessionTokenExpired
        if "app id mismatch" in detail_text:
            return AvatarSDKErrorCode.appIDMismatch
        if "appidunrecognized" in detail_text or "app id unrecognized" in detail_text:
            return AvatarSDKErrorCode.appIDUnrecognized
        if "avatar not found" in detail_text:
            return AvatarSDKErrorCode.avatarNotFound
        if "unsupported sample rate" in detail_text:
            return AvatarSDKErrorCode.unsupportedSampleRate
        if (
            "livekit silence timeout" in detail_text
            or "no audio input for" in detail_text
        ):
            return AvatarSDKErrorCode.idleTimeout
        if "livekit_egress" in detail_text or "agora_egress" in detail_text:
            return AvatarSDKErrorCode.invalidEgressConfig
        if (
            "missing livekit credentials" in detail_text
            or "provide api_token or both api_key and api_secret" in detail_text
            or "unauthorized" in detail_text
        ):
            return AvatarSDKErrorCode.invalidEgressConfig
        if (
            "egress client is not configured on server" in detail_text
            or "failed to create egress connection" in detail_text
        ):
            return AvatarSDKErrorCode.egressUnavailable
        if (
            "driven server returned non-200 status code" in detail_text
            or "driven server request failed" in detail_text
        ):
            return AvatarSDKErrorCode.upstreamError
        if (
            "expected clientconfiguresession message" in detail_text
            or "clientconfiguresession message is nil" in detail_text
            or "unexpected message type" in detail_text
            or "failed to unmarshal initial message" in detail_text
            or "failed during websocket handshake: expected binary protobuf message"
            in detail_text
            or "failed during websocket handshake: invalid protobuf payload"
            in detail_text
        ):
            return AvatarSDKErrorCode.protocolError

        mapped = cls._map_http_status_to_error_code(http_status, phase, detail_text)
        if mapped != AvatarSDKErrorCode.unknown:
            return mapped

        if phase in ("websocket_handshake", "websocket_runtime"):
            return AvatarSDKErrorCode.serverError

        return AvatarSDKErrorCode.unknown

    @staticmethod
    def _map_http_status_to_error_code(
        status: Optional[int], phase: str, detail_text: str = ""
    ) -> AvatarSDKErrorCode:
        if status == 401:
            return AvatarSDKErrorCode.sessionTokenExpired
        if status == 404 and "avatar not found" in detail_text:
            return AvatarSDKErrorCode.avatarNotFound
        if status == 404:
            return AvatarSDKErrorCode.appIDUnrecognized
        if status == 402:
            return AvatarSDKErrorCode.billingRequired
        if status == 400 and "app id mismatch" in detail_text:
            return AvatarSDKErrorCode.appIDMismatch
        if phase == "websocket_connect" and status == 400:
            return AvatarSDKErrorCode.sessionTokenInvalid
        if status is not None and 400 <= status < 500:
            return AvatarSDKErrorCode.invalidRequest
        if status is not None and status >= 500:
            return AvatarSDKErrorCode.serverError
        return AvatarSDKErrorCode.unknown

    @classmethod
    def _build_session_token_error(
        cls,
        status: int,
        *,
        payload: Any,
        raw_body: str,
    ) -> SessionTokenError:
        details = cls._extract_error_details(payload)
        return SessionTokenError(
            cls._compose_error_message(
                "Failed to create session token",
                status=status,
                details=details,
            ),
            code=cls._classify_error_code(
                phase="session_token",
                http_status=status,
                server_code=details.get("code"),
                title=details.get("title"),
                detail=details.get("detail") or details.get("message"),
            ),
            http_status=status,
            server_code=details.get("code"),
            server_title=details.get("title"),
            server_detail=details.get("detail") or details.get("message"),
            raw_body=raw_body,
        )

    @classmethod
    def _build_websocket_connect_error(cls, exc: Exception) -> AvatarSDKError:
        status = cls._coerce_int(cls._extract_http_status(exc))
        raw_body = cls._extract_http_body(exc)
        details = cls._extract_error_details(raw_body)

        if status is not None:
            server_detail = details.get("detail") or details.get("message")
            return AvatarSDKError(
                code=cls._classify_error_code(
                    phase="websocket_connect",
                    http_status=status,
                    server_code=details.get("code"),
                    title=details.get("title"),
                    detail=server_detail,
                ),
                message=cls._compose_error_message(
                    "WebSocket connection rejected",
                    status=status,
                    details=details,
                ),
                phase="websocket_connect",
                http_status=status,
                server_code=details.get("code"),
                server_title=details.get("title"),
                server_detail=server_detail,
                raw_body=raw_body,
            )

        return cls._coerce_avatar_error(
            exc,
            code=AvatarSDKErrorCode.connectionFailed,
            phase="websocket_connect",
            message=f"Failed to connect to websocket: {exc}",
        )

    @classmethod
    def _build_transport_error(
        cls,
        exc: Exception,
        *,
        phase: str,
        action: str,
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        if isinstance(exc, websockets.exceptions.ConnectionClosed):
            return cls._build_connection_closed_error(exc, phase=phase, req_id=req_id)

        return cls._coerce_avatar_error(
            exc,
            code=AvatarSDKErrorCode.connectionFailed,
            phase=phase,
            message=f"Failed to {action}: {exc}",
            req_id=req_id,
        )

    @staticmethod
    def _extract_http_status(exc: Exception) -> Optional[int]:
        for attr in ("status_code", "status"):
            status = getattr(exc, attr, None)
            if status is not None:
                try:
                    return int(status)
                except (TypeError, ValueError):
                    pass

        response = getattr(exc, "response", None)
        if response is None:
            return None

        for attr in ("status_code", "status"):
            status = getattr(response, attr, None)
            if status is not None:
                try:
                    return int(status)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_http_body(exc: Exception) -> Optional[str]:
        response = getattr(exc, "response", None)
        if response is None:
            return None

        body = getattr(response, "body", None)
        if body is None:
            return None
        if isinstance(body, (bytes, bytearray)):
            return bytes(body).decode("utf-8", errors="replace")
        return str(body)

    @classmethod
    def _build_connection_closed_error(
        cls,
        exc: Exception,
        *,
        phase: str = "websocket_runtime",
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        close_frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
        close_code = getattr(close_frame, "code", None)
        close_reason = getattr(close_frame, "reason", None)

        message = "WebSocket connection closed unexpectedly"
        if close_code is not None and close_reason:
            message += f" (code {close_code}: {close_reason})"
        elif close_code is not None:
            message += f" (code {close_code})"
        elif close_reason:
            message += f" ({close_reason})"

        return AvatarSDKError(
            code=AvatarSDKErrorCode.connectionClosed,
            message=message,
            phase=phase,
            req_id=req_id,
            close_code=close_code,
            close_reason=close_reason,
        )

    @staticmethod
    def _coerce_avatar_error(
        exc: Exception,
        *,
        code: AvatarSDKErrorCode,
        phase: str,
        message: str,
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        if isinstance(exc, AvatarSDKError):
            return exc

        return AvatarSDKError(
            code=code,
            message=message,
            phase=phase,
            req_id=req_id,
        )

    def _notify_error(self, error: Exception) -> None:
        try:
            self._config.on_error(error)
        except Exception:
            pass


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
    if "audio_format" in kwargs:
        builder.with_audio_format(kwargs["audio_format"])
    if "ogg_opus_encoder" in kwargs and kwargs["ogg_opus_encoder"] is not None:
        builder.with_ogg_opus_encoder(kwargs["ogg_opus_encoder"])
    if "on_encoded_audio" in kwargs and kwargs["on_encoded_audio"] is not None:
        builder.with_on_encoded_audio(kwargs["on_encoded_audio"])
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
