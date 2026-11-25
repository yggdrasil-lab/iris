import base64
from datetime import datetime
import hashlib
from typing import Any, Dict, List, Literal, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.alerting import OutboundAlerter
from app.db.models import InboundEmail, InboundEmailAttachment
from app.inbound.catalog import (
    build_inbound_email_questions,
    build_inbound_email_state_text,
    evaluate_evidence_guard,
    get_route_spec,
    is_valid_route_target,
)
from app.inbound.ports import (
    InboundActionResult,
    InboundConfig,
    InboundEmailNotFoundError,
    PlutusAttachmentPayload,
    PlutusDispatchPort,
    PlutusDispatchRequest,
)
from app.inbound.sanitizer import (
    build_raw_excerpt,
    clean_html_for_storage,
    ensure_utc,
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
    InboundEmailIngestResponse,
    InboundEmailWebhookPayload,
    RouteTargetType,
)


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
    )


def _build_plutus_request(email_row: InboundEmail) -> PlutusDispatchRequest:
    attachments = tuple(
        PlutusAttachmentPayload(
            filename=att.filename,
            content_type=att.content_type,
            size_bytes=att.size_bytes,
            sha256=att.sha256,
            content_base64=att.content_base64,
        )
        for att in email_row.attachments
    )
    return PlutusDispatchRequest(
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
    existing = db.scalar(select(InboundEmail).where(InboundEmail.message_id == message_id))
    if existing is not None:
        return existing, True

    email_row = _build_inbound_email_row(payload, message_id, received_at)
    db.add(email_row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_after_race = db.scalar(
            select(InboundEmail).where(InboundEmail.message_id == message_id)
        )
        if existing_after_race is not None:
            return existing_after_race, True
        raise
    db.refresh(email_row)
    _ = list(email_row.attachments)
    return email_row, False


class InboundEmailPipeline:
    """Deep domain pipeline for inbound email capture, Jev routing, Evidence Guards, and Plutus dispatch."""

    def __init__(
        self,
        *,
        config: InboundConfig,
        jev: JevClientPort,
        plutus: PlutusDispatchPort,
        alerter: OutboundAlerter,
    ) -> None:
        self._config = config
        self._jev = jev
        self._plutus = plutus
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

    async def _send_operator_notice(
        self,
        *,
        title: str,
        details: str,
        severity: Literal["error", "warning"],
        channel: str,
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
    ) -> InboundActionResult:
        email_row.dispatch_status = "needs_review"
        email_row.error_detail = error_detail
        db.commit()
        db.refresh(email_row)
        if notify_decisions:
            await self._send_operator_notice(
                title=title,
                details=details,
                severity="warning",
                channel="decisions",
            )
        return InboundActionResult(email=email_row, disposition="needs_review")

    async def _dispatch_plutus_route(
        self,
        db: Session,
        email_row: InboundEmail,
        route_target: RouteTargetType,
        *,
        has_actionable_amount: bool,
        manual_reroute: bool,
        notify_operator: bool,
    ) -> InboundActionResult:
        if not manual_reroute:
            guard = evaluate_evidence_guard(
                route_target,
                email_id=email_row.id,
                from_address=email_row.from_address,
                subject=email_row.subject,
                has_actionable_amount=has_actionable_amount,
                has_document_attachment=has_document_attachment(email_row.attachments_meta),
            )
            if guard is not None:
                return await self._park_for_review(
                    db,
                    email_row,
                    error_detail=guard.error_detail,
                    title=guard.title,
                    details=guard.details,
                    notify_decisions=notify_operator,
                )

        if not self._config.plutus_dispatch_enabled:
            email_row.dispatch_status = "needs_review"
            email_row.error_detail = "plutus_dispatch_disabled_phase1_capture"
            db.commit()
            db.refresh(email_row)
            return InboundActionResult(email=email_row, disposition="needs_review")

        plutus_result = await self._plutus.dispatch_email(_build_plutus_request(email_row))
        if plutus_result.status == "dispatched":
            email_row.dispatch_status = "dispatched"
            email_row.consumer_ref = plutus_result.consumer_ref
            email_row.error_detail = None
            db.commit()
            db.refresh(email_row)
            return InboundActionResult(email=email_row, disposition="dispatched")

        email_row.dispatch_status = "failed"
        email_row.error_detail = plutus_result.error_detail
        db.commit()
        db.refresh(email_row)
        if notify_operator:
            await self._send_operator_notice(
                title=f"Plutus Dispatch Failed: Email #{email_row.id}",
                details=f"Route: {route_target}\nError: {plutus_result.error_detail}",
                severity="error",
                channel="alerts",
            )
        return InboundActionResult(
            email=email_row,
            disposition="failed",
            delivery_unavailable=(plutus_result.status == "unavailable"),
        )

    async def _apply_route_target(
        self,
        db: Session,
        email_row: InboundEmail,
        *,
        route_target: RouteTargetType,
        has_actionable_amount: bool,
        manual_reroute: bool,
        notify_operator: bool,
    ) -> InboundActionResult:
        spec = get_route_spec(route_target)
        email_row.route_target = spec.target

        if spec.target == "ignore":
            email_row.dispatch_status = "ignored"
            email_row.error_detail = None
            db.commit()
            db.refresh(email_row)
            return InboundActionResult(email=email_row, disposition="ignored")

        if spec.target == "actionable_notice":
            subject_label = email_row.subject or "(no subject)"
            return await self._park_for_review(
                db,
                email_row,
                error_detail=None,
                title=f"Actionable Notice: {subject_label}",
                details=(
                    f"Email #{email_row.id} from {email_row.from_address} marked actionable_notice.\n"
                    f"{email_row.raw_excerpt or ''}"
                ),
                notify_decisions=notify_operator,
            )

        return await self._dispatch_plutus_route(
            db,
            email_row,
            spec.target,
            has_actionable_amount=has_actionable_amount,
            manual_reroute=manual_reroute,
            notify_operator=notify_operator,
        )

    async def _classify_and_dispatch(
        self,
        db: Session,
        email_row: InboundEmail,
        *,
        manual_reroute: bool,
        notify_operator: bool,
    ) -> InboundActionResult:
        try:
            verdict = await self.classify_email(
                from_address=email_row.from_address,
                to_address=email_row.to_address,
                subject=email_row.subject,
                received_at=email_row.received_at,
                raw_excerpt=email_row.raw_excerpt or "",
                attachments_meta=email_row.attachments_meta or [],
            )
        except JevRouterUnavailableError as exc:
            email_row.dispatch_status = "failed"
            email_row.error_detail = f"jev_router_unavailable: {exc}"
            db.commit()
            db.refresh(email_row)
            if notify_operator:
                await self._send_operator_notice(
                    title=f"Jev Email Router Unavailable: Email #{email_row.id}",
                    details=f"From: {email_row.from_address}\nSubject: {email_row.subject}\nError: {exc}",
                    severity="error",
                    channel="alerts",
                )
            return InboundActionResult(email=email_row, disposition="failed", delivery_unavailable=True)

        email_row.jev_verdict = verdict.model_dump()
        email_row.route_target = verdict.route_target
        if verdict.confidence < self._config.confidence_threshold:
            threshold = self._config.confidence_threshold
            return await self._park_for_review(
                db,
                email_row,
                error_detail=f"low_confidence: {verdict.confidence:.2f} < {threshold:.2f}",
                title=f"Low-Confidence Email Needs Review: {email_row.subject or '(no subject)'}",
                details=(
                    f"Email #{email_row.id} from {email_row.from_address}\n"
                    f"Suggested route: {verdict.route_target} (confidence {verdict.confidence:.2f})\n"
                    f"Reason: {verdict.reason}"
                ),
                notify_decisions=notify_operator,
            )

        return await self._apply_route_target(
            db,
            email_row,
            route_target=verdict.route_target,
            has_actionable_amount=verdict.has_actionable_amount,
            manual_reroute=manual_reroute,
            notify_operator=notify_operator,
        )

    async def ingest(self, db: Session, payload: InboundEmailWebhookPayload) -> InboundActionResult:
        """Persist, classify, and dispatch an inbound email idempotently."""
        email_row, is_duplicate = _persist_inbound_email(db, payload)
        if is_duplicate:
            return InboundActionResult(email=email_row, disposition="duplicate", duplicate=True)
        return await self._classify_and_dispatch(
            db, email_row, manual_reroute=False, notify_operator=True
        )

    async def retry(
        self,
        db: Session,
        email_id: int,
        *,
        reclassify: bool = False,
    ) -> InboundActionResult:
        """Retry dispatching an inbound email, optionally re-running Jev classification."""
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")

        email_row.retry_count += 1
        email_row.error_detail = None
        needs_classify = (
            reclassify or not is_valid_route_target(email_row.route_target) or not email_row.jev_verdict
        )
        if needs_classify:
            if email_row.text_body is not None or email_row.html_excerpt is not None:
                email_row.raw_excerpt = build_raw_excerpt(email_row.text_body, email_row.html_excerpt)
            return await self._classify_and_dispatch(
                db, email_row, manual_reroute=False, notify_operator=False
            )

        existing_route: RouteTargetType = email_row.route_target  # type: ignore[assignment]
        return await self._apply_route_target(
            db,
            email_row,
            route_target=existing_route,
            has_actionable_amount=True,
            manual_reroute=True,
            notify_operator=False,
        )

    async def reroute(
        self,
        db: Session,
        email_id: int,
        *,
        new_target: RouteTargetType,
    ) -> InboundActionResult:
        """Manually override route_target and dispatch without Evidence Guard blocking."""
        spec = get_route_spec(new_target)
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")

        email_row.retry_count += 1
        email_row.error_detail = None
        return await self._apply_route_target(
            db,
            email_row,
            route_target=spec.target,
            has_actionable_amount=True,
            manual_reroute=True,
            notify_operator=False,
        )

    def ignore(self, db: Session, email_id: int) -> InboundActionResult:
        """Mark an inbound email as ignored without downstream dispatch or Discord notice."""
        email_row = self.get_email_with_attachments(db, email_id)
        if email_row is None:
            raise InboundEmailNotFoundError(f"Inbound email {email_id} not found")
        email_row.dispatch_status = "ignored"
        email_row.error_detail = None
        db.commit()
        db.refresh(email_row)
        return InboundActionResult(email=email_row, disposition="ignored")

    def get_email_with_attachments(self, db: Session, email_id: int) -> Optional[InboundEmail]:
        return db.scalar(
            select(InboundEmail)
            .options(selectinload(InboundEmail.attachments))
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
        stmt = select(InboundEmail)
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
