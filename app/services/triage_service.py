from datetime import datetime
from typing import Any, Dict, List, Optional

from app.alerting.triage import evaluate_alert_triage
from app.core.config import settings
from app.jev import (
    JevConfig,
    JevRouterUnavailableError,
    MuninnJevAdapter,
    parse_bool,
    parse_clamped_score,
    resolve_calibrated_confidence,
)
from app.models.schemas import EmailRouteVerdict, RouteTargetType, TriageResult

VALID_ROUTE_TARGETS: set[str] = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_payslip",
    "plutus_bank_statement",
    "actionable_notice",
    "ignore",
}

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
            "plutus_payslip": (
                "Employer payroll notification, salary advice, or forwarded payslip email "
                "(e.g., 'Your Latest Payslip Is Attached' or an attached payslip.pdf / payroll document), "
                "even when the email body contains zero dollar figures"
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
            "if a payment or booking confirmation carries an invoice/receipt PDF or image attachment "
            "(e.g., TaxInvoice.pdf, SI_*.jpg) even if the exact monetary amount appears inside the attachment or later in a quoted thread, "
            "or if a payroll/payslip notification carries an attached payslip PDF or image "
            "(e.g., payslip.pdf) even when the net pay amount resides inside the PDF attachment; "
            "False if no concrete monetary transaction amount or invoice/receipt/payslip attachment is present."
        ),
    },
    "significance_score": {
        "type": "score",
        "criteria": [
            "Marketing promo, newsletter, OTP code, or shipping tracking noise with no financial or action relevance",
            "Low-priority informational email without urgent financial or operator action",
            "Valid bank alert, purchase receipt, payslip, or financial statement ready for ingestion",
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


class TriageService:
    """Slice 3 temporary inbound email Jev router delegating transport to app.jev."""

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

    def _build_jev_adapter(self) -> MuninnJevAdapter:
        return MuninnJevAdapter(
            JevConfig(
                muninn_base=self.muninn_base,
                muninn_api_key=self.muninn_api_key,
                muninn_jev_model=self.muninn_jev_model,
                score_threshold=self.score_threshold,
                confidence_threshold=self.confidence_threshold,
            )
        )

    async def evaluate_alert(
        self,
        service: str,
        title: str,
        details: str,
        severity: str = "error",
        node: Optional[str] = None,
    ) -> TriageResult:
        """Delegate alert triage evaluation to app.alerting.triage via MuninnJevAdapter."""
        return await evaluate_alert_triage(
            self._build_jev_adapter(),
            service=service,
            title=title,
            details=details,
            severity=severity,
            node=node,
            score_threshold=self.score_threshold,
            confidence_threshold=self.confidence_threshold,
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
        parsed = await self._build_jev_adapter().evaluate_questions(state_text, _INBOUND_EMAIL_QUESTIONS)
        route_target = parsed.get("route_target")
        if not isinstance(route_target, str) or route_target not in VALID_ROUTE_TARGETS:
            raise JevRouterUnavailableError(
                f"Invalid or missing route_target from Muninn Jev: {route_target!r}"
            )
        has_actionable_amount = parse_bool(parsed.get("has_actionable_amount", False))
        scaled_score = parse_clamped_score(parsed.get("significance_score"), default=5)
        overall_confidence = resolve_calibrated_confidence(
            parsed, question_keys=("route_target", "has_actionable_amount"), context_label="email"
        )
        typed_target: RouteTargetType = route_target  # type: ignore[assignment]
        return EmailRouteVerdict(
            route_target=typed_target,
            has_actionable_amount=has_actionable_amount,
            confidence=overall_confidence,
            urgency_score=scaled_score,
            reason=(
                f"Jev classified as {route_target} "
                f"(amount={has_actionable_amount}, score={scaled_score}/10, conf={overall_confidence:.2f})"
            ),
            provider="muninn-jev",
        )
