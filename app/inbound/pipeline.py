import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.alerting import OutboundAlerter
from app.db.models import InboundEmail, InboundEmailAttachment, InboundEmailDelivery
from app.inbound.catalog import (
    build_inbound_email_questions,
    build_inbound_email_state_text,
    evaluate_evidence_guard,
    get_route_spec,
    is_valid_route_target,
)
from app.inbound.ports import (
    ConsumerAttachmentPayload,
    ConsumerDispatchPort,
    ConsumerDispatchRequest,
    ConsumerDispatchResult,
    ConsumerName,
    InboundActionResult,
    InboundConfig,
    InboundEmailNotFoundError,
)
from app.inbound.sanitizer import (
    build_raw_excerpt,
    clean_html_for_storage,
    ensure_utc,
    has_calendar_attachment,
    has_document_attachment,
    normalize_message_id,
    truncate_body,
)
from app.jev import (
    JevClientPort,
    JevRouterUnavailableError,
    parse_bool,
    parse_clamped_score,
    resolve_calibrated_confidence,
)
from app.models.schemas import (
    EmailRouteVerdict,
    InboundEmailDeliveryRead,
    InboundEmailIngestResponse,
    InboundEmailWebhookPayload,
    RouteTargetType,
)


@dataclass(frozen=True)
class _RoutingContext:
    route_target: RouteTargetType
    has_actionable_amount: bool
    has_schedule_event: bool
    route_confidence_ok: bool
    confidence_value: float
    verdict_reason: str
    manual_reroute: bool
    is_retry: bool
    notify_operator: bool
    has_travel_booking: bool = False


def _verdict_from_row(row: InboundEmail) -> Optional[EmailRouteVerdict]:
    if not row.jev_verdict:
        return None
    try:
        return EmailRouteVerdict.model_validate(row.jev_verdict)
    except Exception:
        return None


def build_ingest_response(result: InboundActionResult) -> InboundEmailIngestResponse:
    """Project a typed InboundActionResult onto the public webhook response schema."""
    row = result.email
    status_label = "duplicate" if result.duplicate else row.dispatch_status
    deliveries = [
        InboundEmailDeliveryRead.model_validate(item) for item in (row.deliveries or [])
    ]
    return InboundEmailIngestResponse(
        status=status_label,
        duplicate=result.duplicate,
        email_id=row.id,
        message_id=row.message_id,
        route_target=row.route_target,
        dispatch_status=row.dispatch_status,
        consumer_ref=row.consumer_ref,
        error_detail=row.error_detail,
        jev_verdict=_verdict_from_row(row),
        deliveries=deliveries,
    )


def _build_dispatch_request(email_row: InboundEmail) -> ConsumerDispatchRequest:
    attachments = tuple(
        ConsumerAttachmentPayload(
            filename=att.filename,
            content_type=att.content_type,
            size_bytes=att.size_bytes,
            sha256=att.sha256,
            content_base64=att.content_base64,
        )
        for att in email_row.attachments
    )
    return ConsumerDispatchRequest(
        email_id=email_row.id,
        message_id=email_row.message_id,
        route_target=email_row.route_target,
        from_address=email_row.from_address,
        to_address=email_row.to_address,
        subject=email_row.subject,
        received_at=email_row.received_at,
        text_body=email_row.text_body,
        html_excerpt=email_row.html_excerpt,
        raw_excerpt=email_row.raw_excerpt,
        jev_verdict=email_row.jev_verdict,
        attachments=attachments,
    )


def _build_inbound_email_row(
    payload: InboundEmailWebhookPayload,
    message_id: str,
    received_at: datetime,
) -> InboundEmail:
    attachments_meta: List[Dict[str, Any]] = []
    attachment_rows: List[InboundEmailAttachment] = []
    for att in payload.attachments:
        raw_bytes = base64.b64decode(att.content_base64, validate=True)
        size_bytes = att.size_bytes if att.size_bytes is not None else len(raw_bytes)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
        attachments_meta.append(
            {
                "filename": att.filename,
                "content_type": att.content_type,
                "size_bytes": size_bytes,
                "sha256": sha256_hex,
            }
        )
        attachment_rows.append(
            InboundEmailAttachment(
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=size_bytes,
                sha256=sha256_hex,
                content_base64=att.content_base64,
            )
        )
    return InboundEmail(
        message_id=message_id,
        from_address=payload.from_address.strip(),
        to_address=payload.to_address.strip(),
        subject=payload.subject or "",
        received_at=received_at,
        text_body=truncate_body(payload.text_body),
        html_excerpt=clean_html_for_storage(payload.html_body),
        raw_excerpt=build_raw_excerpt(payload.text_body, payload.html_body),
        attachments_meta=attachments_meta,
        route_target="unclassified",
        dispatch_status="received",
        attachments=attachment_rows,
    )


def _persist_inbound_email(
    db: Session,
    payload: InboundEmailWebhookPayload,
) -> Tuple[InboundEmail, bool]:
    received_at = ensure_utc(payload.received_at)
    message_id = normalize_message_id(
        raw_message_id=payload.message_id,
        from_address=payload.from_address,
        subject=payload.subject,
        received_at=received_at,
        text_body=payload.text_body,
    )
    existing_stmt = (
        select(InboundEmail)
        .options(selectinload(InboundEmail.deliveries), selectinload(InboundEmail.attachments))
        .where(InboundEmail.message_id == message_id)
    )
    existing = db.scalar(existing_stmt)
    if existing is not None:
        return existing, True

    email_row = _build_inbound_email_row(payload, message_id, received_at)
    db.add(email_row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_after_race = db.scalar(existing_stmt)
        if existing_after_race is not None:
            return existing_after_race, True
        raise
    db.refresh(email_row)
    _ = list(email_row.attachments)
    _ = list(email_row.deliveries)
    return email_row, False


def _resolve_schedule_detection(
    parsed: Mapping[str, Any],
    attachments_meta: List[Dict[str, Any]],
    confidence_threshold: float,
) -> Tuple[bool, Optional[float]]:
    if has_calendar_attachment(attachments_meta):
        return True, 1.0
    if not parse_bool(parsed.get("has_schedule_event", False)):
        return False, None
    schedule_confidence = resolve_calibrated_confidence(
        parsed, question_keys=("has_schedule_event",), context_label="email_schedule"
    )
    return schedule_confidence >= confidence_threshold, schedule_confidence


def _resolve_travel_detection(
    parsed: Mapping[str, Any],
    route_target: str,
    confidence_threshold: float,
) -> Tuple[bool, Optional[float]]:
    has_travel_flag = parse_bool(parsed.get("has_travel_booking", False))
    if not has_travel_flag and route_target != "travel_booking":
        return False, None
    question_keys = ("has_travel_booking",) if has_travel_flag else ("route_target",)
    travel_confidence = resolve_calibrated_confidence(
        parsed, question_keys=question_keys, context_label="email_travel"
    )
    return travel_confidence >= confidence_threshold, travel_confidence


_CONSUMER_ENABLED_ATTRS: tuple[Tuple[ConsumerName, str], ...] = (
    ("plutus", "plutus_dispatch_enabled"),
    ("chronos", "chronos_dispatch_enabled"),
    ("odysseus", "odysseus_dispatch_enabled"),
)

_DISABLED_REASON_ORDER: tuple[Tuple[ConsumerName, str], ...] = (
    ("plutus", "plutus_dispatch_disabled_phase1_capture"),
    ("odysseus", "odysseus_dispatch_disabled"),
    ("chronos", "chronos_dispatch_disabled"),
)


def _sync_email_scalar_status(
    email_row: InboundEmail,
    active_deliveries: Sequence[InboundEmailDelivery],
    failed_deliveries: Sequence[InboundEmailDelivery],
) -> None:
    if len(active_deliveries) == 1:
        email_row.consumer_ref = active_deliveries[0].consumer_ref
    else:
        labeled_refs = [
            f"{item.consumer}:{item.consumer_ref}"
            for item in active_deliveries
            if item.dispatch_status == "dispatched" and item.consumer_ref
        ]
        if labeled_refs:
            email_row.consumer_ref = ", ".join(labeled_refs)[:256]

    if not failed_deliveries:
        email_row.dispatch_status = "dispatched"
        email_row.error_detail = None
        return

    email_row.dispatch_status = "failed"
    if len(active_deliveries) == 1:
        email_row.error_detail = failed_deliveries[0].error_detail
    else:
        email_row.error_detail = "; ".join(
            f"{item.consumer}: {item.error_detail}"
            for item in failed_deliveries
            if item.error_detail
        )


def _reconstruct_secondary_flags(
    email_row: InboundEmail, target: str, cached: Optional[EmailRouteVerdict]
) -> Tuple[bool, bool]:
    if target == "ignore":
        return False, False
    has_sched = (
        has_calendar_attachment(email_row.attachments_meta)
        or bool(cached and cached.has_schedule_event)
        or any(item.consumer == "chronos" for item in email_row.deliveries)
    )
    has_travel = (
        target == "travel_booking"
        or bool(cached and cached.has_travel_booking)
        or any(item.consumer == "odysseus" for item in email_row.deliveries)
    )
    return has_sched, has_travel


def _format_failure_alert_title(email_id: int, failed: Sequence[InboundEmailDelivery]) -> str:
    if len(failed) == 1:
        return f"{failed[0].consumer.capitalize()} Dispatch Failed: Email #{email_id}"
    names = ", ".join(item.consumer for item in failed)
    return f"Consumer Dispatch Failed ({names}): Email #{email_id}"


class InboundEmailPipeline:
    """Deep domain pipeline for inbound email capture, Jev routing, and multi-target dispatch."""

    def __init__(
        self,
        *,
        config: InboundConfig,
        jev: JevClientPort,
        plutus: Optional[ConsumerDispatchPort] = None,
        chronos: Optional[ConsumerDispatchPort] = None,
        odysseus: Optional[ConsumerDispatchPort] = None,
        consumers: Optional[Mapping[str, ConsumerDispatchPort]] = None,
        alerter: OutboundAlerter,
    ) -> None:
        self._config = config
        self._jev = jev
        resolved_consumers: Dict[str, ConsumerDispatchPort] = dict(consumers or {})
        for name, adapter in (("plutus", plutus), ("chronos", chronos), ("odysseus", odysseus)):
            if adapter is not None:
                resolved_consumers.setdefault(name, adapter)
        self._consumers = resolved_consumers
        self._alerter = alerter

    async def classify_email(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        received_at: datetime | str,
        raw_excerpt: str,
        attachments_meta: List[Dict[str, Any]],
    ) -> EmailRouteVerdict:
        """Classify and route an inbound email via JevClientPort; fails loud on errors."""
        state_text = build_inbound_email_state_text(
            from_address, to_address, subject, received_at, raw_excerpt, attachments_meta
        )
        parsed = await self._jev.evaluate_questions(state_text, build_inbound_email_questions())
        route_target = parsed.get("route_target")
        if not isinstance(route_target, str) or not is_valid_route_target(route_target):
            raise JevRouterUnavailableError(
                f"Invalid or missing route_target from Muninn Jev: {route_target!r}"
            )
        has_amount = parse_bool(parsed.get("has_actionable_amount", False))
        score = parse_clamped_score(parsed.get("significance_score"), default=5)
        conf = resolve_calibrated_confidence(
            parsed, question_keys=("route_target", "has_actionable_amount"), context_label="email"
        )
        has_sched, sched_conf = _resolve_schedule_detection(
            parsed, attachments_meta, self._config.confidence_threshold
        )
        has_travel, travel_conf = _resolve_travel_detection(
            parsed, route_target, self._config.confidence_threshold
        )
        return EmailRouteVerdict(
            route_target=route_target,  # type: ignore[arg-type]
            has_actionable_amount=has_amount,
            has_schedule_event=has_sched,
            schedule_confidence=sched_conf,
            has_travel_booking=has_travel,
            travel_confidence=travel_conf,
            confidence=conf,
            urgency_score=score,
            reason=(
                f"Jev classified as {route_target} "
                f"(amount={has_amount}, schedule={has_sched}, travel={has_travel}, score={score}/10, conf={conf:.2f})"
            ),
            provider="muninn-jev",
        )

    async def _send_operator_notice(
        self, *, title: str, details: str, severity: Literal["error", "warning"], channel: str
    ) -> None:
        await self._alerter.send_operator_notice(
            service="iris-inbound",
            bot_name="Iris Inbound Gateway",
            title=title,
            details=details,
            severity=severity,
            destination_channel=channel,
        )

    async def _park_for_review(
        self,
        db: Session,
        email_row: InboundEmail,
        *,
        error_detail: Optional[str],
        title: str,
        details: str,
        notify_decisions: bool,
        delivery_unavailable: bool = False,
    ) -> InboundActionResult:
        email_row.dispatch_status = "needs_review"
        email_row.error_detail = error_detail
        db.commit()
        db.refresh(email_row)
        _ = list(email_row.deliveries)
        if notify_decisions:
            await self._send_operator_notice(
                title=title, details=details, severity="warning", channel="decisions"
            )
        return InboundActionResult(
            email=email_row, disposition="needs_review", delivery_unavailable=delivery_unavailable
        )

    def _build_low_confidence_block(
        self, email_row: InboundEmail, ctx: _RoutingContext
    ) -> Tuple[Optional[str], str, str]:
        threshold = self._config.confidence_threshold
        return (
            f"low_confidence: {ctx.confidence_value:.2f} < {threshold:.2f}",
            f"Low-Confidence Email Needs Review: {email_row.subject or '(no subject)'}",
            (
                f"Email #{email_row.id} from {email_row.from_address}\n"
                f"Suggested route: {ctx.route_target} (confidence {ctx.confidence_value:.2f})\n"
                f"Reason: {ctx.verdict_reason}"
            ),
        )

    def _resolve_plutus_block(
        self, email_row: InboundEmail, ctx: _RoutingContext
    ) -> Optional[Tuple[Optional[str], str, str]]:
        if ctx.manual_reroute:
            return None
        if not ctx.route_confidence_ok:
            return self._build_low_confidence_block(email_row, ctx)
        guard = evaluate_evidence_guard(
            ctx.route_target,
            email_id=email_row.id,
            from_address=email_row.from_address,
            subject=email_row.subject,
            has_actionable_amount=ctx.has_actionable_amount,
            has_document_attachment=has_document_attachment(email_row.attachments_meta),
        )
        return (guard.error_detail, guard.title, guard.details) if guard is not None else None

    def _ensure_delivery_row(
        self,
        db: Session,
        email_row: InboundEmail,
        consumer: str,
        existing: Optional[InboundEmailDelivery],
        *,
        is_retry: bool,
    ) -> InboundEmailDelivery:
        if existing is None:
            delivery = InboundEmailDelivery(
                email_id=email_row.id, consumer=consumer, dispatch_status="pending", retry_count=0
            )
            db.add(delivery)
            email_row.deliveries.append(delivery)
            return delivery
        if is_retry:
            existing.retry_count += 1
        return existing

    async def _dispatch_single_consumer(
        self,
        db: Session,
        email_row: InboundEmail,
        consumer: str,
        dispatch_request: ConsumerDispatchRequest,
        existing: Optional[InboundEmailDelivery],
        *,
        is_retry: bool,
    ) -> Tuple[InboundEmailDelivery, bool]:
        if existing is not None and existing.dispatch_status == "dispatched":
            return existing, False
        delivery = self._ensure_delivery_row(db, email_row, consumer, existing, is_retry=is_retry)
        adapter = self._consumers.get(consumer)
        result = (
            await adapter.dispatch_email(dispatch_request)
            if adapter is not None
            else ConsumerDispatchResult(
                status="unavailable",
                error_detail=f"{consumer}_dispatch_misconfigured: dispatcher not registered",
            )
        )
        if result.status == "dispatched":
            delivery.dispatch_status = "dispatched"
            delivery.consumer_ref = result.consumer_ref
            delivery.error_detail = None
            delivery.dispatched_at = datetime.now(timezone.utc)
            return delivery, False
        delivery.dispatch_status = "failed"
        delivery.error_detail = result.error_detail
        return delivery, (result.status == "unavailable")

    async def _fanout_to_consumers(
        self,
        db: Session,
        email_row: InboundEmail,
        consumers: Sequence[str],
        *,
        is_retry: bool,
        notify_operator: bool,
    ) -> Tuple[List[InboundEmailDelivery], List[InboundEmailDelivery], bool]:
        dispatch_request = _build_dispatch_request(email_row)
        existing_by_consumer = {item.consumer: item for item in (email_row.deliveries or [])}
        active: List[InboundEmailDelivery] = []
        failed: List[InboundEmailDelivery] = []
        has_transient = False

        for consumer in consumers:
            delivery, is_unavailable = await self._dispatch_single_consumer(
                db, email_row, consumer, dispatch_request,
                existing_by_consumer.get(consumer), is_retry=is_retry,
            )
            active.append(delivery)
            if delivery.dispatch_status != "dispatched":
                failed.append(delivery)
                has_transient = has_transient or is_unavailable

        if failed and notify_operator:
            details = f"Route: {email_row.route_target}\n" + "\n".join(
                f"{item.consumer}: {item.error_detail}" for item in failed
            )
            await self._send_operator_notice(
                title=_format_failure_alert_title(email_row.id, failed),
                details=details, severity="error", channel="alerts",
            )
        return active, failed, has_transient

    def _enabled_consumers(
        self,
        *,
        wants_plutus: bool,
        wants_chronos: bool,
        wants_odysseus: bool = False,
    ) -> List[str]:
        wanted: Dict[ConsumerName, bool] = {
            "plutus": wants_plutus,
            "chronos": wants_chronos,
            "odysseus": wants_odysseus,
        }
        return [
            consumer
            for consumer, config_attr in _CONSUMER_ENABLED_ATTRS
            if wanted[consumer] and getattr(self._config, config_attr)
        ]

    def _disabled_reason(
        self,
        *,
        wants_plutus: bool,
        wants_chronos: bool,
        wants_odysseus: bool,
    ) -> str:
        wanted: Dict[ConsumerName, bool] = {
            "plutus": wants_plutus,
            "chronos": wants_chronos,
            "odysseus": wants_odysseus,
        }
        for consumer, reason in _DISABLED_REASON_ORDER:
            if wanted[consumer]:
                return reason
        return "consumer_dispatch_disabled"

    async def _handle_non_consumer_route(
        self, db: Session, email_row: InboundEmail, ctx: _RoutingContext
    ) -> InboundActionResult:
        if not ctx.manual_reroute and not ctx.route_confidence_ok:
            block = self._resolve_plutus_block(email_row, ctx)
            assert block is not None
            return await self._park_for_review(
                db, email_row, error_detail=block[0], title=block[1],
                details=block[2], notify_decisions=ctx.notify_operator,
            )
        if ctx.route_target == "ignore":
            email_row.dispatch_status = "ignored"
            email_row.error_detail = None
            db.commit()
            db.refresh(email_row)
            return InboundActionResult(email=email_row, disposition="ignored")
        subject_label = email_row.subject or "(no subject)"
        return await self._park_for_review(
            db, email_row, error_detail=None, title=f"Actionable Notice: {subject_label}",
            details=(
                f"Email #{email_row.id} from {email_row.from_address} marked actionable_notice.\n"
                f"{email_row.raw_excerpt or ''}"
            ),
            notify_decisions=ctx.notify_operator,
        )

    async def _park_plutus_block(
        self,
        db: Session,
        email_row: InboundEmail,
        plutus_block: Tuple[Optional[str], str, str],
        ctx: _RoutingContext,
    ) -> InboundActionResult:
        secondary_consumers = self._enabled_consumers(
            wants_plutus=False,
            wants_chronos=ctx.has_schedule_event,
            wants_odysseus=ctx.has_travel_booking,
        )
        has_transient = False
        if secondary_consumers:
            _, _, has_transient = await self._fanout_to_consumers(
                db, email_row, secondary_consumers, is_retry=ctx.is_retry, notify_operator=ctx.notify_operator
            )
        return await self._park_for_review(
            db, email_row, error_detail=plutus_block[0], title=plutus_block[1],
            details=plutus_block[2], notify_decisions=ctx.notify_operator,
            delivery_unavailable=has_transient,
        )

    async def _apply_routing_and_dispatch(
        self, db: Session, email_row: InboundEmail, ctx: _RoutingContext
    ) -> InboundActionResult:
        spec = get_route_spec(ctx.route_target)
        email_row.route_target = spec.target
        wants_plutus = "plutus" in spec.fanout_targets
        wants_odysseus = ("odysseus" in spec.fanout_targets) or ctx.has_travel_booking
        wants_chronos = ctx.has_schedule_event
        if not wants_plutus and not wants_odysseus and not wants_chronos:
            return await self._handle_non_consumer_route(db, email_row, ctx)

        if wants_plutus:
            plutus_block = self._resolve_plutus_block(email_row, ctx)
            if plutus_block is not None:
                return await self._park_plutus_block(db, email_row, plutus_block, ctx)
        elif "odysseus" in spec.fanout_targets and not ctx.manual_reroute and not ctx.route_confidence_ok:
            low_conf_block = self._build_low_confidence_block(email_row, ctx)
            return await self._park_for_review(
                db, email_row, error_detail=low_conf_block[0], title=low_conf_block[1],
                details=low_conf_block[2], notify_decisions=ctx.notify_operator,
            )

        consumers = self._enabled_consumers(
            wants_plutus=wants_plutus, wants_chronos=wants_chronos, wants_odysseus=wants_odysseus
        )
        if not consumers:
            reason = self._disabled_reason(
                wants_plutus=wants_plutus, wants_chronos=wants_chronos, wants_odysseus=wants_odysseus
            )
            return await self._park_for_review(
                db, email_row, error_detail=reason, title="", details="", notify_decisions=False
            )

        active, failed, has_transient = await self._fanout_to_consumers(
            db, email_row, consumers, is_retry=ctx.is_retry, notify_operator=ctx.notify_operator
        )
        _sync_email_scalar_status(email_row, active, failed)
        db.commit()
        db.refresh(email_row)
        _ = list(email_row.deliveries)
        return InboundActionResult(
            email=email_row,
            disposition="failed" if failed else "dispatched",
            delivery_unavailable=has_transient,
        )

    async def _classify_and_dispatch(
        self,
        db: Session,
        email_row: InboundEmail,
        *,
        manual_reroute: bool,
        is_retry: bool,
        notify_operator: bool,
    ) -> InboundActionResult:
        try:
            verdict = await self.classify_email(
                from_address=email_row.from_address, to_address=email_row.to_address,
                subject=email_row.subject, received_at=email_row.received_at,
                raw_excerpt=email_row.raw_excerpt or "", attachments_meta=email_row.attachments_meta or [],
            )
        except JevRouterUnavailableError as exc:
            return await self._handle_jev_unavailable(db, email_row, exc, notify_operator)

        email_row.jev_verdict = verdict.model_dump()
        email_row.route_target = verdict.route_target
        ctx = _RoutingContext(
            route_target=verdict.route_target,
            has_actionable_amount=verdict.has_actionable_amount,
            has_schedule_event=verdict.has_schedule_event,
            has_travel_booking=verdict.has_travel_booking,
            route_confidence_ok=(verdict.confidence >= self._config.confidence_threshold),
            confidence_value=verdict.confidence,
            verdict_reason=verdict.reason,
            manual_reroute=manual_reroute,
            is_retry=is_retry,
            notify_operator=notify_operator,
        )
        return await self._apply_routing_and_dispatch(db, email_row, ctx)

    async def _handle_jev_unavailable(
        self, db: Session, email_row: InboundEmail, exc: Exception, notify_operator: bool
    ) -> InboundActionResult:
        email_row.dispatch_status = "failed"
        email_row.error_detail = f"jev_router_unavailable: {exc}"
        db.commit()
        db.refresh(email_row)
        if notify_operator:
            await self._send_operator_notice(
                title=f"Jev Email Router Unavailable: Email #{email_row.id}",
                details=f"From: {email_row.from_address}\nSubject: {email_row.subject}\nError: {exc}",
                severity="error", channel="alerts",
            )
        return InboundActionResult(email=email_row, disposition="failed", delivery_unavailable=True)

    async def ingest(self, db: Session, payload: InboundEmailWebhookPayload) -> InboundActionResult:
        """Persist, classify, and dispatch an inbound email idempotently."""
        email_row, is_duplicate = _persist_inbound_email(db, payload)
        if is_duplicate:
            return InboundActionResult(email=email_row, disposition="duplicate", duplicate=True)
        return await self._classify_and_dispatch(
            db, email_row, manual_reroute=False, is_retry=False, notify_operator=True
        )

    async def retry(
        self, db: Session, email_id: int, *, reclassify: bool = False
    ) -> InboundActionResult:
        """Retry dispatching an inbound email, optionally re-running Jev classification."""
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")
        email_row.retry_count += 1
        email_row.error_detail = None
        if reclassify or not is_valid_route_target(email_row.route_target) or not email_row.jev_verdict:
            if email_row.text_body is not None or email_row.html_excerpt is not None:
                email_row.raw_excerpt = build_raw_excerpt(email_row.text_body, email_row.html_excerpt)
            return await self._classify_and_dispatch(
                db, email_row, manual_reroute=False, is_retry=True, notify_operator=False
            )
        cached = _verdict_from_row(email_row)
        has_sched, has_travel = _reconstruct_secondary_flags(
            email_row, email_row.route_target, cached
        )
        ctx = _RoutingContext(
            route_target=email_row.route_target,  # type: ignore[arg-type]
            has_actionable_amount=True, has_schedule_event=has_sched,
            has_travel_booking=has_travel, route_confidence_ok=True,
            confidence_value=cached.confidence if cached else 1.0,
            verdict_reason=cached.reason if cached else "",
            manual_reroute=True, is_retry=True, notify_operator=False,
        )
        return await self._apply_routing_and_dispatch(db, email_row, ctx)

    async def reroute(
        self, db: Session, email_id: int, *, new_target: RouteTargetType
    ) -> InboundActionResult:
        """Manually override route_target and dispatch without Evidence Guard blocking."""
        spec = get_route_spec(new_target)
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")
        email_row.retry_count += 1
        email_row.error_detail = None
        cached = _verdict_from_row(email_row)
        has_sched, has_travel = _reconstruct_secondary_flags(email_row, spec.target, cached)
        ctx = _RoutingContext(
            route_target=spec.target, has_actionable_amount=True, has_schedule_event=has_sched,
            has_travel_booking=has_travel, route_confidence_ok=True, confidence_value=1.0,
            verdict_reason="Operator manual reroute",
            manual_reroute=True, is_retry=True, notify_operator=False,
        )
        return await self._apply_routing_and_dispatch(db, email_row, ctx)

    def ignore(self, db: Session, email_id: int) -> InboundActionResult:
        """Mark an inbound email as ignored without downstream dispatch or Discord notice."""
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")
        email_row.dispatch_status = "ignored"
        email_row.error_detail = None
        db.commit()
        db.refresh(email_row)
        _ = list(email_row.deliveries)
        return InboundActionResult(email=email_row, disposition="ignored")

    def get_email_with_attachments(self, db: Session, email_id: int) -> Optional[InboundEmail]:
        return db.scalar(
            select(InboundEmail)
            .options(
                selectinload(InboundEmail.attachments),
                selectinload(InboundEmail.deliveries),
            )
            .where(InboundEmail.id == email_id)
        )

    def list_emails(
        self,
        db: Session,
        *,
        status_filter: Optional[str] = None,
        route_target: Optional[str] = None,
        query_text: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> Tuple[List[InboundEmail], int]:
        stmt = select(InboundEmail).options(selectinload(InboundEmail.deliveries))
        count_stmt = select(func.count()).select_from(InboundEmail)

        conditions = []
        if status_filter and status_filter.strip() and status_filter.strip().lower() != "all":
            conditions.append(InboundEmail.dispatch_status == status_filter.strip())
        if route_target and route_target.strip() and route_target.strip().lower() != "all":
            conditions.append(InboundEmail.route_target == route_target.strip())
        if query_text and query_text.strip():
            pattern = f"%{query_text.strip()}%"
            conditions.append(
                or_(
                    InboundEmail.subject.ilike(pattern),
                    InboundEmail.from_address.ilike(pattern),
                    InboundEmail.to_address.ilike(pattern),
                    InboundEmail.raw_excerpt.ilike(pattern),
                    InboundEmail.consumer_ref.ilike(pattern),
                    InboundEmail.message_id.ilike(pattern),
                )
            )

        for cond in conditions:
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        total = int(db.scalar(count_stmt) or 0)
        offset = (page - 1) * page_size
        items = list(
            db.scalars(
                stmt.order_by(InboundEmail.received_at.desc(), InboundEmail.id.desc())
                .offset(offset)
                .limit(page_size)
            ).all()
        )
        return items, total

    def get_email_stats(self, db: Session) -> Tuple[int, Dict[str, int], Dict[str, int]]:
        total = int(db.scalar(select(func.count()).select_from(InboundEmail)) or 0)
        status_rows = db.execute(
            select(InboundEmail.dispatch_status, func.count()).group_by(InboundEmail.dispatch_status)
        ).all()
        target_rows = db.execute(
            select(InboundEmail.route_target, func.count()).group_by(InboundEmail.route_target)
        ).all()
        by_status = {str(k): int(v) for k, v in status_rows}
        by_route_target = {str(k): int(v) for k, v in target_rows}
        return total, by_status, by_route_target
