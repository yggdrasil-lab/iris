"""Tests for POST /webhook/email ingestion, Muninn jev-decision routing, and latent bug fixes."""

import base64
from datetime import datetime, timezone
import json
from typing import Any, Optional
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import InboundEmail, InboundEmailAttachment
from app.services.inbound_email_service import (
    MAX_RAW_EXCERPT_CHARS,
    MAX_STORED_BODY_CHARS,
    _dispatch_to_plutus,
    build_raw_excerpt,
    clean_html_for_storage,
    sanitize_html_to_text,
)


def _make_muninn_jev_response(
    route_target: str,
    has_amount: str | bool = "true",
    score: str | int = "7",
    confidence: Optional[float] = 0.92,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "route_target": route_target,
        "has_actionable_amount": has_amount,
        "significance_score": score,
    }
    if confidence is not None:
        payload["_confidence"] = confidence
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload),
                }
            }
        ]
    }


def _mock_async_client(monkeypatch, handler) -> None:
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.init_headers = kwargs.get("headers") or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None):
            merged = {**self.init_headers, **(headers or {})}
            return await handler(url, json, merged)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


def test_webhook_email_auth_fail_closed_and_401(client: TestClient, monkeypatch) -> None:
    payload = {
        "message_id": "<auth-1@example.com>",
        "from_address": "sender@example.com",
        "to_address": "iris@example.com",
        "subject": "Auth check",
        "text_body": "Body",
    }

    monkeypatch.setattr(settings, "IRIS_WEBHOOK_SECRET", None)
    resp_503 = client.post("/webhook/email", json=payload, headers={"Authorization": "Bearer any"})
    assert resp_503.status_code == 503

    monkeypatch.setattr(settings, "IRIS_WEBHOOK_SECRET", "top-secret")
    resp_missing = client.post("/webhook/email", json=payload)
    assert resp_missing.status_code == 401

    resp_bad_scheme = client.post(
        "/webhook/email", json=payload, headers={"Authorization": "Basic dGVzdA=="}
    )
    assert resp_bad_scheme.status_code == 401

    resp_wrong = client.post(
        "/webhook/email", json=payload, headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp_wrong.status_code == 401


def test_webhook_email_capture_idempotency_and_attachments(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")
    monkeypatch.setattr(settings, "IRIS_PLUTUS_DISPATCH_ENABLED", False)

    calls: list[tuple[str, Any, dict[str, str]]] = []

    async def fake_post(url: str, json_body: Any, headers: dict[str, str]):
        calls.append((url, json_body, headers))
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("plutus_dbs_alert", has_amount="true", confidence=0.95),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post)

    raw_pdf = b"%PDF-1.4\nSample DBS statement"
    encoded_pdf = base64.b64encode(raw_pdf).decode("ascii")
    payload = {
        "message_id": "  <dbs-001@bank.example.com>  ",
        "from_address": "alerts@bank.example.com",
        "to_address": "iris@example.com",
        "subject": "Card Transaction Alert",
        "received_at": "2026-11-20T08:00:00Z",
        "html_body": "<html><style>.x{color:red}</style><script>alert(1)</script><body><p>SGD 18.40 at FairPrice</p></body></html>",
        "attachments": [
            {
                "filename": "advice.pdf",
                "content_type": "application/pdf",
                "content_base64": encoded_pdf,
            }
        ],
    }

    resp = client.post(
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
    assert len(calls) == 1
    call_url, call_body, call_headers = calls[0]
    assert call_url == "http://muninn.local/v1/chat/completions"
    assert call_headers["Authorization"] == "Bearer muninn-key"
    assert call_body["model"] == "jev-decision"
    assert call_body["response_format"] == {"type": "json_object"}
    assert '"include_meta": true' in call_body["messages"][0]["content"]

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
    dup_resp = client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert dup_resp.status_code == 200
    dup_body = dup_resp.json()
    assert dup_body["duplicate"] is True
    assert dup_body["status"] == "duplicate"
    assert dup_body["email_id"] == body["email_id"]
    assert len(calls) == 1


def test_webhook_email_missing_message_id_uses_sha256_idempotency(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")

    async def fake_post(url: str, json_body: Any, headers: dict[str, str]):
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("ignore", has_amount="false", score="2", confidence=0.91),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post)

    payload = {
        "from_address": "promo@shop.example.com",
        "to_address": "iris@example.com",
        "subject": "Weekend Sale",
        "received_at": "2026-11-20T09:00:00Z",
        "text_body": "Big discounts all week.",
    }
    first = client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["message_id"].startswith("sha256:")
    assert first_data["dispatch_status"] == "ignored"

    second = client.post(
        "/webhook/email",
        json=payload,
        headers={"Authorization": "Bearer test-iris-webhook-secret"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first_data["message_id"]


def test_webhook_email_low_or_missing_confidence_and_missing_amount_park_for_review(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "discord-bot-token")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_INFO_ID", "chan-info-123")

    discord_posts: list[str] = []

    async def fake_post_low_conf(url: str, json_body: Any, headers: dict[str, str]):
        if "discord.com/api/v10" in url:
            discord_posts.append(url)
            assert headers.get("Authorization") == "Bot discord-bot-token"
            return httpx.Response(200, json={"id": "thread-1"}, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("plutus_dbs_alert", has_amount="true", confidence=0.55),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post_low_conf)

    resp_low = client.post(
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
    assert discord_posts == ["https://discord.com/api/v10/channels/chan-info-123/threads"]

    # Missing _confidence from jev-shim must default to 0.0 and park as needs_review
    async def fake_post_missing_conf(url: str, json_body: Any, headers: dict[str, str]):
        if "discord.com/api/v10" in url:
            discord_posts.append(url)
            return httpx.Response(200, json={"id": "thread-1b"}, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("ignore", has_amount="false", confidence=None),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post_missing_conf)

    resp_missing_conf = client.post(
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

    async def fake_post_no_amount(url: str, json_body: Any, headers: dict[str, str]):
        if "discord.com/api/v10" in url:
            discord_posts.append(url)
            return httpx.Response(200, json={"id": "thread-2"}, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("plutus_purchase_receipt", has_amount="false", confidence=0.94),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post_no_amount)

    resp_no_amt = client.post(
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
    assert len(discord_posts) == 3


def test_webhook_email_muninn_unconfigured_or_down_fails_loud_503(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "discord-bot-token")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "chan-alert-456")

    alert_posts: list[str] = []

    async def fake_alert_only(url: str, json_body: Any, headers: dict[str, str]):
        if "discord.com/api/v10" in url:
            alert_posts.append(url)
            return httpx.Response(200, json={"id": "alert-thread"}, request=httpx.Request("POST", url))
        return httpx.Response(503, text="Muninn unavailable", request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_alert_only)

    # Case 1: MUNINN_API_KEY missing -> immediate fail-loud 503
    monkeypatch.setattr(settings, "MUNINN_API_KEY", None)
    resp_unconf = client.post(
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

    # Case 2: MUNINN_API_KEY set but Muninn returns 503 -> fail-loud 503 and Discord alert
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-secret-token")

    resp_503 = client.post(
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
    assert len(alert_posts) == 2

    failed_row = db_session.scalar(
        select(InboundEmail).where(InboundEmail.message_id == "<muninn-down@example.com>")
    )
    assert failed_row is not None
    assert failed_row.dispatch_status == "failed"


def test_webhook_email_plutus_dispatch_statuses(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")
    monkeypatch.setattr(settings, "IRIS_PLUTUS_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLUTUS_API_URL", "http://plutus-api:8000")
    monkeypatch.setattr(settings, "PLUTUS_WEBHOOK_SECRET", "plutus-secret")

    plutus_status = {"code": 200, "json": {"transaction_id": 777}}

    async def fake_dispatch(url: str, json_body: Any, headers: dict[str, str]):
        if "muninn.local" in url:
            return httpx.Response(
                200,
                json=_make_muninn_jev_response("plutus_dbs_alert", has_amount="true", confidence=0.95),
                request=httpx.Request("POST", url),
            )
        assert headers.get("Authorization") == "Bearer plutus-secret"
        assert headers.get("User-Agent") == "Yggdrasil-Iris/1.0"
        return httpx.Response(
            plutus_status["code"],
            json=plutus_status["json"],
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_dispatch)

    r_ok = client.post(
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

    plutus_status["code"] = 422
    plutus_status["json"] = {"detail": "Unparseable amount"}
    r_422 = client.post(
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

    plutus_status["code"] = 503
    plutus_status["json"] = {"detail": "DB busy"}
    r_503 = client.post(
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


def test_notify_discord_without_triage_does_not_raise_unbound_error(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "IRIS_ALERT_TRIAGE_ENABLED", False)
    send_mock = AsyncMock(return_value={"status": "delivered", "thread_id": "t-1"})
    monkeypatch.setattr("app.main.discord_service.send_alert", send_mock)

    resp = client.post(
        "/notify/discord",
        json={
            "service": "charon",
            "title": "Backup complete",
            "details": "All snapshots ok",
            "severity": "info",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"


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

    # If text_body becomes empty after stripping preheaders and tracking URLs, fall back to HTML
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
    client: TestClient, db_session: Session, monkeypatch
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

    # Verify ingest_email persists cleaned html_excerpt
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")
    monkeypatch.setattr(settings, "IRIS_PLUTUS_DISPATCH_ENABLED", False)

    async def fake_post(url: str, json_body: Any, headers: dict[str, str]):
        return httpx.Response(
            200,
            json=_make_muninn_jev_response("plutus_purchase_receipt", has_amount="true", confidence=0.95),
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post)
    resp = client.post(
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


def test_retry_email_reclassify_rebuilds_raw_excerpt_and_dispatch_uses_120s_timeout(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-key")
    monkeypatch.setattr(settings, "IRIS_PLUTUS_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLUTUS_API_URL", "http://plutus-api:8000")
    monkeypatch.setattr(settings, "PLUTUS_WEBHOOK_SECRET", "plutus-secret")

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

    recorded_calls: list[tuple[str, Any, Any]] = []

    class CapturingAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None):
            recorded_calls.append((url, self.timeout, json))
            if "muninn.local" in url:
                return httpx.Response(
                    200,
                    json=_make_muninn_jev_response("plutus_purchase_receipt", has_amount="true", confidence=0.96),
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"consumer_ref": "plutus-rcpt-42"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    retry_resp = client.post(
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

    assert len(recorded_calls) == 2
    muninn_url, muninn_timeout, muninn_body = recorded_calls[0]
    assert muninn_url == "http://muninn.local/v1/chat/completions"
    assert muninn_timeout == 10.0
    user_prompt = muninn_body["messages"][1]["content"]
    assert "Order Confirmed: SGD 179.74 at Example Mart" in user_prompt
    assert "starved preheader" not in user_prompt

    plutus_url, plutus_timeout, plutus_body = recorded_calls[1]
    assert plutus_url == "http://plutus-api:8000/integrations/email"
    assert plutus_timeout == 120.0
    assert "Order Confirmed: SGD 179.74 at Example Mart" in plutus_body["raw_excerpt"]


@pytest.mark.asyncio
async def test_dispatch_to_plutus_configures_120s_timeout(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PLUTUS_API_URL", "http://plutus-api:8000")
    monkeypatch.setattr(settings, "PLUTUS_WEBHOOK_SECRET", "plutus-secret")

    captured_timeouts: list[Any] = []

    class CapturingAsyncClient:
        def __init__(self, *args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None):
            return httpx.Response(
                200,
                json={"consumer_ref": "plutus-rcpt-120"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    email_row = InboundEmail(
        id=101,
        message_id="<timeout-check@example.com>",
        from_address="reservations@example-hotel.test",
        to_address="iris@example.com",
        subject="Hotel Deposit Receipt",
        received_at=datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc),
        text_body="Deposit Paid: PHP 11,700.00",
        html_excerpt=None,
        raw_excerpt="Deposit Paid: PHP 11,700.00",
        attachments_meta=[],
        jev_verdict={"route_target": "plutus_purchase_receipt"},
        route_target="plutus_purchase_receipt",
        dispatch_status="received",
        attachments=[],
    )

    status_code, dispatch_status, consumer_ref = await _dispatch_to_plutus(email_row)
    assert status_code == 200
    assert dispatch_status == "dispatched"
    assert consumer_ref == "plutus-rcpt-120"
    assert captured_timeouts == [120.0]
