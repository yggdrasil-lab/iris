from datetime import datetime
import json
import logging
import math
from typing import Any, Dict, List, Optional
import httpx
from app.core.config import settings
from app.models.schemas import EmailRouteVerdict, RouteTargetType, TriageResult

logger = logging.getLogger("iris.triage")

VALID_ROUTE_TARGETS: set[str] = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_bank_statement",
    "actionable_notice",
    "ignore",
}


class JevRouterUnavailableError(RuntimeError):
    """Raised when the Muninn Jev decision router fails or is unconfigured."""


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "1", "yes"):
            return True
        if cleaned in ("false", "0", "no"):
            return False
    return False


def _parse_clamped_score(value: Any, default: int = 5) -> int:
    if value is None:
        return default
    try:
        numeric = float(str(value).strip())
        return max(1, min(10, int(round(numeric))))
    except (TypeError, ValueError):
        return default


def _parse_clamped_confidence(value: Any) -> float:
    """Parse a finite numeric confidence value and clamp it to [0.0, 1.0]."""
    if isinstance(value, bool):
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))


_INBOUND_EMAIL_QUESTIONS: Dict[str, Any] = {
    "route_target": {
        "type": "choice",
        "criteria": {
            "plutus_dbs_alert": (
                "DBS Bank transaction alert (including iBanking Alerts, digibank Alert, or emails from ibanking.alert@dbs.com), "
                "card charge, PayNow, FAST transfer, bill payment, or GIRO notification"
            ),
            "plutus_purchase_receipt": (
                "E-commerce order confirmation (e.g., Amazon), tax invoice, flight itinerary or airline e-ticket receipt, "
                "hotel or travel booking payment confirmation or deposit receipt, or forwarded purchase receipt "
                "(containing an itemized or total spend, or emails with attached invoice/receipt PDFs or images such as TaxInvoice.pdf or SI_*.jpg)"
            ),
            "plutus_bank_statement": "Bank or credit card statement notification or attached statement PDF/CSV",
            "actionable_notice": (
                "Important non-bank notice requiring action without a paid purchase receipt or invoice, "
                "such as rego/WOF reminder, unpaid utility bill notice, medical result, "
                "flight schedule change or check-in reminder, or renewal"
            ),
            "ignore": "Shipping tracking update without purchase total, marketing promo, newsletter, OTP login code, or low-value noise",
        },
    },
    "has_actionable_amount": {
        "type": "noul",
        "instructions": (
            "True if a concrete monetary purchase or financial transaction amount with currency or dollar figure is present "
            "(including compact currency strings such as SGD2000.00, $179.74, or AUD amounts), "
            "or if a payment or booking confirmation carries an invoice/receipt PDF or image attachment "
            "(e.g., TaxInvoice.pdf, SI_*.jpg) even if the exact monetary amount appears inside the attachment or later in a quoted thread; "
            "False if no concrete monetary transaction amount or invoice/receipt attachment is present."
        ),
    },
    "significance_score": {
        "type": "score",
        "criteria": [
            "Marketing promo, newsletter, OTP code, or shipping tracking noise with no financial or action relevance",
            "Low-priority informational email without urgent financial or operator action",
            "Valid bank alert, purchase receipt, or financial statement ready for ingestion",
            "High-priority financial charge, large transfer, overdue bill, or urgent actionable notice",
        ],
    },
}


def _build_inbound_email_state_text(
    from_address: str,
    to_address: str,
    subject: str,
    received_at: datetime | str,
    raw_excerpt: str,
    attachments_meta: List[Dict[str, Any]],
) -> str:
    """Format inbound email headers, attachment metadata, and excerpt for Jev."""
    received_str = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at)
    if attachments_meta:
        attachment_lines = [
            f"- {att.get('filename', 'unnamed')} ({att.get('content_type', 'application/octet-stream')}, {att.get('size_bytes', 0)} bytes)"
            for att in attachments_meta
        ]
        attachments_summary = "\n".join(attachment_lines)
    else:
        attachments_summary = "None"

    return (
        f"From: {from_address}\n"
        f"To: {to_address}\n"
        f"Received At: {received_str}\n"
        f"Subject: {subject}\n"
        f"Attachments:\n{attachments_summary}\n\n"
        f"Body Excerpt:\n{raw_excerpt}"
    )


def _resolve_inbound_routing_confidence(parsed: Dict[str, Any]) -> float:
    """Derive inbound email routing confidence from Jev question confidences or _confidence."""
    question_conf = parsed.get("_question_confidence")
    if (
        isinstance(question_conf, dict)
        and question_conf.get("route_target") is not None
        and question_conf.get("has_actionable_amount") is not None
    ):
        return min(
            _parse_clamped_confidence(question_conf["route_target"]),
            _parse_clamped_confidence(question_conf["has_actionable_amount"]),
        )

    raw_conf = parsed.get("_confidence")
    if raw_conf is None:
        logger.warning(
            "Muninn jev-decision email response missing _confidence (odin jev-shim include_meta update is needed); "
            "setting confidence=0.0 so email parks safely as needs_review."
        )
        return 0.0
    return _parse_clamped_confidence(raw_conf)


class TriageService:
    """Centralized Muninn Jev decision triage and inbound email routing service.

    Routes all System One primitives (Score, Noul, Choice) through Muninn's
    `jev-decision` model (`MUNINN_API_BASE` + `MUNINN_API_KEY`).
    """

    def __init__(
        self,
        muninn_base: Optional[str] = None,
        muninn_api_key: Optional[str] = None,
        muninn_jev_model: Optional[str] = None,
        score_threshold: Optional[int] = None,
        confidence_threshold: Optional[float] = None,
    ):
        self._overrides: Dict[str, Any] = {}
        if muninn_base is not None:
            self._overrides["muninn_base"] = muninn_base
        if muninn_api_key is not None:
            self._overrides["muninn_api_key"] = muninn_api_key
        if muninn_jev_model is not None:
            self._overrides["muninn_jev_model"] = muninn_jev_model
        if score_threshold is not None:
            self._overrides["score_threshold"] = score_threshold
        if confidence_threshold is not None:
            self._overrides["confidence_threshold"] = confidence_threshold

    @property
    def muninn_base(self) -> str:
        return self._overrides["muninn_base"] if "muninn_base" in self._overrides else settings.MUNINN_API_BASE

    @muninn_base.setter
    def muninn_base(self, value: str) -> None:
        self._overrides["muninn_base"] = value

    @property
    def muninn_api_key(self) -> Optional[str]:
        return self._overrides["muninn_api_key"] if "muninn_api_key" in self._overrides else settings.MUNINN_API_KEY

    @muninn_api_key.setter
    def muninn_api_key(self, value: Optional[str]) -> None:
        self._overrides["muninn_api_key"] = value

    @property
    def muninn_jev_model(self) -> str:
        return (
            self._overrides["muninn_jev_model"]
            if "muninn_jev_model" in self._overrides
            else settings.MUNINN_JEV_MODEL
        )

    @muninn_jev_model.setter
    def muninn_jev_model(self, value: str) -> None:
        self._overrides["muninn_jev_model"] = value

    @property
    def score_threshold(self) -> int:
        return self._overrides.get("score_threshold", settings.IRIS_ALERT_SCORE_THRESHOLD)

    @score_threshold.setter
    def score_threshold(self, value: int) -> None:
        self._overrides["score_threshold"] = value

    @property
    def confidence_threshold(self) -> float:
        return self._overrides.get("confidence_threshold", settings.IRIS_ALERT_CONFIDENCE_THRESHOLD)

    @confidence_threshold.setter
    def confidence_threshold(self, value: float) -> None:
        self._overrides["confidence_threshold"] = value

    async def _call_muninn_jev(
        self,
        state_text: str,
        questions: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Invoke Muninn `jev-decision` via OpenAI-compatible chat completions."""
        base = (self.muninn_base or "").strip()
        api_key = (self.muninn_api_key or "").strip()
        if not base or not api_key:
            raise JevRouterUnavailableError(
                "Muninn Jev router unconfigured: MUNINN_API_BASE and MUNINN_API_KEY are required"
            )

        schema_json = json.dumps({"questions": questions, "include_meta": True})
        system_prompt = (
            "Evaluate the state against the following Jev System One decision schema:\n"
            f"```json\n{schema_json}\n```"
        )
        payload = {
            "model": (self.muninn_jev_model or "jev-decision").strip(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state_text},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Yggdrasil-Iris/1.0",
        }

        url = f"{base.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise JevRouterUnavailableError(f"Muninn Jev call failed: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Assistant message content is not a string")
            parsed = json.loads(content)
        except Exception as exc:
            raise JevRouterUnavailableError(f"Invalid Muninn Jev JSON response: {exc}") from exc

        if not isinstance(parsed, dict):
            raise JevRouterUnavailableError("Muninn Jev response payload must be a JSON object")
        return parsed

    async def evaluate_alert(
        self,
        service: str,
        title: str,
        details: str,
        severity: str = "error",
        node: Optional[str] = None,
    ) -> TriageResult:
        """Evaluate an operational alert via Muninn jev-decision with fail-open safety."""
        state_text = (
            f"Service: {service}\n"
            f"Node: {node or 'yggdrasil-cluster'}\n"
            f"Reported Severity: {severity}\n"
            f"Alert Title: {title}\n"
            f"Details:\n{details}"
        )
        questions = {
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

        try:
            parsed = await self._call_muninn_jev(state_text, questions)
        except JevRouterUnavailableError as exc:
            logger.warning("Muninn Jev alert triage failed (%s). Applying fail-open fallback.", exc)
            return self._fail_open_fallback(
                severity,
                reason=f"Decision engine unreachable ({exc}); fail-open default applied",
            )

        scaled_score = _parse_clamped_score(parsed.get("urgency_score"), default=7)
        is_transient = _parse_bool(parsed.get("is_transient_noise", False))
        choice = str(parsed.get("destination_channel", "alerts")).strip().lower()
        if choice not in ("alerts", "decisions", "metrics"):
            choice = "alerts"

        raw_conf = parsed.get("_confidence")
        if raw_conf is None:
            logger.warning(
                "Muninn jev-decision alert response missing _confidence (odin jev-shim include_meta update required); "
                "defaulting confidence to 0.0 for fail-open escalation."
            )
            overall_confidence = 0.0
        else:
            try:
                overall_confidence = max(0.0, min(1.0, float(raw_conf)))
            except (TypeError, ValueError):
                overall_confidence = 0.0

        if overall_confidence >= self.confidence_threshold:
            if scaled_score < self.score_threshold or is_transient:
                suppress = True
                reason = (
                    f"Suppressed by Jev gate: score {scaled_score}/10 < {self.score_threshold} "
                    f"or transient noise detected (confidence: {overall_confidence:.2f})"
                )
            else:
                suppress = False
                reason = (
                    f"Actionable alert passed: score {scaled_score}/10 >= {self.score_threshold} "
                    f"(confidence: {overall_confidence:.2f})"
                )
        else:
            suppress = False
            reason = (
                f"Low confidence ({overall_confidence:.2f} < {self.confidence_threshold}): "
                f"escalating alert fail-open with score {scaled_score}/10"
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
            destination_channel=choice,  # type: ignore[arg-type]
            suppress=suppress,
            reason=reason,
            provider="muninn-jev",
        )

    async def route_inbound_email(
        self,
        from_address: str,
        to_address: str,
        subject: str,
        received_at: datetime | str,
        raw_excerpt: str,
        attachments_meta: List[Dict[str, Any]],
    ) -> EmailRouteVerdict:
        """Classify and route an inbound email via Muninn jev-decision; fails loud on errors."""
        state_text = _build_inbound_email_state_text(
            from_address, to_address, subject, received_at, raw_excerpt, attachments_meta
        )
        parsed = await self._call_muninn_jev(state_text, _INBOUND_EMAIL_QUESTIONS)

        route_target = parsed.get("route_target")
        if not isinstance(route_target, str) or route_target not in VALID_ROUTE_TARGETS:
            raise JevRouterUnavailableError(
                f"Invalid or missing route_target from Muninn Jev: {route_target!r}"
            )

        has_actionable_amount = _parse_bool(parsed.get("has_actionable_amount", False))
        scaled_score = _parse_clamped_score(parsed.get("significance_score"), default=5)
        overall_confidence = _resolve_inbound_routing_confidence(parsed)
        reason = (
            f"Jev classified as {route_target} "
            f"(amount={has_actionable_amount}, score={scaled_score}/10, conf={overall_confidence:.2f})"
        )

        typed_target: RouteTargetType = route_target  # type: ignore[assignment]
        return EmailRouteVerdict(
            route_target=typed_target,
            has_actionable_amount=has_actionable_amount,
            confidence=overall_confidence,
            urgency_score=scaled_score,
            reason=reason,
            provider="muninn-jev",
        )

    def _fail_open_fallback(self, severity: str, reason: str) -> TriageResult:
        """Deterministic fail-open fallback when Muninn Jev is unreachable for alerts."""
        if severity == "error":
            score = 8
            suppress = False
            channel = "alerts"
        elif severity == "warning":
            score = 5
            suppress = False
            channel = "alerts"
        else:
            score = 2
            suppress = False
            channel = "alerts"

        logger.info("Fail-open fallback applied for severity '%s': score=%d, suppress=%s", severity, score, suppress)
        return TriageResult(
            urgency_score=score,
            confidence=1.0,
            is_transient_noise=False,
            destination_channel=channel,
            suppress=suppress,
            reason=reason,
            provider="fail-open-fallback",
        )
