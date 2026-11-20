"""Tests for POST /webhook/email ingestion, Muninn jev-decision routing, and latent bug fixes."""

import base64
import json
from typing import Any, Optional
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import InboundEmail, InboundEmailAttachment
from app.services.inbound_email_service import sanitize_html_to_text


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
