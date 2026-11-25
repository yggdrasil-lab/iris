from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable


@dataclass(frozen=True)
class AlertingConfig:
    bot_token: Optional[str]
    alert_channel_id: Optional[str]
    info_channel_id: Optional[str]
    health_channel_id: Optional[str]
    wealth_channel_id: Optional[str]
    default_user_id: Optional[str]
    default_cooldown_seconds: int = 1800
    alert_triage_enabled: bool = False
    score_threshold: int = 7
    confidence_threshold: float = 0.80


@runtime_checkable
class ClockPort(Protocol):
    def now_seconds(self) -> float:
        """Return monotonic seconds for cooldown interval tracking."""

    def now_utc(self) -> datetime:
        """Return current timezone-aware UTC datetime for alert timestamps."""


class SystemClockAdapter:
    """Production clock adapter backed by monotonic time and UTC datetime."""

    def now_seconds(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


@runtime_checkable
class DiscordTransportPort(Protocol):
    async def post_message(
        self,
        *,
        bot_token: str,
        channel_id: str,
        thread_name: str,
        message_payload: Dict[str, Any],
        thread_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Deliver a Discord message payload to a thread or channel."""
