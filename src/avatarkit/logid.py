"""Log ID generation utilities for avatar sessions."""

from datetime import datetime, timezone

from nanoid import generate

LOG_ID_TIME_FORMAT = "%Y%m%d%H%M%S"
LOG_ID_NANOID_LENGTH = 12


def generate_log_id() -> str:
    """
    Generate a log identifier in the format "YYYYMMDDHHMMSS_<nanoid>".

    The timestamp is generated in UTC and the nanoid suffix contains 12 characters.

    Returns:
        A unique log ID string.

    Example:
        "20231215143022_AbC123XyZ456"
    """
    return _generate_log_id(datetime.now(timezone.utc))


def _generate_log_id(now: datetime) -> str:
    """Internal function to generate log ID with a specific datetime."""
    timestamp = now.strftime(LOG_ID_TIME_FORMAT)
    suffix = generate(size=LOG_ID_NANOID_LENGTH)
    return f"{timestamp}_{suffix}"
