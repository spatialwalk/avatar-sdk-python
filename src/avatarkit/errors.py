from __future__ import annotations

from enum import Enum
from typing import Optional


class AvatarSDKErrorCode(str, Enum):
    """
    Stable error codes surfaced by the SDK.

    Notes:
    - These codes are intentionally string enums so they serialize cleanly to logs/JSON.
    - Server-specific details are attached separately on AvatarSDKError.
    """

    sessionTokenExpired = "sessionTokenExpired"
    sessionTokenInvalid = "sessionTokenInvalid"
    appIDUnrecognized = "appIDUnrecognized"
    appIDMismatch = "appIDMismatch"
    avatarNotFound = "avatarNotFound"
    billingRequired = "billingRequired"
    creditsExhausted = "creditsExhausted"
    sessionDurationExceeded = "sessionDurationExceeded"
    unsupportedSampleRate = "unsupportedSampleRate"
    invalidEgressConfig = "invalidEgressConfig"
    egressUnavailable = "egressUnavailable"
    idleTimeout = "idleTimeout"
    upstreamError = "upstreamError"
    invalidRequest = "invalidRequest"
    connectionFailed = "connectionFailed"
    connectionClosed = "connectionClosed"
    protocolError = "protocolError"
    serverError = "serverError"
    unknown = "unknown"


class AvatarSDKError(Exception):
    """SDK exception with a stable error code and structured context."""

    def __init__(
        self,
        code: AvatarSDKErrorCode,
        message: str,
        *,
        phase: str = "unknown",
        http_status: Optional[int] = None,
        server_code: Optional[str] = None,
        server_title: Optional[str] = None,
        server_detail: Optional[str] = None,
        connection_id: Optional[str] = None,
        req_id: Optional[str] = None,
        raw_body: Optional[str] = None,
        close_code: Optional[int] = None,
        close_reason: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.phase = phase
        self.http_status = http_status
        self.server_code = server_code
        self.server_title = server_title
        self.server_detail = server_detail
        self.connection_id = connection_id
        self.req_id = req_id
        self.raw_body = raw_body
        self.close_code = close_code
        self.close_reason = close_reason

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code.value}: {self.message}"


class SessionTokenError(AvatarSDKError):
    """Raised when session token request or parsing fails."""

    def __init__(
        self,
        message: str,
        *,
        code: AvatarSDKErrorCode = AvatarSDKErrorCode.invalidRequest,
        phase: str = "session_token",
        http_status: Optional[int] = None,
        server_code: Optional[str] = None,
        server_title: Optional[str] = None,
        server_detail: Optional[str] = None,
        connection_id: Optional[str] = None,
        req_id: Optional[str] = None,
        raw_body: Optional[str] = None,
        close_code: Optional[int] = None,
        close_reason: Optional[str] = None,
    ):
        super().__init__(
            code=code,
            message=message,
            phase=phase,
            http_status=http_status,
            server_code=server_code,
            server_title=server_title,
            server_detail=server_detail,
            connection_id=connection_id,
            req_id=req_id,
            raw_body=raw_body,
            close_code=close_code,
            close_reason=close_reason,
        )
