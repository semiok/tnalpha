"""Timezone helpers for China-facing display on a US-time server."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core import config

SERVER_TZ = ZoneInfo(config.SERVER_TIMEZONE)
DISPLAY_TZ = ZoneInfo(config.DISPLAY_TIMEZONE)


def china_today() -> date:
    return datetime.now(DISPLAY_TZ).date()


def to_display_time(value: datetime | None) -> datetime | None:
    """Convert a server-local timestamp to display timezone.

    The app stores legacy system timestamps as naive datetimes created on the
    server. Treat naive values as SERVER_TZ; aware values are converted directly.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=SERVER_TZ)
    return value.astimezone(DISPLAY_TZ).replace(tzinfo=None)


def format_display_time(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    converted = to_display_time(value)
    return converted.strftime(fmt) if converted else ""
