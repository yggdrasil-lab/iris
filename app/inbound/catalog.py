from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from app.inbound.ports import ConsumerName, InvalidRouteTargetError
from app.models.schemas import RouteTargetType

GuardMode = Literal["amount_required", "amount_or_document"]


@dataclass(frozen=True)
class RouteSpec:
    target: RouteTargetType
    jev_criteria: str
    dispatches_to_plutus: bool = False
    fanout_targets: tuple[ConsumerName, ...] = ()
    guard_mode: Optional[GuardMode] = None
    guard_error_code: Optional[str] = None
    guard_title_prefix: Optional[str] = None
    guard_reason: Optional[str] = None


@dataclass(frozen=True)
class EvidenceGuardNotice:
    error_detail: Optional[str]
    title: str
    details: str


ROUTE_CATALOG: Dict[str, RouteSpec] = {
    "plutus_dbs_alert": RouteSpec(
        target="plutus_dbs_alert",
        jev_criteria=(
            "DBS Bank transaction alert (including iBanking Alerts, digibank Alert, or emails from ibanking.alert@dbs.com), "
            "card charge, PayNow, FAST transfer, bill payment, or GIRO notification"
        ),
        dispatches_to_plutus=True,
        fanout_targets=("plutus",),
    ),
    "plutus_purchase_receipt": RouteSpec(
        target="plutus_purchase_receipt",
        jev_criteria=(
            "E-commerce order confirmation (e.g., Amazon), tax invoice, flight itinerary or airline e-ticket receipt, "
            "hotel or travel booking payment confirmation or deposit receipt, or forwarded purchase receipt "
            "(containing an itemized or total spend, or emails with attached invoice/receipt PDFs or images such as TaxInvoice.pdf or SI_*.jpg)"
        ),
        dispatches_to_plutus=True,
        fanout_targets=("plutus",),
        guard_mode="amount_required",
        guard_error_code="receipt_missing_actionable_amount",
        guard_title_prefix="Receipt Needs Review",
        guard_reason="without actionable monetary amount",
    ),
    "plutus_payslip": RouteSpec(
        target="plutus_payslip",
        jev_criteria=(
            "Employer payroll notification, salary advice, or forwarded payslip email "
            "(e.g., 'Your Latest Payslip Is Attached' or an attached payslip.pdf / payroll document), "
            "even when the email body contains zero dollar figures"
        ),
        dispatches_to_plutus=True,
        fanout_targets=("plutus",),
        guard_mode="amount_or_document",
        guard_error_code="payslip_missing_attachment",
        guard_title_prefix="Payslip Needs Review",
        guard_reason="without attached payslip PDF or image",
    ),
    "plutus_bank_statement": RouteSpec(
        target="plutus_bank_statement",
        jev_criteria="Bank or credit card statement notification or attached statement PDF/CSV",
        dispatches_to_plutus=True,
        fanout_targets=("plutus",),
    ),
    "travel_booking": RouteSpec(
        target="travel_booking",
        jev_criteria=(
            "Non-payment travel booking or itinerary confirmation without a paid receipt total: "
            "flight itinerary, boarding pass, pay-at-property hotel reservation, car rental confirmation, "
            "rail or ferry ticket, activity or tour voucher, or travel insurance confirmation"
        ),
        dispatches_to_plutus=False,
        fanout_targets=("odysseus",),
    ),
    "actionable_notice": RouteSpec(
        target="actionable_notice",
        jev_criteria=(
            "Important non-bank notice requiring action without a paid purchase receipt or invoice, "
            "such as rego/WOF reminder, unpaid utility bill notice, medical result, "
            "flight schedule change or check-in reminder, or renewal"
        ),
        dispatches_to_plutus=False,
    ),
    "ignore": RouteSpec(
        target="ignore",
        jev_criteria="Shipping tracking update without purchase total, marketing promo, newsletter, OTP login code, or low-value noise",
        dispatches_to_plutus=False,
    ),
}

VALID_ROUTE_TARGETS: frozenset[str] = frozenset(ROUTE_CATALOG.keys())
PLUTUS_ROUTE_TARGETS: frozenset[str] = frozenset(
    name for name, spec in ROUTE_CATALOG.items() if spec.dispatches_to_plutus
)

_ACTIONABLE_AMOUNT_INSTRUCTIONS = (
    "True if a concrete monetary purchase or financial transaction amount with currency or dollar figure is present "
    "(including compact currency strings such as SGD2000.00, $179.74, or AUD amounts), "
    "if a payment or booking confirmation carries an invoice/receipt PDF or image attachment "
    "(e.g., TaxInvoice.pdf, SI_*.jpg) even if the exact monetary amount appears inside the attachment or later in a quoted thread, "
    "or if a payroll/payslip notification carries an attached payslip PDF or image "
    "(e.g., payslip.pdf) even when the net pay amount resides inside the PDF attachment; "
    "False if no concrete monetary transaction amount or invoice/receipt/payslip attachment is present."
)

_SCHEDULE_EVENT_INSTRUCTIONS = (
    "True if the email contains a calendar invitation, .ics attachment, scheduled appointment, "
    "travel or venue booking with a specific date/time (flight, hotel check-in, restaurant reservation, "
    "medical/dentist visit, event ticket, meeting), or an explicit scheduled deadline/due date; "
    "False if there is no concrete scheduled event or time-bound booking."
)

_TRAVEL_BOOKING_INSTRUCTIONS = (
    "True if the email is a travel reservation, booking confirmation, or travel document "
    "(flight itinerary, boarding pass, hotel or accommodation reservation, car rental, rail or ferry ticket, "
    "activity or tour voucher, or travel insurance confirmation) whether or not it includes a payment total; "
    "False if it is not a travel booking or trip reservation."
)

_SIGNIFICANCE_SCORE_CRITERIA = [
    "Marketing promo, newsletter, OTP code, or shipping tracking noise with no financial or action relevance",
    "Low-priority informational email without urgent financial or operator action",
    "Valid bank alert, purchase receipt, payslip, or financial statement ready for ingestion",
    "High-priority financial charge, large transfer, overdue bill, or urgent actionable notice",
]


def is_valid_route_target(target: Optional[str]) -> bool:
    """Return True when target is a registered Route Target in ROUTE_CATALOG."""
    return isinstance(target, str) and target in ROUTE_CATALOG


def get_route_spec(target: str) -> RouteSpec:
    """Look up a RouteSpec by target name, failing loud on unknown targets."""
    spec = ROUTE_CATALOG.get(target)
    if spec is None:
        raise InvalidRouteTargetError(f"Unsupported route_target: {target!r}")
    return spec


def build_inbound_email_questions() -> Dict[str, Any]:
    """Build the Jev System One decision schema from ROUTE_CATALOG."""
    return {
        "route_target": {
            "type": "choice",
            "criteria": {name: spec.jev_criteria for name, spec in ROUTE_CATALOG.items()},
        },
        "has_actionable_amount": {
            "type": "noul",
            "instructions": _ACTIONABLE_AMOUNT_INSTRUCTIONS,
        },
        "has_schedule_event": {
            "type": "noul",
            "instructions": _SCHEDULE_EVENT_INSTRUCTIONS,
        },
        "has_travel_booking": {
            "type": "noul",
            "instructions": _TRAVEL_BOOKING_INSTRUCTIONS,
        },
        "significance_score": {
            "type": "score",
            "criteria": list(_SIGNIFICANCE_SCORE_CRITERIA),
        },
    }


def build_inbound_email_state_text(
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


def evaluate_evidence_guard(
    route_target: RouteTargetType,
    *,
    email_id: int,
    from_address: str,
    subject: str,
    has_actionable_amount: bool,
    has_document_attachment: bool,
) -> Optional[EvidenceGuardNotice]:
    """Evaluate table-driven Evidence Guard rules for a Route Target."""
    spec = get_route_spec(route_target)
    if spec.guard_mode is None:
        return None

    missing_evidence = False
    if spec.guard_mode == "amount_required":
        missing_evidence = not has_actionable_amount
    elif spec.guard_mode == "amount_or_document":
        missing_evidence = not has_actionable_amount and not has_document_attachment

    if not missing_evidence:
        return None

    subject_label = subject or "(no subject)"
    return EvidenceGuardNotice(
        error_detail=spec.guard_error_code,
        title=f"{spec.guard_title_prefix}: {subject_label}",
        details=f"Email #{email_id} from {from_address} classified as {route_target} {spec.guard_reason}.",
    )
