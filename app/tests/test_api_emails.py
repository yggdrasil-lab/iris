"""Tests for Authentik-protected operator /api/emails endpoints."""

import base64
from datetime import datetime, timezone
from typing import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import InboundEmail, InboundEmailAttachment
from app.inbound.ports import PlutusDispatchResult
from app.tests.conftest import IrisTestHarness
from app.tests.fakes import InMemoryDiscordTransport, InMemoryJevClient, InMemoryPlutusDispatcher


def _seed_emails(db: Session) -> tuple[InboundEmail, InboundEmail]:
    first = InboundEmail(
        message_id="<api-1@example.com>",
        from_address="alerts@bank.example.com",
        to_address="iris@example.com",
        subject="DBS Card Charge SGD 12.50",
        received_at=datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc),
        text_body="Charged SGD 12.50 at Cafe.",
        html_excerpt="<p>Charged SGD 12.50 at Cafe.</p>",
        raw_excerpt="Charged SGD 12.50 at Cafe.",
        attachments_meta=[
            {
                "filename": "slip.pdf",
                "content_type": "application/pdf",
                "size_bytes": 4,
                "sha256": "a" * 64,
            }
        ],
        jev_verdict={
            "route_target": "plutus_dbs_alert",
            "has_actionable_amount": True,
            "confidence": 0.96,
            "urgency_score": 8,
            "reason": "Bank alert",
            "provider": "muninn-jev",
        },
        route_target="plutus_dbs_alert",
        dispatch_status="needs_review",
        error_detail="plutus_dispatch_disabled_phase1_capture",
        attachments=[
            InboundEmailAttachment(
                filename="slip.pdf",
                content_type="application/pdf",
                size_bytes=4,
                sha256="a" * 64,
                content_base64="dGVzdA==",
            )
        ],
    )
    second = InboundEmail(
        message_id="<api-2@example.com>",
        from_address="news@promo.example.com",
        to_address="iris@example.com",
        subject="Weekly Newsletter",
        received_at=datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc),
        raw_excerpt="Unsubscribe anytime.",
        attachments_meta=[],
        route_target="ignore",
        dispatch_status="ignored",
    )
    db.add_all([first, second])
    db.commit()
    db.refresh(first)
    db.refresh(second)
    return first, second


def test_api_emails_requires_authentik_headers_in_production(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    first, _ = _seed_emails(db_session)
    prod = make_harness(settings_overrides={"ENV": "production"})

    assert prod.client.get("/api/emails").status_code == 401
    assert prod.client.get("/api/emails/stats").status_code == 401
    assert prod.client.get(f"/api/emails/{first.id}").status_code == 401
    assert prod.client.post(f"/api/emails/{first.id}/retry").status_code == 401
    assert prod.client.post(f"/api/emails/{first.id}/reroute", json={"route_target": "ignore"}).status_code == 401
    assert prod.client.post(f"/api/emails/{first.id}/ignore").status_code == 401

    resp_ok = prod.client.get(
        "/api/emails",
        headers={"X-authentik-username": "operator1"},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["total"] == 2


def test_api_emails_list_stats_detail_and_filters(
    client: TestClient,
    db_session: Session,
) -> None:
    first, _ = _seed_emails(db_session)

    list_all = client.get("/api/emails")
    assert list_all.status_code == 200
    data_all = list_all.json()
    assert data_all["total"] == 2
    assert data_all["items"][0]["id"] == first.id

    filtered = client.get("/api/emails", params={"status": "needs_review", "q": "Cafe"})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["message_id"] == "<api-1@example.com>"

    stats = client.get("/api/emails/stats")
    assert stats.status_code == 200
    stats_data = stats.json()
    assert stats_data["total"] == 2
    assert stats_data["by_status"]["needs_review"] == 1
    assert stats_data["by_status"]["ignored"] == 1
    assert stats_data["by_route_target"]["plutus_dbs_alert"] == 1

    detail = client.get(f"/api/emails/{first.id}")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["text_body"] == "Charged SGD 12.50 at Cafe."
    assert len(detail_data["attachments"]) == 1
    assert detail_data["attachments"][0]["content_base64"] == "dGVzdA=="

    assert client.get("/api/emails/999999").status_code == 404


def test_api_emails_retry_reroute_and_ignore_fire_zero_discord_decision_alerts(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    first, second = _seed_emails(db_session)
    plutus = InMemoryPlutusDispatcher(
        responses=[
            PlutusDispatchResult(status="dispatched", consumer_ref="plutus-tx-99"),
            PlutusDispatchResult(status="dispatched", consumer_ref="plutus-tx-100"),
        ]
    )
    discord = InMemoryDiscordTransport()
    h = make_harness(
        settings_overrides={
            "IRIS_PLUTUS_DISPATCH_ENABLED": True,
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_INFO_ID": "chan-info-123",
            "DISCORD_CHANNEL_ALERT_ID": "chan-alert-456",
        },
        plutus_dispatcher=plutus,
        discord_transport=discord,
    )

    retry_resp = h.client.post(f"/api/emails/{first.id}/retry")
    assert retry_resp.status_code == 200
    retry_data = retry_resp.json()
    assert retry_data["dispatch_status"] == "dispatched"
    assert retry_data["consumer_ref"] == "plutus-tx-99"
    assert retry_data["retry_count"] == 1

    reroute_resp = h.client.post(
        f"/api/emails/{second.id}/reroute",
        json={"route_target": "plutus_purchase_receipt"},
    )
    assert reroute_resp.status_code == 200
    reroute_data = reroute_resp.json()
    assert reroute_data["route_target"] == "plutus_purchase_receipt"
    assert reroute_data["dispatch_status"] == "dispatched"
    assert reroute_data["consumer_ref"] == "plutus-tx-100"
    assert reroute_data["retry_count"] == 1

    reroute_notice = h.client.post(
        f"/api/emails/{second.id}/reroute",
        json={"route_target": "actionable_notice"},
    )
    assert reroute_notice.status_code == 200
    assert reroute_notice.json()["dispatch_status"] == "needs_review"

    ignore_resp = h.client.post(f"/api/emails/{first.id}/ignore")
    assert ignore_resp.status_code == 200
    assert ignore_resp.json()["dispatch_status"] == "ignored"

    assert len(plutus.requests) == 2
    assert discord.posts == []


def test_api_emails_payslip_reroute_and_reclassify_retry(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    encoded_pdf = base64.b64encode(b"%PDF-1.4\nAcme Payroll Payslip").decode("ascii")

    portal_notice = InboundEmail(
        message_id="<api-payslip-portal@acme.example>",
        from_address="payroll@acme.example",
        to_address="iris@example.com",
        subject="Your latest payslip notification",
        received_at=datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc),
        raw_excerpt="Please review your Acme Payroll notice.",
        attachments_meta=[],
        route_target="actionable_notice",
        dispatch_status="needs_review",
    )
    stranded_payslip = InboundEmail(
        message_id="<api-payslip-stranded@acme.example>",
        from_address="alex.tan@example.com",
        to_address="iris@example.com",
        subject="Fwd: Your Latest Payslip Is Attached",
        received_at=datetime(2026, 11, 22, 1, 5, tzinfo=timezone.utc),
        text_body="Hi Alex Tan, your Acme Payroll payslip is attached.",
        raw_excerpt="Hi Alex Tan, your Acme Payroll payslip is attached.",
        attachments_meta=[
            {
                "filename": "payslip.pdf",
                "content_type": "application/pdf",
                "size_bytes": 29,
                "sha256": "b" * 64,
            }
        ],
        jev_verdict={
            "route_target": "actionable_notice",
            "has_actionable_amount": False,
            "confidence": 0.70,
            "urgency_score": 5,
            "reason": "Parked pre-calibration",
            "provider": "muninn-jev",
        },
        route_target="actionable_notice",
        dispatch_status="needs_review",
        error_detail="low_confidence: 0.70 < 0.80",
        attachments=[
            InboundEmailAttachment(
                filename="payslip.pdf",
                content_type="application/pdf",
                size_bytes=29,
                sha256="b" * 64,
                content_base64=encoded_pdf,
            )
        ],
    )
    db_session.add_all([portal_notice, stranded_payslip])
    db_session.commit()
    db_session.refresh(portal_notice)
    db_session.refresh(stranded_payslip)

    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_payslip",
            "has_actionable_amount": True,
            "significance_score": 8,
            "_confidence": 0.95,
        }
    )
    plutus = InMemoryPlutusDispatcher(
        responses=[
            PlutusDispatchResult(status="dispatched", consumer_ref="receipt:88"),
            PlutusDispatchResult(status="dispatched", consumer_ref="receipt:91"),
        ]
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": True},
        jev_client=jev,
        plutus_dispatcher=plutus,
    )

    reroute_resp = h.client.post(
        f"/api/emails/{portal_notice.id}/reroute",
        json={"route_target": "plutus_payslip"},
    )
    assert reroute_resp.status_code == 200
    reroute_data = reroute_resp.json()
    assert reroute_data["route_target"] == "plutus_payslip"
    assert reroute_data["dispatch_status"] == "dispatched"
    assert reroute_data["consumer_ref"] == "receipt:88"
    assert reroute_data["error_detail"] is None

    reclassify_resp = h.client.post(
        f"/api/emails/{stranded_payslip.id}/retry",
        json={"reclassify": True},
    )
    assert reclassify_resp.status_code == 200
    reclassify_data = reclassify_resp.json()
    assert reclassify_data["route_target"] == "plutus_payslip"
    assert reclassify_data["dispatch_status"] == "dispatched"
    assert reclassify_data["consumer_ref"] == "receipt:91"
    assert reclassify_data["error_detail"] is None

    assert len(plutus.requests) == 2
    assert plutus.requests[1].route_target == "plutus_payslip"
    assert plutus.requests[1].attachments[0].filename == "payslip.pdf"
    assert plutus.requests[1].attachments[0].content_base64 == encoded_pdf
