from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable


class JevRouterUnavailableError(RuntimeError):
    """Raised when the Muninn Jev decision router fails, is unconfigured, or returns invalid output."""


@dataclass(frozen=True)
class JevConfig:
    muninn_base: str
    muninn_api_key: Optional[str]
    muninn_jev_model: str = "jev-decision"
    score_threshold: int = 7
    confidence_threshold: float = 0.80
    timeout_seconds: float = 10.0


@runtime_checkable
class JevClientPort(Protocol):
    async def evaluate_questions(
        self,
        state_text: str,
        questions: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Evaluate state_text against a Jev System One questions schema."""
