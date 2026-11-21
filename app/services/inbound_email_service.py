import base64
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.models import InboundEmail, InboundEmailAttachment
from app.models.schemas import (
    DiscordAlertRequest,
    EmailRouteVerdict,
    InboundEmailIngestResponse,
    InboundEmailWebhookPayload,
    RouteTargetType,
    TriageResult,
)
from app.services.discord_service import DiscordService
from app.services.triage_service import (
    VALID_ROUTE_TARGETS,
    JevRouterUnavailableError,
    TriageService,
)

logger = logging.getLogger("iris.inbound")

MAX_STORED_BODY_CHARS = 65536
MAX_RAW_EXCERPT_CHARS = 4000
PLUTUS_ROUTE_TARGETS = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_bank_statement",
}

_SKIP_HTML_TAGS = {"script", "style", "noscript", "head", "title"}
_BLOCK_HTML_TAGS = {
    "p",
    "div",
    "tr",
    "li",
    "br",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
}
_INLINE_PAD_TAGS = {"td", "th"}
_INVISIBLE_CHARS_RE = re.compile(r"[\u034f\u200b\u200c\u200d\u200e\u200f\u2007\u2060\u00ad\ufeff]+")
_INVISIBLE_CODEPOINTS = {
    0x034F,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x2007,
    0x2060,
    0x00AD,
    0xFEFF,
}
_INVISIBLE_ENTITY_NAMES = {"zwnj", "zwj", "lrm", "rlm", "shy"}
_ANGLE_TRACKING_URL_RE = re.compile(r"<https?://[^>]+>", re.IGNORECASE)
_LONG_TRACKING_URL_RE = re.compile(r"https?://\S{40,}", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_HTML_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_HTML_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_MAX_TRACKING_ATTR_LEN = 80


def _strip_invisible_and_tracking(text: str) -> str:
    """Remove invisible Unicode preheaders, tracking URLs, and redundant whitespace."""
    if not text:
        return ""
    cleaned = _INVISIBLE_CHARS_RE.sub("", text)
    cleaned = _ANGLE_TRACKING_URL_RE.sub("", cleaned)
    cleaned = _LONG_TRACKING_URL_RE.sub("", cleaned)
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in cleaned.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty)


class _PlainTextHTMLParser(HTMLParser):
    """Stdlib HTML-to-text extractor stripping scripts, styles, and markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0:
            if lowered in _BLOCK_HTML_TAGS:
                self._chunks.append("\n")
            elif lowered in _INLINE_PAD_TAGS:
                self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _SKIP_HTML_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0:
            if lowered in _BLOCK_HTML_TAGS:
                self._chunks.append("\n")
            elif lowered in _INLINE_PAD_TAGS:
                self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return _strip_invisible_and_tracking("".join(self._chunks))


class _HTMLStorageCleaner(HTMLParser):
    """Strip inline styles, tracking URLs, comments, and invisible spacers from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._skip_depth = 0
        self._chunks: List[str] = []

    def _format_attrs(self, attrs: list[tuple[str, Optional[str]]]) -> str:
        parts: List[str] = []
        for name, value in attrs:
            lowered = name.lower()
            if lowered == "style":
                continue
            if lowered in ("href", "src") and value is not None and len(value.strip()) > _MAX_TRACKING_ATTR_LEN:
                continue
            if value is None:
                parts.append(name)
            else:
                parts.append(f'{name}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(parts)) if parts else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth == 0:
            self._chunks.append(f"<{tag}{self._format_attrs(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered in ("script", "style"):
            return
        if self._skip_depth == 0:
            self._chunks.append(f"<{tag}{self._format_attrs(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in ("script", "style"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0:
            self._chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(_INVISIBLE_CHARS_RE.sub("", data))

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth != 0:
            return
        lowered = name.lower()
        if lowered in _INVISIBLE_ENTITY_NAMES:
            return
        if lowered == "nbsp":
            self._chunks.append(" ")
            return
        self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth != 0:
            return
        try:
            code = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
        except ValueError:
            self._chunks.append(f"&#{name};")
            return
        if code in _INVISIBLE_CODEPOINTS:
            return
        if code == 0x00A0:
            self._chunks.append(" ")
            return
        self._chunks.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def get_html(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in raw.splitlines()]
        non_empty = [line for line in lines if line]
        return "\n".join(non_empty)


def clean_html_for_storage(
    html_body: Optional[str],
    max_chars: int = MAX_STORED_BODY_CHARS,
) -> Optional[str]:
    """Strip bloated styles, scripts, head, comments, spacers, and long tracking URLs from HTML."""
    if html_body is None:
        return None
    stripped = _HTML_COMMENT_RE.sub("", html_body)
    stripped = _HTML_SCRIPT_RE.sub("", stripped)
    stripped = _HTML_STYLE_RE.sub("", stripped)
    stripped = _HTML_HEAD_RE.sub("", stripped)
    stripped = _INVISIBLE_CHARS_RE.sub("", stripped)

    parser = _HTMLStorageCleaner()
    parser.feed(stripped)
    parser.close()
    cleaned = parser.get_html().strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def sanitize_html_to_text(html_body: Optional[str]) -> str:
    """Strip HTML scripts, styles, and tags into normalized plain text."""
    if not html_body:
        return ""
    parser = _PlainTextHTMLParser()
    parser.feed(html_body)
    parser.close()
    return parser.get_text()


def build_raw_excerpt(
    text_body: Optional[str],
    html_body: Optional[str],
    max_chars: int = MAX_RAW_EXCERPT_CHARS,
) -> str:
    """Build bounded plain-text excerpt preferring cleaned text_body over sanitized HTML."""
    normalized = _strip_invisible_and_tracking(text_body or "")
    if not normalized:
        normalized = sanitize_html_to_text(html_body)
    return normalized[:max_chars]


def normalize_message_id(
    raw_message_id: Optional[str],
    from_address: str,
    subject: str,
    received_at: datetime,
    text_body: Optional[str],
) -> str:
    """Return trimmed RFC 5322 Message-ID or deterministic sha256 fallback."""
    cleaned = (raw_message_id or "").strip()
    if cleaned:
        if len(cleaned) <= 512:
            return cleaned
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    canonical = (
        f"{from_address.strip().lower()}\n"
        f"{subject.strip()}\n"
        f"{received_at.astimezone(timezone.utc).isoformat()}\n"
        f"{(text_body or '')[:4096]}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _ensure_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _truncate_body(value: Optional[str], max_chars: int = MAX_STORED_BODY_CHARS) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def _verdict_from_row(row: InboundEmail) -> Optional[EmailRouteVerdict]:
    if not row.jev_verdict:
        return None
    try:
        return EmailRouteVerdict.model_validate(row.jev_verdict)
    except Exception:
        return None


def _build_ingest_response(row: InboundEmail, status_label: Optional[str] = None, duplicate: bool = False) -> InboundEmailIngestResponse:
    return InboundEmailIngestResponse(
        status=status_label or row.dispatch_status,
        duplicate=duplicate,
        email_id=row.id,
        message_id=row.message_id,
        route_target=row.route_target,
        dispatch_status=row.dispatch_status,
        consumer_ref=row.consumer_ref,
        error_detail=row.error_detail,
        jev_verdict=_verdict_from_row(row),
    )


async def _notify_discord_best_effort(
    discord_service: DiscordService,
    title: str,
    details: str,
    severity: str = "error",
    channel: str = "alerts",
) -> None:
    """Send best-effort Discord notice without ever raising into caller."""
    try:
        triage_hint: Optional[TriageResult] = None
        if channel == "decisions":
            triage_hint = TriageResult(
                urgency_score=5,
                confidence=1.0,
                is_transient_noise=False,
                destination_channel="decisions",
                suppress=False,
                reason="Inbound email operator review notice",
                provider="iris-inbound",
            )
        req = DiscordAlertRequest(
            service="iris-inbound",
            bot_name="Iris Inbound Gateway",
            title=title[:120],
            details=details[:1500],
            severity=severity,
            destination_channel=channel,
            triage=False,
        )
        await discord_service.send_alert(req, triage_result=triage_hint)
    except Exception as exc:
        logger.warning("Best-effort Discord notice skipped/failed (%s): %s", title, exc)


async def _dispatch_to_plutus(email_row: InboundEmail) -> Tuple[int, str, Optional[str]]:
    """Dispatch routed financial email over aether-net to Plutus POST /integrations/email.

    Returns (http_status_code, dispatch_status, consumer_ref_or_error).
    """
    plutus_url = (settings.PLUTUS_API_URL or "").strip()
    plutus_secret = (settings.PLUTUS_WEBHOOK_SECRET or "").strip()
    if not plutus_url or not plutus_secret:
        missing = []
        if not plutus_url:
            missing.append("PLUTUS_API_URL")
        if not plutus_secret:
            missing.append("PLUTUS_WEBHOOK_SECRET")
        return 503, "failed", f"plutus_dispatch_misconfigured: missing {', '.join(missing)}"

    attachments_payload = [
        {
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": att.size_bytes,
            "sha256": att.sha256,
            "content_base64": att.content_base64,
        }
        for att in email_row.attachments
    ]

    payload = {
        "email_id": email_row.id,
        "message_id": email_row.message_id,
        "route_target": email_row.route_target,
        "from_address": email_row.from_address,
        "to_address": email_row.to_address,
        "subject": email_row.subject,
        "received_at": email_row.received_at.isoformat(),
        "text_body": email_row.text_body,
        "html_excerpt": email_row.html_excerpt,
        "raw_excerpt": email_row.raw_excerpt,
        "jev_verdict": email_row.jev_verdict,
        "attachments": attachments_payload,
    }
    headers = {
        "Authorization": f"Bearer {plutus_secret}",
        "Content-Type": "application/json",
        "User-Agent": "Yggdrasil-Iris/1.0",
    }
    target_url = f"{plutus_url.rstrip('/')}/integrations/email"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(target_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return 503, "failed", f"plutus_network_error: {exc}"

    if resp.status_code in (200, 201):
        try:
            body = resp.json()
        except Exception:
            body = {}
        consumer_ref = (
            body.get("consumer_ref")
            or body.get("transaction_id")
            or body.get("receipt_id")
            or body.get("status")
            or "dispatched"
        )
        return 200, "dispatched", str(consumer_ref)[:256]

    if resp.status_code == 422:
        return 200, "failed", f"plutus_validation_422: {resp.text[:500]}"

    return 503, "failed", f"plutus_upstream_{resp.status_code}: {resp.text[:500]}"


async def _dispatch_routed_email(
    db: Session,
    email_row: InboundEmail,
    route_target: RouteTargetType,
    has_actionable_amount: bool,
    manual_reroute: bool,
    discord_service: DiscordService,
) -> int:
    """Apply routing rules and downstream consumer dispatch, returning HTTP status code."""
    email_row.route_target = route_target

    if route_target == "ignore":
        email_row.dispatch_status = "ignored"
        email_row.error_detail = None
        db.commit()
        db.refresh(email_row)
        return 200

    if route_target == "actionable_notice":
        email_row.dispatch_status = "needs_review"
        email_row.error_detail = None
        db.commit()
        db.refresh(email_row)
        await _notify_discord_best_effort(
            discord_service,
            title=f"Actionable Notice: {email_row.subject or '(no subject)'}",
            details=f"Email #{email_row.id} from {email_row.from_address} marked actionable_notice.\n{email_row.raw_excerpt or ''}",
            severity="warning",
            channel="decisions",
        )
        return 200

    if route_target in PLUTUS_ROUTE_TARGETS:
        if route_target == "plutus_purchase_receipt" and not has_actionable_amount and not manual_reroute:
            email_row.dispatch_status = "needs_review"
            email_row.error_detail = "receipt_missing_actionable_amount"
            db.commit()
            db.refresh(email_row)
            await _notify_discord_best_effort(
                discord_service,
                title=f"Receipt Needs Review: {email_row.subject or '(no subject)'}",
                details=(
                    f"Email #{email_row.id} from {email_row.from_address} classified as plutus_purchase_receipt "
                    "without actionable monetary amount."
                ),
                severity="warning",
                channel="decisions",
            )
            return 200

        if not settings.IRIS_PLUTUS_DISPATCH_ENABLED:
            email_row.dispatch_status = "needs_review"
            email_row.error_detail = "plutus_dispatch_disabled_phase1_capture"
            db.commit()
            db.refresh(email_row)
            return 200

        status_code, new_status, detail = await _dispatch_to_plutus(email_row)
        email_row.dispatch_status = new_status
        if new_status == "dispatched":
            email_row.consumer_ref = detail
            email_row.error_detail = None
        else:
            email_row.error_detail = detail
        db.commit()
        db.refresh(email_row)

        if new_status == "failed":
            await _notify_discord_best_effort(
                discord_service,
                title=f"Plutus Dispatch Failed: Email #{email_row.id}",
                details=f"Route: {route_target}\nError: {detail}",
                severity="error",
                channel="alerts",
            )
        return status_code

    email_row.dispatch_status = "needs_review"
    email_row.error_detail = f"unsupported_route_target: {route_target}"
    db.commit()
    db.refresh(email_row)
    return 200


async def ingest_email(
    db: Session,
    payload: InboundEmailWebhookPayload,
    triage_service: Optional[TriageService] = None,
    discord_service: Optional[DiscordService] = None,
) -> Tuple[InboundEmailIngestResponse, int]:
    """Persist, classify, and dispatch an inbound email idempotently."""
    triage = triage_service or TriageService()
    discord = discord_service or DiscordService()

    received_at = _ensure_utc(payload.received_at)
    message_id = normalize_message_id(
        raw_message_id=payload.message_id,
        from_address=payload.from_address,
        subject=payload.subject,
        received_at=received_at,
        text_body=payload.text_body,
    )

    existing = db.scalar(
        select(InboundEmail).where(InboundEmail.message_id == message_id)
    )
    if existing is not None:
        return _build_ingest_response(existing, status_label="duplicate", duplicate=True), 200

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

    raw_excerpt = build_raw_excerpt(payload.text_body, payload.html_body)
    email_row = InboundEmail(
        message_id=message_id,
        from_address=payload.from_address.strip(),
        to_address=payload.to_address.strip(),
        subject=payload.subject or "",
        received_at=received_at,
        text_body=_truncate_body(payload.text_body),
        html_excerpt=clean_html_for_storage(payload.html_body),
        raw_excerpt=raw_excerpt,
        attachments_meta=attachments_meta,
        route_target="unclassified",
        dispatch_status="received",
        attachments=attachment_rows,
    )

    db.add(email_row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_after_race = db.scalar(
            select(InboundEmail).where(InboundEmail.message_id == message_id)
        )
        if existing_after_race is not None:
            return (
                _build_ingest_response(existing_after_race, status_label="duplicate", duplicate=True),
                200,
            )
        raise

    db.refresh(email_row)
    # Eagerly load attachments so Plutus dispatch has them ready
    _ = list(email_row.attachments)

    try:
        verdict = await triage.route_inbound_email(
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
        await _notify_discord_best_effort(
            discord,
            title=f"Jev Email Router Unavailable: Email #{email_row.id}",
            details=f"From: {email_row.from_address}\nSubject: {email_row.subject}\nError: {exc}",
            severity="error",
            channel="alerts",
        )
        return _build_ingest_response(email_row), 503

    email_row.jev_verdict = verdict.model_dump()
    email_row.route_target = verdict.route_target

    if verdict.confidence < settings.IRIS_ALERT_CONFIDENCE_THRESHOLD:
        email_row.dispatch_status = "needs_review"
        email_row.error_detail = (
            f"low_confidence: {verdict.confidence:.2f} < {settings.IRIS_ALERT_CONFIDENCE_THRESHOLD:.2f}"
        )
        db.commit()
        db.refresh(email_row)
        await _notify_discord_best_effort(
            discord,
            title=f"Low-Confidence Email Needs Review: {email_row.subject or '(no subject)'}",
            details=(
                f"Email #{email_row.id} from {email_row.from_address}\n"
                f"Suggested route: {verdict.route_target} (confidence {verdict.confidence:.2f})\n"
                f"Reason: {verdict.reason}"
            ),
            severity="warning",
            channel="decisions",
        )
        return _build_ingest_response(email_row), 200

    status_code = await _dispatch_routed_email(
        db=db,
        email_row=email_row,
        route_target=verdict.route_target,
        has_actionable_amount=verdict.has_actionable_amount,
        manual_reroute=False,
        discord_service=discord,
    )
    return _build_ingest_response(email_row), status_code


def get_email_with_attachments(db: Session, email_id: int) -> Optional[InboundEmail]:
    return db.scalar(
        select(InboundEmail)
        .options(selectinload(InboundEmail.attachments))
        .where(InboundEmail.id == email_id)
    )


def list_emails(
    db: Session,
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


def get_email_stats(db: Session) -> Tuple[int, Dict[str, int], Dict[str, int]]:
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


async def retry_email(
    db: Session,
    email_id: int,
    reclassify: bool = False,
    triage_service: Optional[TriageService] = None,
    discord_service: Optional[DiscordService] = None,
) -> Tuple[Optional[InboundEmail], int]:
    """Retry dispatching an inbound email, optionally re-running Jev classification."""
    email_row = get_email_with_attachments(db, email_id)
    if email_row is None:
        return None, 404

    triage = triage_service or TriageService()
    discord = discord_service or DiscordService()

    email_row.retry_count += 1
    email_row.error_detail = None

    needs_classify = reclassify or email_row.route_target not in VALID_ROUTE_TARGETS or not email_row.jev_verdict
    if needs_classify:
        if email_row.text_body is not None or email_row.html_excerpt is not None:
            email_row.raw_excerpt = build_raw_excerpt(email_row.text_body, email_row.html_excerpt)
        try:
            verdict = await triage.route_inbound_email(
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
            return email_row, 503

        email_row.jev_verdict = verdict.model_dump()
        email_row.route_target = verdict.route_target

        if reclassify and verdict.confidence < settings.IRIS_ALERT_CONFIDENCE_THRESHOLD:
            email_row.dispatch_status = "needs_review"
            email_row.error_detail = (
                f"low_confidence: {verdict.confidence:.2f} < {settings.IRIS_ALERT_CONFIDENCE_THRESHOLD:.2f}"
            )
            db.commit()
            db.refresh(email_row)
            return email_row, 200

        status_code = await _dispatch_routed_email(
            db=db,
            email_row=email_row,
            route_target=verdict.route_target,
            has_actionable_amount=verdict.has_actionable_amount,
            manual_reroute=not reclassify,
            discord_service=discord,
        )
        return email_row, status_code

    existing_route: RouteTargetType = email_row.route_target  # type: ignore[assignment]
    status_code = await _dispatch_routed_email(
        db=db,
        email_row=email_row,
        route_target=existing_route,
        has_actionable_amount=True,
        manual_reroute=True,
        discord_service=discord,
    )
    return email_row, status_code


async def reroute_email(
    db: Session,
    email_id: int,
    new_target: RouteTargetType,
    discord_service: Optional[DiscordService] = None,
) -> Tuple[Optional[InboundEmail], int]:
    """Manually re-route an email to a target consumer and dispatch."""
    email_row = get_email_with_attachments(db, email_id)
    if email_row is None:
        return None, 404

    discord = discord_service or DiscordService()
    email_row.retry_count += 1
    email_row.error_detail = None
    email_row.route_target = new_target

    status_code = await _dispatch_routed_email(
        db=db,
        email_row=email_row,
        route_target=new_target,
        has_actionable_amount=True,
        manual_reroute=True,
        discord_service=discord,
    )
    return email_row, status_code


def ignore_email(db: Session, email_id: int) -> Optional[InboundEmail]:
    """Mark an inbound email as ignored without dispatching."""
    email_row = get_email_with_attachments(db, email_id)
    if email_row is None:
        return None
    email_row.dispatch_status = "ignored"
    email_row.error_detail = None
    db.commit()
    db.refresh(email_row)
    return email_row
