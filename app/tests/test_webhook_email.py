"""Tests for POST /webhook/email ingestion, Jev routing, sanitization, and Plutus dispatch."""

import base64
from datetime import datetime, timezone
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import InboundEmail, InboundEmailAttachment
from app.inbound import (
    MAX_RAW_EXCERPT_CHARS,
    MAX_STORED_BODY_CHARS,
    ConsumerDispatchResult,
    PlutusDispatchResult,
    build_raw_excerpt,
    clean_html_for_storage,
    sanitize_html_to_text,
)
from app.jev import JevConfig, JevRouterUnavailableError, MuninnJevAdapter
from app.tests.conftest import IrisTestHarness
from app.tests.fakes import (
    InMemoryConsumerDispatcher,
    InMemoryDiscordTransport,
    InMemoryJevClient,
    InMemoryPlutusDispatcher,
)


def test_webhook_email_auth_fail_closed_and_401(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    payload = {
        "message_id": "<auth-1@example.com>",
        "from_address": "sender@example.com",
        "to_address": "iris@example.com",
        "subject": "Auth check",
        "text_body": "Body",
    }

    unconfigured = make_harness(settings_overrides={"IRIS_WEBHOOK_SECRET": None})
    resp_503 = unconfigured.client.post(
        "/webhook/email", json=payload, headers={"Authorization": "Bearer any"}
    )
    assert resp_503.status_code == 503

    configured = make_harness(settings_overrides={"IRIS_WEBHOOK_SECRET": "top-secret"})
    assert configured.client.post("/webhook/email", json=payload).status_code == 401
    assert (
        configured.client.post(
            "/webhook/email", json=payload, headers={"Authorization": "Basic dGVzdA=="}
        ).status_code
        == 401
    )
    assert (
        configured.client.post(
            "/webhook/email", json=payload, headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )


def test_webhook_email_capture_idempotency_and_attachments(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_dbs_alert",
            "has_actionable_amount": True,
            "significance_score": 7,
            "_confidence": 0.95,
        }
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": False},
        jev_client=jev,
    )

    raw_pdf = b"%PDF-1.4\nSample DBS statement"
    encoded_pdf = base64.b64encode(raw_pdf).decode("ascii")
    payload = {
        "message_id": "  <dbs-001@bank.example.com>  ",
        "from_address": "alerts@bank.example.com",
        "to_address": "iris@example.com",
        "subject": "Card Transaction Alert",
        "received_at": "2026-11-20T08:00:00Z",
        "html_body": (
            "<html><style>.x{color:red}</style><script>alert(1)</script>"
            "<body><p>SGD 18.40 at FairPrice</p></body></html>"
        ),
        "attachments": [
            {
                "filename": "advice.pdf",
                "content_type": "application/pdf",
                "content_base64": encoded_pdf,
            }
        ],
    }

    resp = h.client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["duplicate"] is False
    assert body["route_target"] == "plutus_dbs_alert"
    assert body["dispatch_status"] == "needs_review"
    assert body["error_detail"] == "plutus_dispatch_disabled_phase1_capture"
    assert body["jev_verdict"]["provider"] == "muninn-jev"
    assert len(jev.calls) == 1
    assert "route_target" in jev.calls[0].questions
    assert "advice.pdf" in jev.calls[0].state_text

    saved = db_session.scalar(
        select(InboundEmail).where(InboundEmail.message_id == "<dbs-001@bank.example.com>")
    )
    assert saved is not None
    assert "alert(1)" not in (saved.raw_excerpt or "")
    assert "SGD 18.40 at FairPrice" in (saved.raw_excerpt or "")
    assert len(saved.attachments_meta) == 1
    assert saved.attachments_meta[0]["filename"] == "advice.pdf"

    att_rows = list(
        db_session.scalars(
            select(InboundEmailAttachment).where(InboundEmailAttachment.email_id == saved.id)
        ).all()
    )
    assert len(att_rows) == 1
    assert att_rows[0].content_base64 == encoded_pdf

    # Duplicate delivery must not mutate or re-invoke Jev
    dup_resp = h.client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert dup_resp.status_code == 200
    dup_body = dup_resp.json()
    assert dup_body["duplicate"] is True
    assert dup_body["status"] == "duplicate"
    assert dup_body["email_id"] == body["email_id"]
    assert len(jev.calls) == 1


def test_webhook_email_missing_message_id_uses_sha256_idempotency(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "ignore",
            "has_actionable_amount": False,
            "significance_score": 2,
            "_confidence": 0.91,
        }
    )
    h = make_harness(jev_client=jev)

    payload = {
        "from_address": "promo@shop.example.com",
        "to_address": "iris@example.com",
        "subject": "Weekend Sale",
        "received_at": "2026-11-20T09:00:00Z",
        "text_body": "Big discounts all week.",
    }
    first = h.client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["message_id"].startswith("sha256:")
    assert first_data["dispatch_status"] == "ignored"

    second = h.client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first_data["message_id"]


def test_webhook_email_low_or_missing_confidence_and_missing_amount_park_for_review(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        responses=[
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.55,
            },
            {
                "route_target": "ignore",
                "has_actionable_amount": False,
                "significance_score": 7,
            },
            {
                "route_target": "plutus_purchase_receipt",
                "has_actionable_amount": False,
                "significance_score": 7,
                "_confidence": 0.94,
            },
        ]
    )
    discord = InMemoryDiscordTransport()
    h = make_harness(
        settings_overrides={
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_INFO_ID": "chan-info-123",
        },
        jev_client=jev,
        discord_transport=discord,
    )

    resp_low = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<low-conf-1@example.com>",
            "from_address": "unknown@example.com",
            "to_address": "iris@example.com",
            "subject": "Unclear bank email",
            "text_body": "Reference 999",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_low.status_code == 200
    assert resp_low.json()["dispatch_status"] == "needs_review"
    assert "low_confidence" in resp_low.json()["error_detail"]

    resp_missing_conf = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<missing-conf-1@example.com>",
            "from_address": "info@example.com",
            "to_address": "iris@example.com",
            "subject": "Ambiguous notice",
            "text_body": "Details inside",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_missing_conf.status_code == 200
    assert resp_missing_conf.json()["dispatch_status"] == "needs_review"
    assert "low_confidence: 0.00" in resp_missing_conf.json()["error_detail"]

    resp_no_amt = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<no-amt-1@example.com>",
            "from_address": "orders@merchant.example.com",
            "to_address": "iris@example.com",
            "subject": "Your parcel has shipped",
            "text_body": "Tracking number XYZ123",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_no_amt.status_code == 200
    assert resp_no_amt.json()["dispatch_status"] == "needs_review"
    assert resp_no_amt.json()["error_detail"] == "receipt_missing_actionable_amount"
    assert [post.channel_id for post in discord.posts] == ["chan-info-123", "chan-info-123", "chan-info-123"]


def test_webhook_email_muninn_unconfigured_or_down_fails_loud_503(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    discord = InMemoryDiscordTransport()
    unconfigured = make_harness(
        settings_overrides={
            "MUNINN_API_KEY": None,
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_ALERT_ID": "chan-alert-456",
        },
        jev_client=MuninnJevAdapter(JevConfig(muninn_base="http://muninn.local/v1", muninn_api_key=None)),
        discord_transport=discord,
    )
    resp_unconf = unconfigured.client.post(
        "/webhook/email",
        json={
            "message_id": "<unconf-1@example.com>",
            "from_address": "alerts@bank.example.com",
            "to_address": "iris@example.com",
            "subject": "DBS Alert",
            "text_body": "SGD 10.00",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_unconf.status_code == 503
    assert resp_unconf.json()["dispatch_status"] == "failed"
    assert resp_unconf.json()["error_detail"].startswith("jev_router_unavailable:")

    down_jev = InMemoryJevClient(error=JevRouterUnavailableError("Muninn 503 down"))
    down_harness = make_harness(
        settings_overrides={
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_ALERT_ID": "chan-alert-456",
        },
        jev_client=down_jev,
        discord_transport=discord,
    )
    resp_503 = down_harness.client.post(
        "/webhook/email",
        json={
            "message_id": "<muninn-down@example.com>",
            "from_address": "alerts@bank.example.com",
            "to_address": "iris@example.com",
            "subject": "DBS Alert",
            "text_body": "SGD 10.00",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_503.status_code == 503
    data_503 = resp_503.json()
    assert data_503["dispatch_status"] == "failed"
    assert data_503["error_detail"].startswith("jev_router_unavailable:")
    assert [post.channel_id for post in discord.posts] == ["chan-alert-456", "chan-alert-456"]

    failed_row = db_session.scalar(
        select(InboundEmail).where(InboundEmail.message_id == "<muninn-down@example.com>")
    )
    assert failed_row is not None
    assert failed_row.dispatch_status == "failed"


def test_webhook_email_plutus_dispatch_statuses(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_dbs_alert",
            "has_actionable_amount": True,
            "significance_score": 8,
            "_confidence": 0.95,
        }
    )
    plutus = InMemoryPlutusDispatcher(
        responses=[
            PlutusDispatchResult(status="dispatched", consumer_ref="777"),
            PlutusDispatchResult(status="rejected", error_detail="plutus_validation_422: Unparseable amount"),
            PlutusDispatchResult(status="unavailable", error_detail="plutus_upstream_503: DB busy"),
        ]
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": True},
        jev_client=jev,
        plutus_dispatcher=plutus,
    )

    r_ok = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<plutus-ok@example.com>",
            "from_address": "alerts@bank.example.com",
            "to_address": "iris@example.com",
            "subject": "DBS Card Alert",
            "text_body": "SGD 25.00 at Grab",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["dispatch_status"] == "dispatched"
    assert r_ok.json()["consumer_ref"] == "777"

    r_422 = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<plutus-422@example.com>",
            "from_address": "alerts@bank.example.com",
            "to_address": "iris@example.com",
            "subject": "Bad alert",
            "text_body": "No amount",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert r_422.status_code == 200
    assert r_422.json()["dispatch_status"] == "failed"
    assert "plutus_validation_422" in r_422.json()["error_detail"]

    r_503 = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<plutus-503@example.com>",
            "from_address": "alerts@bank.example.com",
            "to_address": "iris@example.com",
            "subject": "Retry alert",
            "text_body": "SGD 99.00",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert r_503.status_code == 503
    assert r_503.json()["dispatch_status"] == "failed"


def test_sanitize_html_to_text_strips_scripts_and_styles() -> None:
    cleaned = sanitize_html_to_text(
        "<html><head><style>body{display:none}</style></head>"
        "<body><script>evil()</script><h1>Receipt</h1><p>Total: $19.99</p></body></html>"
    )
    assert "evil()" not in cleaned
    assert "display:none" not in cleaned
    assert "Receipt" in cleaned
    assert "Total: $19.99" in cleaned


def test_build_raw_excerpt_and_sanitize_html_strip_invisible_and_tracking() -> None:
    preheader = ("\u034f\u200c\u2007\u00ad\u200b\ufeff\xa0" * 300) + "\n"
    text_body = (
        f"{preheader}"
        "View in browser <https://track.example.com/click?token=abc123def456>\n"
        "https://redirect.example.com/r/1234567890123456789012345678901234567890\n"
        "Order Total:   SGD 179.74\n\n"
        "Merchant:   Example Store\n"
        "Short link: https://example.com/ok"
    )
    excerpt = build_raw_excerpt(text_body, None)
    assert "\u034f" not in excerpt
    assert "\u200c" not in excerpt
    assert "\u2007" not in excerpt
    assert "\u00ad" not in excerpt
    assert "track.example.com" not in excerpt
    assert "redirect.example.com" not in excerpt
    assert "Order Total: SGD 179.74\nMerchant: Example Store" in excerpt
    assert "https://example.com/ok" in excerpt

    empty_after_strip = (
        "\u034f\u200c\u2007\u00ad\n"
        "<https://track.example.com/only-tracking-link>\n"
        "https://redirect.example.com/r/1234567890123456789012345678901234567890"
    )
    fallback_html = (
        "<html><body><p>&#847;&zwnj;\u034f\u200c</p>"
        "<p>Item Total: $49.90</p>"
        "<p>https://click.example.com/t/1234567890123456789012345678901234567890</p>"
        "</body></html>"
    )
    fallback_excerpt = build_raw_excerpt(empty_after_strip, fallback_html)
    assert fallback_excerpt == "Item Total: $49.90"


def test_build_raw_excerpt_preserves_content_past_1500_chars_up_to_4000_chars() -> None:
    assert MAX_RAW_EXCERPT_CHARS == 4000

    lead_in = ("Booking notes and cancellation policy details. " * 50)[:1900].rstrip()
    amount_marker = "\nDeposit Paid: PHP 11,700.00\n"
    middle_pad = ("Terms and conditions line item. " * 80)[:2200].rstrip()
    trailing_marker = "TRAILING_BEYOND_4000"
    long_text = f"{lead_in}{amount_marker}{middle_pad}{trailing_marker}"

    excerpt = build_raw_excerpt(long_text, None)
    assert len(excerpt) == 4000
    assert "Deposit Paid: PHP 11,700.00" in excerpt
    assert trailing_marker not in excerpt


def test_clean_html_for_storage_shrinks_bloated_receipt_and_preserves_metadata(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    css_bloat = ".cls { color: #333; padding: 10px; }\n" * 600
    long_href = "https://track.example.com/gp/r.html?C=1234567890&K=ABCDEFGHIJKLMNOPQRSTUVWX&U=order123"
    long_src = "https://cdn.example.com/images/pixel/123456789012345678901234567890123456789012345.png"
    assert len(long_href) > 80
    assert len(long_src) > 80

    row_html = (
        '<tr style="background-color:#ffffff;font-family:Arial,sans-serif;padding:12px;">'
        f'<td style="width:100px;"><img style="border:0;" src="{long_src}" alt="Ergonomic Wireless Keyboard"></td>'
        f'<td style="padding:8px;"><a style="text-decoration:none;" href="{long_href}">Keyboard Order</a></td>'
        '<td style="text-align:right;"><span style="font-weight:bold;" aria-label="SGD 179.74">SGD 179.74</span></td>'
        "</tr>\n"
    )
    bloated_html = (
        "<!DOCTYPE html><html>"
        f"<head><title>Receipt</title><style>{css_bloat}</style></head>"
        "<body>"
        "<!-- internal tracking metadata comment -->"
        "<script>window.track = true;</script>"
        "<div>&#847;&zwnj;\u034f\u200c\u2007\u00ad&nbsp;</div>"
        '<a href="https://shop.example.com/orders/1">Order 1</a>'
        f"<table>{row_html * 220}</table>"
        "</body></html>"
    )
    assert len(bloated_html) > MAX_STORED_BODY_CHARS

    cleaned = clean_html_for_storage(bloated_html)
    assert cleaned is not None
    assert len(cleaned) < MAX_STORED_BODY_CHARS
    assert "<style" not in cleaned.lower()
    assert "<script" not in cleaned.lower()
    assert "<head" not in cleaned.lower()
    assert "internal tracking metadata comment" not in cleaned
    assert 'style="' not in cleaned.lower()
    assert "track.example.com" not in cleaned
    assert "cdn.example.com" not in cleaned
    assert "\u034f" not in cleaned
    assert "\u200c" not in cleaned
    assert "&#847;" not in cleaned
    assert "&zwnj;" not in cleaned
    assert 'aria-label="SGD 179.74"' in cleaned
    assert 'alt="Ergonomic Wireless Keyboard"' in cleaned
    assert 'href="https://shop.example.com/orders/1"' in cleaned
    assert "Keyboard Order" in cleaned

    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "significance_score": 8,
            "_confidence": 0.95,
        }
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": False},
        jev_client=jev,
    )
    resp = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<bloated-receipt@example.com>",
            "from_address": "orders@shop.example.com",
            "to_address": "iris@example.com",
            "subject": "Your example order",
            "html_body": bloated_html,
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp.status_code == 200
    saved = db_session.scalar(
        select(InboundEmail).where(InboundEmail.message_id == "<bloated-receipt@example.com>")
    )
    assert saved is not None
    assert saved.html_excerpt is not None
    assert len(saved.html_excerpt) < MAX_STORED_BODY_CHARS
    assert 'aria-label="SGD 179.74"' in saved.html_excerpt
    assert 'alt="Ergonomic Wireless Keyboard"' in saved.html_excerpt


def test_retry_email_reclassify_rebuilds_raw_excerpt_and_dispatches_to_plutus(
    make_harness: Callable[..., IrisTestHarness],
    db_session: Session,
) -> None:
    stale_row = InboundEmail(
        message_id="<stale-captured-1@example.com>",
        from_address="orders@shop.example.com",
        to_address="iris@example.com",
        subject="Your Order Confirmation",
        received_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
        text_body="\u034f\u200c\u2007\u00ad\n<https://track.example.com/preheader-only>\n",
        html_excerpt="<html><style>.x{display:none}</style><body><p>Order Confirmed: SGD 179.74 at Example Mart</p></body></html>",
        raw_excerpt="\u034f\u200c starved preheader",
        attachments_meta=[],
        jev_verdict={
            "route_target": "ignore",
            "has_actionable_amount": False,
            "confidence": 0.91,
            "urgency_score": 1,
            "reason": "stale",
            "provider": "muninn-jev",
        },
        route_target="ignore",
        dispatch_status="ignored",
    )
    db_session.add(stale_row)
    db_session.commit()
    db_session.refresh(stale_row)

    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "significance_score": 8,
            "_confidence": 0.96,
        }
    )
    plutus = InMemoryPlutusDispatcher(
        default_result=PlutusDispatchResult(status="dispatched", consumer_ref="plutus-rcpt-42")
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": True},
        jev_client=jev,
        plutus_dispatcher=plutus,
    )

    retry_resp = h.client.post(
        f"/api/emails/{stale_row.id}/retry",
        json={"reclassify": True},
    )
    assert retry_resp.status_code == 200
    data = retry_resp.json()
    assert data["route_target"] == "plutus_purchase_receipt"
    assert data["dispatch_status"] == "dispatched"
    assert data["consumer_ref"] == "plutus-rcpt-42"
    assert "Order Confirmed: SGD 179.74 at Example Mart" in data["raw_excerpt"]
    assert "\u034f" not in data["raw_excerpt"]

    assert len(jev.calls) == 1
    assert "Order Confirmed: SGD 179.74 at Example Mart" in jev.calls[0].state_text
    assert "starved preheader" not in jev.calls[0].state_text

    assert len(plutus.requests) == 1
    assert "Order Confirmed: SGD 179.74 at Example Mart" in (plutus.requests[0].raw_excerpt or "")


@pytest.mark.parametrize(
    ("case_id", "has_amount", "attachments"),
    [
        (
            "pdf-with-amount",
            True,
            [{"filename": "payslip.pdf", "content_type": "application/pdf"}],
        ),
        (
            "pdf-without-body-amount",
            False,
            [{"filename": "payslip.pdf", "content_type": "application/pdf"}],
        ),
        (
            "pdf-extension-octet-stream",
            False,
            [{"filename": "Acme_Payslip.PDF", "content_type": "application/octet-stream"}],
        ),
        (
            "image-payslip-without-body-amount",
            False,
            [{"filename": "payslip_page1.png", "content_type": "image/png; charset=binary"}],
        ),
        (
            "text-amount-only",
            True,
            [],
        ),
    ],
)
def test_webhook_email_payslip_dispatches_to_plutus(
    make_harness: Callable[..., IrisTestHarness],
    case_id: str,
    has_amount: bool,
    attachments: list[dict[str, str]],
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_payslip",
            "has_actionable_amount": has_amount,
            "significance_score": 8,
            "_confidence": 0.95,
        }
    )
    plutus = InMemoryPlutusDispatcher(
        default_result=PlutusDispatchResult(status="dispatched", consumer_ref="receipt:42")
    )
    h = make_harness(
        settings_overrides={"IRIS_PLUTUS_DISPATCH_ENABLED": True},
        jev_client=jev,
        plutus_dispatcher=plutus,
    )

    encoded_doc = base64.b64encode(b"%PDF-1.4\nAcme Payroll Net Pay Synthetic").decode("ascii")
    payload_attachments = [
        {
            "filename": item["filename"],
            "content_type": item["content_type"],
            "content_base64": encoded_doc,
        }
        for item in attachments
    ]

    resp = h.client.post(
        "/webhook/email",
        json={
            "message_id": f"<payslip-{case_id}@acme.example>",
            "from_address": "alex.tan@example.com",
            "to_address": "iris@example.com",
            "subject": "Fwd: Your Latest Payslip Is Attached",
            "text_body": "Hi Alex Tan, please find your latest payslip from Acme Payroll attached.",
            "attachments": payload_attachments,
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["route_target"] == "plutus_payslip"
    assert body["dispatch_status"] == "dispatched"
    assert body["consumer_ref"] == "receipt:42"
    assert body["error_detail"] is None

    assert len(plutus.requests) == 1
    dispatched = plutus.requests[0]
    assert dispatched.route_target == "plutus_payslip"
    assert len(dispatched.attachments) == len(payload_attachments)
    if payload_attachments:
        assert dispatched.attachments[0].filename == payload_attachments[0]["filename"]
        assert dispatched.attachments[0].content_base64 == encoded_doc


def test_webhook_email_payslip_missing_attachment_parks_and_keeps_receipt_guard(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        responses=[
            {
                "route_target": "plutus_payslip",
                "has_actionable_amount": False,
                "significance_score": 7,
                "_confidence": 0.91,
            },
            {
                "route_target": "plutus_purchase_receipt",
                "has_actionable_amount": False,
                "significance_score": 7,
                "_confidence": 0.91,
            },
        ]
    )
    plutus = InMemoryPlutusDispatcher()
    discord = InMemoryDiscordTransport()
    h = make_harness(
        settings_overrides={
            "IRIS_PLUTUS_DISPATCH_ENABLED": True,
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_INFO_ID": "chan-info-123",
        },
        jev_client=jev,
        plutus_dispatcher=plutus,
        discord_transport=discord,
    )

    resp_payslip = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<payslip-portal-only@acme.example>",
            "from_address": "payroll@acme.example",
            "to_address": "iris@example.com",
            "subject": "Log in to view your payslip",
            "text_body": "Hi Alex Tan, your payslip is ready on the HR portal.",
            "attachments": [],
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_payslip.status_code == 200
    payslip_body = resp_payslip.json()
    assert payslip_body["route_target"] == "plutus_payslip"
    assert payslip_body["dispatch_status"] == "needs_review"
    assert payslip_body["error_detail"] == "payslip_missing_attachment"
    assert plutus.requests == []
    assert [post.channel_id for post in discord.posts] == ["chan-info-123"]

    encoded_pdf = base64.b64encode(b"%PDF-1.4\nReceipt").decode("ascii")
    resp_receipt = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<receipt-no-amt-pdf@example.com>",
            "from_address": "orders@merchant.example",
            "to_address": "iris@example.com",
            "subject": "Order document",
            "text_body": "See attached order summary.",
            "attachments": [
                {
                    "filename": "summary.pdf",
                    "content_type": "application/pdf",
                    "content_base64": encoded_pdf,
                }
            ],
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_receipt.status_code == 200
    assert resp_receipt.json()["dispatch_status"] == "needs_review"
    assert resp_receipt.json()["error_detail"] == "receipt_missing_actionable_amount"
    assert plutus.requests == []
    assert len(discord.posts) == 2


def test_webhook_email_chronos_only_and_ics_deterministic_trigger(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        responses=[
            {
                "route_target": "actionable_notice",
                "has_actionable_amount": False,
                "has_schedule_event": True,
                "significance_score": 6,
                "_confidence": 0.93,
            },
            {
                "route_target": "ignore",
                "has_actionable_amount": False,
                "has_schedule_event": False,
                "significance_score": 2,
                "_confidence": 0.91,
            },
        ]
    )
    plutus = InMemoryPlutusDispatcher()
    chronos = InMemoryConsumerDispatcher(
        responses=[
            ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-101"),
            ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-evt-102"),
        ]
    )
    discord = InMemoryDiscordTransport()
    h = make_harness(
        settings_overrides={
            "IRIS_PLUTUS_DISPATCH_ENABLED": True,
            "IRIS_CHRONOS_DISPATCH_ENABLED": True,
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_INFO_ID": "chan-info-123",
            "DISCORD_CHANNEL_ALERT_ID": "chan-alert-456",
        },
        jev_client=jev,
        plutus_dispatcher=plutus,
        chronos_dispatcher=chronos,
        discord_transport=discord,
    )

    resp_dentist = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<dentist-checkup@example.com>",
            "from_address": "bookings@dental.example",
            "to_address": "iris@example.com",
            "subject": "Dentist Checkup",
            "text_body": "Reminder for Alex Tan: Dentist Checkup on 2026-12-03 at 14:30.",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_dentist.status_code == 200
    dentist_body = resp_dentist.json()
    assert dentist_body["dispatch_status"] == "dispatched"
    assert dentist_body["consumer_ref"] == "chronos-evt-101"
    assert len(dentist_body["deliveries"]) == 1
    assert dentist_body["deliveries"][0]["consumer"] == "chronos"
    assert dentist_body["deliveries"][0]["dispatch_status"] == "dispatched"
    assert plutus.requests == []
    assert len(chronos.requests) == 1
    assert discord.posts == []

    encoded_ics = base64.b64encode(
        b"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nSUMMARY:Dentist Checkup\nEND:VEVENT\nEND:VCALENDAR"
    ).decode("ascii")
    resp_ics = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<ics-invite@example.com>",
            "from_address": "calendar@example.org",
            "to_address": "iris@example.com",
            "subject": "Calendar Invite",
            "text_body": "See attached invite.",
            "attachments": [
                {
                    "filename": "invite.ics",
                    "content_type": "text/calendar; method=REQUEST",
                    "content_base64": encoded_ics,
                }
            ],
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert resp_ics.status_code == 200
    ics_body = resp_ics.json()
    assert ics_body["dispatch_status"] == "dispatched"
    assert ics_body["jev_verdict"]["has_schedule_event"] is True
    assert ics_body["consumer_ref"] == "chronos-evt-102"
    assert len(chronos.requests) == 2
    assert chronos.requests[1].attachments[0].filename == "invite.ics"
    assert plutus.requests == []


def test_webhook_email_dual_match_and_422_vs_503_handling(
    make_harness: Callable[..., IrisTestHarness],
) -> None:
    jev = InMemoryJevClient(
        default_response={
            "route_target": "plutus_purchase_receipt",
            "has_actionable_amount": True,
            "has_schedule_event": True,
            "significance_score": 8,
            "_question_confidence": {
                "route_target": 0.95,
                "has_actionable_amount": 0.93,
                "has_schedule_event": 0.92,
            },
        }
    )
    plutus = InMemoryPlutusDispatcher(
        responses=[
            ConsumerDispatchResult(status="dispatched", consumer_ref="plutus-502"),
            ConsumerDispatchResult(status="dispatched", consumer_ref="plutus-422"),
            ConsumerDispatchResult(status="dispatched", consumer_ref="plutus-503"),
        ]
    )
    chronos = InMemoryConsumerDispatcher(
        responses=[
            ConsumerDispatchResult(status="dispatched", consumer_ref="chronos-502"),
            ConsumerDispatchResult(status="rejected", error_detail="chronos_validation_422: bad date"),
            ConsumerDispatchResult(status="unavailable", error_detail="chronos_upstream_503: down"),
        ]
    )
    discord = InMemoryDiscordTransport()
    h = make_harness(
        settings_overrides={
            "IRIS_PLUTUS_DISPATCH_ENABLED": True,
            "IRIS_CHRONOS_DISPATCH_ENABLED": True,
            "DISCORD_BOT_TOKEN": "discord-bot-token",
            "DISCORD_CHANNEL_ALERT_ID": "chan-alert-456",
        },
        jev_client=jev,
        plutus_dispatcher=plutus,
        chronos_dispatcher=chronos,
        discord_transport=discord,
    )

    ok_resp = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<flight-nz502@example.com>",
            "from_address": "tickets@airline.example",
            "to_address": "iris@example.com",
            "subject": "Flight NZ502 to Wellington",
            "text_body": "Paid NZD 245.00. Departure 2026-12-01 09:30.",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert ok_resp.status_code == 200
    ok_body = ok_resp.json()
    assert ok_body["dispatch_status"] == "dispatched"
    assert {d["consumer"]: d["dispatch_status"] for d in ok_body["deliveries"]} == {
        "plutus": "dispatched",
        "chronos": "dispatched",
    }

    r_422 = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<dual-422@example.com>",
            "from_address": "tickets@airline.example",
            "to_address": "iris@example.com",
            "subject": "Flight NZ502 Bad Date",
            "text_body": "Paid NZD 245.00.",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert r_422.status_code == 200
    assert r_422.json()["dispatch_status"] == "failed"
    assert {d["consumer"]: d["dispatch_status"] for d in r_422.json()["deliveries"]} == {
        "plutus": "dispatched",
        "chronos": "failed",
    }

    r_503 = h.client.post(
        "/webhook/email",
        json={
            "message_id": "<dual-503@example.com>",
            "from_address": "tickets@airline.example",
            "to_address": "iris@example.com",
            "subject": "Flight NZ502 Retryable",
            "text_body": "Paid NZD 245.00.",
        },
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert r_503.status_code == 503
    assert r_503.json()["dispatch_status"] == "failed"
    assert [post.channel_id for post in discord.posts] == ["chan-alert-456", "chan-alert-456"]


def test_settings_strips_chronos_webhook_secret_whitespace() -> None:
    cfg = Settings(
        _env_file=None,
        CHRONOS_WEBHOOK_SECRET="  chronos-trimmed-secret\n",
        IRIS_CHRONOS_DISPATCH_ENABLED=True,
    )
    assert cfg.CHRONOS_WEBHOOK_SECRET == "chronos-trimmed-secret"
    assert cfg.IRIS_CHRONOS_DISPATCH_ENABLED is True

