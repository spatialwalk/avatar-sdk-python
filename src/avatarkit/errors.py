from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AvatarSDKErrorCode(str, Enum):
    """
    Stable error codes surfaced by the SDK.

    Notes:
    - These codes are referenced by the v2 websocket API documentation.
    - They are intentionally string enums so they serialize cleanly to logs/JSON.
    """

    sessionTokenExpired = "sessionTokenExpired"
    sessionTokenInvalid = "sessionTokenInvalid"
    appIDUnrecognized = "appIDUnrecognized"
    unknown = "unknown"


@dataclass(frozen=True)
class AvatarSDKError(Exception):
    """
    SDK exception with a stable error code.
    """

    code: AvatarSDKErrorCode
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code.value}: {self.message}"
