"""Unit tests for the deep app.inbound package (catalog, ports, Plutus adapter, pipeline)."""

import base64
from datetime import datetime, timezone
import json
from typing import Any, Callable, get_args

import httpx
import pytest
from sqlalchemy.orm import Session

from app.alerting import AlertingConfig, OutboundAlerter
from app.db.models import InboundEmail
from app.inbound import (
    ConsumerConfig,
    ConsumerDispatchPort,
    ConsumerDispatchRequest,
    ConsumerDispatchResult,
    HttpConsumerDispatchAdapter,
    HttpPlutusDispatchAdapter,
    InboundConfig,
    InboundEmailNotFoundError,
    InboundEmailPipeline,
    InvalidRouteTargetError,
    PLUTUS_ROUTE_TARGETS,
    PLUTUS_USER_AGENT,
    PlutusAttachmentPayload,
    PlutusConfig,
    PlutusDispatchPort,
    PlutusDispatchRequest,
    PlutusDispatchResult,
    ROUTE_CATALOG,
    VALID_ROUTE_TARGETS,
    build_inbound_email_questions,
    evaluate_evidence_guard,
    get_route_spec,
    has_calendar_attachment,
    has_document_attachment,
)
from app.jev import JevConfig, JevRouterUnavailableError, MuninnJevAdapter
from app.models.schemas import InboundEmailWebhookPayload, RouteTargetType
from app.tests.conftest import IrisTestHarness
from app.tests.fakes import (
    FakeClock,
    InMemoryConsumerDispatcher,
    InMemoryDiscordTransport,
    InMemoryJevClient,
    InMemoryPlutusDispatcher,
)


def _make_pipeline(
    *,
    confidence_threshold: float = 0.80,
    plutus_dispatch_enabled: bool = True,
    jev: InMemoryJevClient | MuninnJevAdapter | None = None,
    plutus: InMemoryPlutusDispatcher | HttpPlutusDispatchAdapter | None = None,
    discord: InMemoryDiscordTransport | None = None,
) -> tuple[InboundEmailPipeline, InMemoryJevClient | MuninnJevAdapter, InMemoryPlutusDispatcher | HttpPlutusDispatchAdapter, InMemoryDiscordTransport]:
    resolved_jev = jev or InMemoryJevClient()
    resolved_plutus = plutus or InMemoryPlutusDispatcher()
    resolved_discord = discord or InMemoryDiscordTransport()
    alerter = OutboundAlerter(
        config=AlertingConfig(
            bot_token="discord-bot-token",
            alert_channel_id="ch-alert",
            info_channel_id="ch-info",
            health_channel_id="ch-health",
            wealth_channel_id="ch-wealth",
            default_user_id="user-1",
            confidence_threshold=confidence_threshold,
        ),
        discord=resolved_discord,
        jev=resolved_jev,
        clock=FakeClock(),
    )
    pipeline = InboundEmailPipeline(
        config=InboundConfig(
            confidence_threshold=confidence_threshold,
            plutus_dispatch_enabled=plutus_dispatch_enabled,
        ),
        jev=resolved_jev,
        plutus=resolved_plutus,
        alerter=alerter,
    )
    return pipeline, resolved_jev, resolved_plutus, resolved_discord


def test_route_catalog_integrity_and_plutus_targets() -> None:
    assert VALID_ROUTE_TARGETS == set(get_args(RouteTargetType))
    assert set(ROUTE_CATALOG.keys()) == set(get_args(RouteTargetType))
    assert PLUTUS_ROUTE_TARGETS == {
        "plutus_dbs_alert",
        "plutus_purchase_receipt",
        "plutus_payslip",
        "plutus_bank_statement",
    }
    assert get_route_spec("actionable_notice").dispatches_to_plutus is False
    assert get_route_spec("ignore").dispatches_to_plutus is False
    with pytest.raises(InvalidRouteTargetError, match="Unsupported route_target"):
        get_route_spec("nonexistent_target")


def test_inbound_email_questions_cover_travel_receipts_payslips_and_attachments() -> None:
    questions = build_inbound_email_questions()
    route_criteria = questions["route_target"]["criteria"]
    receipt_criteria = route_criteria["plutus_purchase_receipt"]
    notice_criteria = route_criteria["actionable_notice"]
    payslip_criteria = route_criteria["plutus_payslip"]
    amount_instructions = questions["has_actionable_amount"]["instructions"]

    assert "flight itinerary" in receipt_criteria.lower()
    assert "airline e-ticket" in receipt_criteria.lower()
    assert "hotel" in receipt_criteria.lower()
    assert "payment confirmation" in receipt_criteria.lower()
    assert "deposit receipt" in receipt_criteria.lower()
    assert "tax invoice" in receipt_criteria.lower()
    assert "e-commerce order confirmation" in receipt_criteria.lower()
    assert "forwarded purchase receipt" in receipt_criteria.lower()
    assert "TaxInvoice.pdf" in receipt_criteria
    assert "SI_*.jpg" in receipt_criteria

    assert "travel booking" not in notice_criteria.lower()
    assert "without a paid purchase receipt" in notice_criteria.lower()
    assert "flight schedule change" in notice_criteria.lower()
    assert "check-in reminder" in notice_criteria.lower()

    assert "payroll" in payslip_criteria.lower()
    assert "salary" in payslip_criteria.lower()
    assert "Your Latest Payslip Is Attached" in payslip_criteria
    assert "payslip.pdf" in payslip_criteria
    assert "zero dollar" in payslip_criteria.lower()

    assert "TaxInvoice.pdf" in amount_instructions
    assert "SI_*.jpg" in amount_instructions
    assert "payslip" in amount_instructions.lower()
    assert "payslip.pdf" in amount_instructions
    assert "net pay" in amount_instructions.lower()
    assert "attachment" in amount_instructions.lower()
    assert "quoted thread" in amount_instructions.lower()

    tier3_criteria = questions["significance_score"]["criteria"][2]
    assert "payslip" in tier3_criteria.lower()


def test_evidence_guard_and_document_attachment_detection() -> None:
    assert has_document_attachment([]) is False
    assert has_document_attachment([{"filename": "notes.txt", "content_type": "text/plain"}]) is False
    assert has_document_attachment([{"filename": "doc.pdf", "content_type": "application/pdf"}]) is True
    assert has_document_attachment([{"filename": "PAYSLIP.PDF", "content_type": "application/octet-stream"}]) is True
    assert has_document_attachment([{"filename": "scan.jpg", "content_type": "image/jpeg; charset=binary"}]) is True

    receipt_guard = evaluate_evidence_guard(
        "plutus_purchase_receipt",
        email_id=10,
        from_address="orders@shop.example",
        subject="Order Shipped",
        has_actionable_amount=False,
        has_document_attachment=True,
    )
    assert receipt_guard is not None
    assert receipt_guard.error_detail == "receipt_missing_actionable_amount"

    payslip_guard = evaluate_evidence_guard(
        "plutus_payslip",
        email_id=11,
        from_address="payroll@acme.example",
        subject="Portal ready",
        has_actionable_amount=False,
        has_document_attachment=False,
    )
    assert payslip_guard is not None
    assert payslip_guard.error_detail == "payslip_missing_attachment"

    assert (
        evaluate_evidence_guard(
            "plutus_payslip",
            email_id=12,
            from_address="payroll@acme.example",
            subject="Payslip attached",
            has_actionable_amount=False,
            has_document_attachment=True,
        )
        is None
    )
    assert (
        evaluate_evidence_guard(
            "plutus_dbs_alert",
            email_id=13,
            from_address="alerts@bank.example",
            subject="Alert",
            has_actionable_amount=False,
            has_document_attachment=False,
        )
        is None
    )


@pytest.mark.asyncio
async def test_pipeline_classify_email_rejects_bad_json_or_unknown_target() -> None:
    bad_payloads = ["not-json", '{"route_target":"unknown_route"}']

    def handler(request: httpx.Request) -> httpx.Response:
        content = bad_payloads.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    adapter = MuninnJevAdapter(
        JevConfig(muninn_base="http://muninn.local/v1", muninn_api_key="muninn-master-key"),
        transport=httpx.MockTransport(handler),
    )
    pipeline, _, _, _ = _make_pipeline(jev=adapter)

    with pytest.raises(JevRouterUnavailableError, match="Invalid Muninn Jev JSON"):
        await pipeline.classify_email(
            from_address="a@b.com",
            to_address="iris@b.com",
            subject="Subj",
            received_at="2026-11-20T00:00:00Z",
            raw_excerpt="body",
            attachments_meta=[],
        )

    with pytest.raises(JevRouterUnavailableError, match="Invalid or missing route_target"):
        await pipeline.classify_email(
            from_address="a@b.com",
            to_address="iris@b.com",
            subject="Subj",
            received_at="2026-11-20T00:00:00Z",
            raw_excerpt="body",
            attachments_meta=[],
        )


@pytest.mark.asyncio
async def test_pipeline_classify_email_sends_prompt_criteria_for_dbs_travel_and_payslip() -> None:
    captured: list[dict[str, Any]] = []
    responses = [
        {
            "route_target": "plutus_dbs_alert",
            "has_actionable_amount": "true",
            "significance_score": "8",
            "_confidence": 0.94,
        },
        {
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "significance_score": 7,
            "_question_confidence": {
                "route_target": 0.95,
                "has_actionable_amount": 0.91,
                "significance_score": 0.90,
            },
        },
        {
            "route_target": "plutus_payslip",
            "has_actionable_amount": True,
            "significance_score": 8,
            "_question_confidence": {
                "route_target": 0.96,
                "has_actionable_amount": 0.92,
                "significance_score": 0.90,
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(responses.pop(0))}}]},
        )

    adapter = MuninnJevAdapter(
        JevConfig(muninn_base="http://muninn.local/v1", muninn_api_key="muninn-master-key"),
        transport=httpx.MockTransport(handler),
    )
    pipeline, _, _, _ = _make_pipeline(jev=adapter)

    dbs_verdict = await pipeline.classify_email(
        from_address="ibanking.alert@dbs.com",
        to_address="iris@example.com",
        subject="iBanking Alerts - FAST Transfer",
        received_at="2026-11-20T08:00:00Z",
        raw_excerpt="Amount: SGD2000.00 to Example Recipient",
        attachments_meta=[],
    )
    assert dbs_verdict.route_target == "plutus_dbs_alert"
    assert dbs_verdict.has_actionable_amount is True
    assert dbs_verdict.confidence == pytest.approx(0.94)
    assert dbs_verdict.urgency_score == 8

    system_content = captured[0]["messages"][0]["content"]
    assert "iBanking Alerts" in system_content
    assert "digibank Alert" in system_content
    assert "ibanking.alert@dbs.com" in system_content
    assert "SGD2000.00" in system_content
    assert "$179.74" in system_content

    travel_verdict = await pipeline.classify_email(
        from_address="reservations@example-hotel.test",
        to_address="iris@example.com",
        subject="Hotel Reservation Deposit Confirmation",
        received_at="2026-11-21T02:00:00Z",
        raw_excerpt="Thank you for your deposit payment. See attached invoice image.",
        attachments_meta=[
            {"filename": "SI_99881.jpg", "content_type": "image/jpeg", "size_bytes": 480000}
        ],
    )
    assert travel_verdict.route_target == "plutus_purchase_receipt"
    assert travel_verdict.confidence == pytest.approx(0.91)
    assert "SI_99881.jpg" in captured[1]["messages"][1]["content"]

    payslip_verdict = await pipeline.classify_email(
        from_address="alex.tan@example.com",
        to_address="iris@example.com",
        subject="Fwd: Your Latest Payslip Is Attached",
        received_at="2026-11-22T01:00:00Z",
        raw_excerpt="Forwarded message from Acme Payroll. Hi Alex Tan, your latest payslip is attached.",
        attachments_meta=[
            {"filename": "payslip.pdf", "content_type": "application/pdf", "size_bytes": 20480}
        ],
    )
    assert payslip_verdict.route_target == "plutus_payslip"
    assert payslip_verdict.confidence == pytest.approx(0.92)
    assert "payslip.pdf" in captured[2]["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_confidence"),
    [
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": "true",
                "significance_score": "7",
                "_confidence": 0.68,
                "_question_confidence": {
                    "route_target": 1.0,
                    "has_actionable_amount": 0.98,
                    "significance_score": 0.63,
                },
            },
            0.98,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": "true",
                "significance_score": "9",
                "_confidence": 0.72,
                "_question_confidence": {
                    "route_target": 0.72,
                    "has_actionable_amount": 0.96,
                    "significance_score": 0.95,
                },
            },
            0.72,
        ),
        (
            {
                "route_target": "plutus_purchase_receipt",
                "has_actionable_amount": "false",
                "significance_score": "8",
                "_confidence": 0.65,
                "_question_confidence": {
                    "route_target": 0.96,
                    "has_actionable_amount": 0.65,
                    "significance_score": 0.95,
                },
            },
            0.65,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.88,
            },
            0.88,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.83,
                "_question_confidence": ["unexpected"],
            },
            0.83,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.77,
                "_question_confidence": {"route_target": 0.99},
            },
            0.77,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.74,
                "_question_confidence": {"route_target": 0.99, "has_actionable_amount": None},
            },
            0.74,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
            },
            0.0,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.90,
                "_question_confidence": {"route_target": "not-a-number", "has_actionable_amount": 0.95},
            },
            0.0,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.90,
                "_question_confidence": {"route_target": float("inf"), "has_actionable_amount": 0.95},
            },
            0.0,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.90,
                "_question_confidence": {"route_target": "-inf", "has_actionable_amount": "nan"},
            },
            0.0,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.90,
                "_question_confidence": {"route_target": 0.0, "has_actionable_amount": 0.95},
            },
            0.0,
        ),
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.50,
                "_question_confidence": {"route_target": 1.4, "has_actionable_amount": "0.91"},
            },
            0.91,
        ),
    ],
)
async def test_pipeline_classify_email_confidence_calibration_and_edge_cases(
    payload: dict[str, Any],
    expected_confidence: float,
) -> None:
    pipeline, _, _, _ = _make_pipeline(jev=InMemoryJevClient(default_response=payload))
    verdict = await pipeline.classify_email(
        from_address="sender@example.com",
        to_address="iris@example.com",
        subject="Subject",
        received_at="2026-11-20T08:00:00Z",
        raw_excerpt="Body",
        attachments_meta=[],
    )
    assert verdict.confidence == pytest.approx(expected_confidence)


def test_in_memory_plutus_dispatcher_satisfies_protocol() -> None:
    assert isinstance(InMemoryPlutusDispatcher(), PlutusDispatchPort)


@pytest.mark.asyncio
async def test_http_plutus_dispatch_adapter_statuses_headers_and_errors() -> None:
    captured: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    responses: list[httpx.Response | Exception] = [
        httpx.Response(201, json={"receipt_id": 501}),
        httpx.Response(422, text="missing merchant"),
        httpx.Response(503, text="db busy"),
        httpx.ConnectError("connection refused"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                str(request.url),
                dict(request.headers),
                json.loads(request.content.decode("utf-8")),
            )
        )
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    adapter = HttpPlutusDispatchAdapter(
        PlutusConfig(
            api_url="http://plutus-api:8000/",
            webhook_secret="plutus-secret-token",
            timeout_seconds=120.0,
        ),
        transport=httpx.MockTransport(handler),
    )
    assert adapter._build_client().timeout.read == 120.0

    req = PlutusDispatchRequest(
        email_id=101,
        message_id="<adapter-1@example.com>",
        route_target="plutus_purchase_receipt",
        from_address="reservations@example-hotel.test",
        to_address="iris@example.com",
        subject="Hotel Deposit Receipt",
        received_at=datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc),
        text_body="Deposit Paid: PHP 11,700.00",
        html_excerpt=None,
        raw_excerpt="Deposit Paid: PHP 11,700.00",
        jev_verdict={"route_target": "plutus_purchase_receipt"},
        attachments=(
            PlutusAttachmentPayload(
                filename="invoice.pdf",
                content_type="application/pdf",
                size_bytes=4,
                sha256="a" * 64,
                content_base64=base64.b64encode(b"test").decode("ascii"),
            ),
        ),
    )

    res_ok = await adapter.dispatch_email(req)
    assert res_ok == PlutusDispatchResult(status="dispatched", consumer_ref="501")
    assert captured[0][0] == "http://plutus-api:8000/integrations/email"
    assert captured[0][1]["authorization"] == "Bearer plutus-secret-token"
    assert captured[0][1]["user-agent"] == PLUTUS_USER_AGENT
    assert captured[0][2]["attachments"][0]["filename"] == "invoice.pdf"

    res_422 = await adapter.dispatch_email(req)
    assert res_422.status == "rejected"
    assert "plutus_validation_422" in (res_422.error_detail or "")

    res_503 = await adapter.dispatch_email(req)
    assert res_503.status == "unavailable"
    assert "plutus_upstream_503" in (res_503.error_detail or "")

    res_net = await adapter.dispatch_email(req)
    assert res_net.status == "unavailable"
    assert "plutus_network_error" in (res_net.error_detail or "")

    unconfigured = HttpPlutusDispatchAdapter(
        PlutusConfig(api_url="", webhook_secret=None),
    )
    res_unconf = await unconfigured.dispatch_email(req)
    assert res_unconf.status == "unavailable"
    assert res_unconf.error_detail == "plutus_dispatch_misconfigured: missing PLUTUS_API_URL, PLUTUS_WEBHOOK_SECRET"


@pytest.mark.asyncio
async def test_uniform_low_confidence_policy_across_ingest_and_retry(
    db_session: Session,
) -> None:
    discord = InMemoryDiscordTransport()
    jev = InMemoryJevClient(
        responses=[
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 8,
                "_confidence": 0.62,
            },
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 8,
                "_confidence": 0.64,
            },
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 8,
                "_confidence": 0.61,
            },
        ]
    )
    plutus = InMemoryPlutusDispatcher()
    pipeline, _, _, _ = _make_pipeline(
        confidence_threshold=0.80,
        plutus_dispatch_enabled=True,
        jev=jev,
        plutus=plutus,
        discord=discord,
    )

    # 1. Automatic ingest parks low confidence AND fires Discord #decisions alert
    ingest_res = await pipeline.ingest(
        db_session,
        InboundEmailWebhookPayload(
            message_id="<uniform-low-1@example.com>",
            from_address="alerts@bank.example",
            to_address="iris@example.com",
            subject="Low confidence ingest",
            text_body="SGD 10.00",
        ),
    )
    assert ingest_res.disposition == "needs_review"
    assert ingest_res.email.dispatch_status == "needs_review"
    assert ingest_res.email.error_detail == "low_confidence: 0.62 < 0.80"
    assert len(discord.posts) == 1
    assert discord.posts[0].channel_id == "ch-info"

    # 2. Manual retry(reclassify=True) parks low confidence with ZERO new Discord alerts
    retry_reclass = await pipeline.retry(db_session, ingest_res.email.id, reclassify=True)
    assert retry_reclass.disposition == "needs_review"
    assert retry_reclass.email.error_detail == "low_confidence: 0.64 < 0.80"
    assert len(discord.posts) == 1

    # 3. Manual retry(reclassify=False) on unclassified row ALSO enforces low-confidence uniformly
    unclassified_row = InboundEmail(
        message_id="<unclassified-retry@example.com>",
        from_address="alerts@bank.example",
        to_address="iris@example.com",
        subject="Unclassified after Jev outage",
        received_at=datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc),
        text_body="SGD 45.00",
        raw_excerpt="SGD 45.00",
        attachments_meta=[],
        route_target="unclassified",
        dispatch_status="failed",
    )
    db_session.add(unclassified_row)
    db_session.commit()
    db_session.refresh(unclassified_row)

    retry_unclassified = await pipeline.retry(db_session, unclassified_row.id, reclassify=False)
    assert retry_unclassified.disposition == "needs_review"
    assert retry_unclassified.email.error_detail == "low_confidence: 0.61 < 0.80"
    assert len(discord.posts) == 1
    assert plutus.requests == []


@pytest.mark.asyncio
async def test_pipeline_errors_and_reroute_bypass_guard(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    pipeline, _, plutus, discord = _make_pipeline(plutus_dispatch_enabled=True)

    with pytest.raises(InboundEmailNotFoundError):
        await pipeline.retry(db_session, 999999)
    with pytest.raises(InboundEmailNotFoundError):
        await pipeline.reroute(db_session, 999999, new_target="ignore")
    with pytest.raises(InboundEmailNotFoundError):
        pipeline.ignore(db_session, 999999)
    with pytest.raises(InvalidRouteTargetError):
        await pipeline.reroute(db_session, 1, new_target="bad_route")  # type: ignore[arg-type]

    row = InboundEmail(
        message_id="<guard-bypass@example.com>",
        from_address="orders@shop.example",
        to_address="iris@example.com",
        subject="Order without body amount",
        received_at=datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc),
        raw_excerpt="See invoice attached",
        attachments_meta=[],
        route_target="actionable_notice",
        dispatch_status="needs_review",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    reroute_res = await pipeline.reroute(
        db_session, row.id, new_target="plutus_purchase_receipt"
    )
    assert reroute_res.disposition == "dispatched"
    assert reroute_res.email.dispatch_status == "dispatched"
    assert len(plutus.requests) == 1
    assert discord.posts == []

    # Invalid route_target passed to HTTP API is rejected (422 schema) or 400 domain error
    h = make_harness()
    bad_api = h.client.post(f"/api/emails/{row.id}/reroute", json={"route_target": "invalid"})
    assert bad_api.status_code == 422


@pytest.mark.asyncio
async def test_pipeline_ingest_actionable_notice_and_plutus_failure_notifications(
    db_session: Session,
) -> None:
    discord = InMemoryDiscordTransport()
    jev = InMemoryJevClient(
        responses=[
            {
                "route_target": "actionable_notice",
                "has_actionable_amount": False,
                "significance_score": 7,
                "_confidence": 0.93,
            },
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 9,
                "_confidence": 0.97,
            },
        ]
    )
    plutus = InMemoryPlutusDispatcher(
        default_result=PlutusDispatchResult(
            status="unavailable",
            error_detail="plutus_upstream_503: unavailable",
        )
    )
    pipeline, _, _, _ = _make_pipeline(
        plutus_dispatch_enabled=True,
        jev=jev,
        plutus=plutus,
        discord=discord,
    )

    notice_res = await pipeline.ingest(
        db_session,
        InboundEmailWebhookPayload(
            message_id="<notice-1@example.com>",
            from_address="council@example.org",
            to_address="iris@example.com",
            subject="Rego Renewal Due",
            text_body="Your vehicle registration renewal is due next week.",
        ),
    )
    assert notice_res.disposition == "needs_review"
    assert notice_res.email.error_detail is None
    assert discord.posts[0].channel_id == "ch-info"

    fail_res = await pipeline.ingest(
        db_session,
        InboundEmailWebhookPayload(
            message_id="<plutus-down-1@example.com>",
            from_address="alerts@bank.example",
            to_address="iris@example.com",
            subject="DBS Alert",
            text_body="SGD 50.00",
        ),
    )
    assert fail_res.disposition == "failed"
    assert fail_res.delivery_unavailable is True
    assert discord.posts[1].channel_id == "ch-alert"


@pytest.mark.asyncio
async def test_pipeline_retry_cached_verdict_and_ignore_emit_zero_discord_posts(
    db_session: Session,
) -> None:
    discord = InMemoryDiscordTransport()
    jev = InMemoryJevClient()
    plutus = InMemoryPlutusDispatcher(
        default_result=PlutusDispatchResult(status="dispatched", consumer_ref="tx-cached-1")
    )
    pipeline, _, _, _ = _make_pipeline(
        plutus_dispatch_enabled=True,
        jev=jev,
        plutus=plutus,
        discord=discord,
    )

    row = InboundEmail(
        message_id="<cached-verdict-1@example.com>",
        from_address="alerts@bank.example",
        to_address="iris@example.com",
        subject="DBS Alert",
        received_at=datetime(2026, 11, 22, 4, 0, tzinfo=timezone.utc),
        raw_excerpt="SGD 30.00",
        attachments_meta=[],
        jev_verdict={
            "route_target": "plutus_dbs_alert",
            "has_actionable_amount": True,
            "confidence": 0.94,
            "urgency_score": 8,
            "reason": "Cached",
            "provider": "muninn-jev",
        },
        route_target="plutus_dbs_alert",
        dispatch_status="failed",
        error_detail="plutus_upstream_503",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    retry_res = await pipeline.retry(db_session, row.id, reclassify=False)
    assert retry_res.disposition == "dispatched"
    assert retry_res.email.consumer_ref == "tx-cached-1"
    assert jev.calls == []
    assert len(plutus.requests) == 1

    ignore_res = pipeline.ignore(db_session, row.id)
    assert ignore_res.disposition == "ignored"
    assert ignore_res.email.dispatch_status == "ignored"
    assert discord.posts == []


def _make_multi_pipeline(
    *,
    confidence_threshold: float = 0.80,
    plutus_dispatch_enabled: bool = True,
    chronos_dispatch_enabled: bool = True,
    jev: InMemoryJevClient | None = None,
    plutus: InMemoryConsumerDispatcher | None = None,
    chronos: InMemoryConsumerDispatcher | None = None,
    discord: InMemoryDiscordTransport | None = None,
) -> tuple[
    InboundEmailPipeline,
    InMemoryJevClient,
    InMemoryConsumerDispatcher,
    InMemoryConsumerDispatcher,
    InMemoryDiscordTransport,
]:
    resolved_jev = jev or InMemoryJevClient()
    resolved_plutus = plutus or InMemoryConsumerDispatcher(
        default_result=ConsumerDispatchResult(status="dispatched", consumer_ref="plutus-tx-1")
    )
    resolved_chronos = chronos or InMemoryConsumerDispatcher(
        default_result=ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-1")
    )
    resolved_discord = discord or InMemoryDiscordTransport()
    alerter = OutboundAlerter(
        config=AlertingConfig(
            bot_token="discord-bot-token",
            alert_channel_id="ch-alert",
            info_channel_id="ch-info",
            health_channel_id="ch-health",
            wealth_channel_id="ch-wealth",
            default_user_id="user-1",
            confidence_threshold=confidence_threshold,
        ),
        discord=resolved_discord,
        jev=resolved_jev,
        clock=FakeClock(),
    )
    pipeline = InboundEmailPipeline(
        config=InboundConfig(
            confidence_threshold=confidence_threshold,
            plutus_dispatch_enabled=plutus_dispatch_enabled,
            chronos_dispatch_enabled=chronos_dispatch_enabled,
        ),
        jev=resolved_jev,
        consumers={"plutus": resolved_plutus, "chronos": resolved_chronos},
        alerter=alerter,
    )
    return pipeline, resolved_jev, resolved_plutus, resolved_chronos, resolved_discord


def test_questions_include_has_schedule_event_and_calendar_attachment_helper() -> None:
    questions = build_inbound_email_questions()
    assert list(questions.keys()) == [
        "route_target",
        "has_actionable_amount",
        "has_schedule_event",
        "significance_score",
    ]
    assert questions["has_schedule_event"]["type"] == "noul"
    assert ".ics" in questions["has_schedule_event"]["instructions"].lower()

    assert has_calendar_attachment([]) is False
    assert has_calendar_attachment([{"filename": "receipt.pdf", "content_type": "application/pdf"}]) is False
    assert has_calendar_attachment([{"filename": "invite.ics", "content_type": "application/octet-stream"}]) is True
    assert has_calendar_attachment([{"filename": "event.dat", "content_type": "text/calendar; charset=utf-8"}]) is True


@pytest.mark.asyncio
async def test_http_consumer_dispatch_adapter_chronos_statuses_and_errors() -> None:
    captured: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    responses: list[httpx.Response | Exception] = [
        httpx.Response(201, json={"status": "created", "consumer_ref": "chronos-evt-88"}),
        httpx.Response(200, json={"status": "duplicate", "consumer_ref": "chronos-evt-88"}),
        httpx.Response(422, text="invalid schedule payload"),
        httpx.Response(503, text="chronos db unavailable"),
        httpx.ConnectError("connection refused"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (str(request.url), dict(request.headers), json.loads(request.content.decode("utf-8")))
        )
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    adapter = HttpConsumerDispatchAdapter(
        ConsumerConfig(
            consumer_name="chronos",
            api_url="http://chronos-api:8000",
            webhook_secret="chronos-secret-token",
        ),
        transport=httpx.MockTransport(handler),
    )
    assert isinstance(adapter, ConsumerDispatchPort)
    req = ConsumerDispatchRequest(
        email_id=202,
        message_id="<cal-1@example.com>",
        route_target="actionable_notice",
        from_address="clinic@example.com",
        to_address="iris@example.com",
        subject="Dentist Checkup",
        received_at=datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc),
        text_body="Appointment at 14:00.",
        html_excerpt=None,
        raw_excerpt="Appointment at 14:00.",
        jev_verdict={"route_target": "actionable_notice", "has_schedule_event": True},
        attachments=(),
    )

    res_created = await adapter.dispatch_email(req)
    assert res_created == ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-88")
    assert captured[0][0] == "http://chronos-api:8000/integrations/email"
    assert captured[0][1]["authorization"] == "Bearer chronos-secret-token"

    res_dup = await adapter.dispatch_email(req)
    assert res_dup == ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-88")

    res_422 = await adapter.dispatch_email(req)
    assert res_422.status == "rejected"
    assert "chronos_validation_422" in (res_422.error_detail or "")

    res_503 = await adapter.dispatch_email(req)
    assert res_503.status == "unavailable"
    assert "chronos_upstream_503" in (res_503.error_detail or "")

    res_net = await adapter.dispatch_email(req)
    assert res_net.status == "unavailable"
    assert "chronos_network_error" in (res_net.error_detail or "")

    unconfigured = HttpConsumerDispatchAdapter(
        ConsumerConfig(consumer_name="chronos", api_url="", webhook_secret=None)
    )
    res_unconf = await unconfigured.dispatch_email(req)
    assert res_unconf.status == "unavailable"
    assert res_unconf.error_detail == (
        "chronos_dispatch_misconfigured: missing CHRONOS_API_URL, CHRONOS_WEBHOOK_SECRET"
    )


@pytest.mark.asyncio
async def test_confidence_isolation_uncertain_schedule_does_not_park_plutus_receipt(
    db_session: Session,
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "has_schedule_event": True,
            "significance_score": 8,
            "_confidence": 0.52,
            "_question_confidence": {
                "route_target": 0.95,
                "has_actionable_amount": 0.91,
                "has_schedule_event": 0.52,
                "significance_score": 0.88,
            },
        }
    )
    pipeline, _, plutus, chronos, _ = _make_multi_pipeline(jev=jev)

    res = await pipeline.ingest(
        db_session,
        InboundEmailWebhookPayload(
            message_id="<iso-conf-1@example.com>",
            from_address="orders@shop.example",
            to_address="iris@example.com",
            subject="Order Receipt",
            text_body="Order total NZD 49.00. Estimated delivery next Tuesday.",
        ),
    )
    assert res.disposition == "dispatched"
    assert res.email.dispatch_status == "dispatched"
    assert res.email.jev_verdict is not None
    assert res.email.jev_verdict["confidence"] == pytest.approx(0.91)
    assert res.email.jev_verdict["has_schedule_event"] is False
    assert res.email.jev_verdict["schedule_confidence"] == pytest.approx(0.52)
    assert len(plutus.requests) == 1
    assert chronos.requests == []


@pytest.mark.asyncio
async def test_dual_match_partial_failure_retry_skips_already_dispatched_consumer(
    db_session: Session,
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "has_schedule_event": True,
            "significance_score": 8,
            "_question_confidence": {
                "route_target": 0.96,
                "has_actionable_amount": 0.94,
                "has_schedule_event": 0.93,
            },
        }
    )
    plutus = InMemoryConsumerDispatcher(
        default_result=ConsumerDispatchResult(status="dispatched", consumer_ref="plutus-tx-502")
    )
    chronos = InMemoryConsumerDispatcher(
        responses=[
            ConsumerDispatchResult(status="unavailable", error_detail="chronos_upstream_503: timeout"),
            ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-502"),
        ]
    )
    pipeline, _, _, _, discord = _make_multi_pipeline(jev=jev, plutus=plutus, chronos=chronos)

    first_res = await pipeline.ingest(
        db_session,
        InboundEmailWebhookPayload(
            message_id="<dual-retry-502@example.com>",
            from_address="bookings@airline.example",
            to_address="iris@example.com",
            subject="Flight NZ502 to Wellington",
            text_body="Total paid NZD 245.00. Departing 2026-12-01 09:30.",
        ),
    )
    assert first_res.disposition == "failed"
    assert first_res.delivery_unavailable is True
    assert first_res.email.dispatch_status == "failed"
    assert "chronos_upstream_503" in (first_res.email.error_detail or "")
    assert len(discord.posts) == 1
    assert discord.posts[0].channel_id == "ch-alert"
    deliveries_after_ingest = {d.consumer: d for d in first_res.email.deliveries}
    assert deliveries_after_ingest["plutus"].dispatch_status == "dispatched"
    assert deliveries_after_ingest["plutus"].consumer_ref == "plutus-tx-502"
    assert deliveries_after_ingest["chronos"].dispatch_status == "failed"
    assert deliveries_after_ingest["chronos"].retry_count == 0

    retry_res = await pipeline.retry(db_session, first_res.email.id, reclassify=False)
    assert retry_res.disposition == "dispatched"
    assert retry_res.delivery_unavailable is False
    assert retry_res.email.dispatch_status == "dispatched"
    assert retry_res.email.error_detail is None
    assert retry_res.email.consumer_ref == "plutus:plutus-tx-502, chronos:chronos-evt-502"
    assert len(plutus.requests) == 1
    assert len(chronos.requests) == 2
    assert len(discord.posts) == 1
    deliveries_after_retry = {d.consumer: d for d in retry_res.email.deliveries}
    assert deliveries_after_retry["plutus"].retry_count == 0
    assert deliveries_after_retry["chronos"].dispatch_status == "dispatched"
    assert deliveries_after_retry["chronos"].consumer_ref == "chronos-evt-502"
    assert deliveries_after_retry["chronos"].retry_count == 1


