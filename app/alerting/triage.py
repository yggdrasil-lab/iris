import logging
from typing import Any, Dict, Literal, Optional

from app.jev import (
    JevClientPort,
    JevRouterUnavailableError,
    parse_bool,
    parse_clamped_score,
    resolve_calibrated_confidence,
)
from app.models.schemas import TriageResult

logger = logging.getLogger("iris.alerting.triage")

_VALID_DESTINATIONS: tuple[str, ...] = ("alerts", "decisions", "metrics")

_ALERT_TRIAGE_QUESTIONS: Dict[str, Any] = {
    "urgency_score": {
        "type": "score",
        "criteria": [
            "Routine observation, transient deploy or restart blip, scheduled retry, or self-clearing healthcheck hiccup",
            "Low priority non-actionable warning or benign status notification",
            "Moderate operational issue requiring standard inspection within normal working hours",
            "High-severity failure, service outage, data corruption, or critical threshold breach requiring immediate attention",
        ],
    },
    "is_transient_noise": {
        "type": "noul",
        "instructions": (
            "True if this alert is transient deployment noise, rolling restart blip, "
            "benign heartbeat jitter, or self-clearing routine event that does not require human intervention; "
            "False if it is a real operational issue requiring action."
        ),
    },
    "destination_channel": {
        "type": "choice",
        "criteria": {
            "alerts": "High-urgency technical alerts and system outages",
            "decisions": "Incidents or changes requiring operator approval or decisions",
            "metrics": "Benign telemetry or silent background metrics",
        },
    },
}


def build_alert_state_text(
    *,
    service: str,
    title: str,
    details: str,
    severity: str,
    node: Optional[str],
) -> str:
    return (
        f"Service: {service}\n"
        f"Node: {node or 'yggdrasil-cluster'}\n"
        f"Reported Severity: {severity}\n"
        f"Alert Title: {title}\n"
        f"Details:\n{details}"
    )


def fail_open_triage_result(severity: str, reason: str) -> TriageResult:
    """Deterministic fail-open fallback when Muninn Jev is unreachable for alerts."""
    score_by_severity = {"error": 8, "warning": 5}
    score = score_by_severity.get(severity, 2)
    logger.info(
        "Fail-open fallback applied for severity '%s': score=%d, suppress=False",
        severity,
        score,
    )
    return TriageResult(
        urgency_score=score,
        confidence=1.0,
        is_transient_noise=False,
        destination_channel="alerts",
        suppress=False,
        reason=reason,
        provider="fail-open-fallback",
    )


def _decide_suppression(
    *,
    scaled_score: int,
    is_transient: bool,
    overall_confidence: float,
    score_threshold: int,
    confidence_threshold: float,
) -> tuple[bool, str]:
    if overall_confidence < confidence_threshold:
        return (
            False,
            f"Low confidence ({overall_confidence:.2f} < {confidence_threshold}): "
            f"escalating alert fail-open with score {scaled_score}/10",
        )
    if scaled_score < score_threshold or is_transient:
        return (
            True,
            f"Suppressed by Jev gate: score {scaled_score}/10 < {score_threshold} "
            f"or transient noise detected (confidence: {overall_confidence:.2f})",
        )
    return (
        False,
        f"Actionable alert passed: score {scaled_score}/10 >= {score_threshold} "
        f"(confidence: {overall_confidence:.2f})",
    )


def _parse_triage_result(
    parsed: Dict[str, Any],
    *,
    score_threshold: int,
    confidence_threshold: float,
) -> TriageResult:
    scaled_score = parse_clamped_score(parsed.get("urgency_score"), default=7)
    is_transient = parse_bool(parsed.get("is_transient_noise", False))
    raw_choice = str(parsed.get("destination_channel", "alerts")).strip().lower()
    choice: Literal["alerts", "decisions", "metrics"] = (
        raw_choice if raw_choice in _VALID_DESTINATIONS else "alerts"  # type: ignore[assignment]
    )
    overall_confidence = resolve_calibrated_confidence(parsed, context_label="alert")
    suppress, reason = _decide_suppression(
        scaled_score=scaled_score,
        is_transient=is_transient,
        overall_confidence=overall_confidence,
        score_threshold=score_threshold,
        confidence_threshold=confidence_threshold,
    )
    logger.info(
        "Muninn Jev alert triage: score=%d/10, transient=%s, route=%s, conf=%.2f, suppress=%s",
        scaled_score,
        is_transient,
        choice,
        overall_confidence,
        suppress,
    )
    return TriageResult(
        urgency_score=scaled_score,
        confidence=overall_confidence,
        is_transient_noise=is_transient,
        destination_channel=choice,
        suppress=suppress,
        reason=reason,
        provider="muninn-jev",
    )


async def evaluate_alert_triage(
    jev: JevClientPort,
    *,
    service: str,
    title: str,
    details: str,
    severity: str = "error",
    node: Optional[str] = None,
    score_threshold: int = 7,
    confidence_threshold: float = 0.80,
) -> TriageResult:
    """Evaluate an operational alert via JevClientPort with fail-open safety."""
    state_text = build_alert_state_text(
        service=service, title=title, details=details, severity=severity, node=node
    )
    try:
        parsed = await jev.evaluate_questions(state_text, _ALERT_TRIAGE_QUESTIONS)
    except JevRouterUnavailableError as exc:
        logger.warning("Muninn Jev alert triage failed (%s). Applying fail-open fallback.", exc)
        return fail_open_triage_result(
            severity,
            reason=f"Decision engine unreachable ({exc}); fail-open default applied",
        )
    return _parse_triage_result(
        parsed,
        score_threshold=score_threshold,
        confidence_threshold=confidence_threshold,
    )
