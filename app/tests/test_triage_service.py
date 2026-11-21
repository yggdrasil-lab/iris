"""Unit tests for centralized Muninn jev-decision TriageService."""

import json
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.services.triage_service import (
    _INBOUND_EMAIL_QUESTIONS,
    JevRouterUnavailableError,
    TriageService,
)


def _mock_async_client(monkeypatch, handler) -> None:
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None):
            return await handler(url, json, headers or {})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_muninn_jev_alert_triage_payload_suppression_and_missing_confidence(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")
    monkeypatch.setattr(settings, "MUNINN_JEV_MODEL", "jev-decision")
    monkeypatch.setattr(settings, "IRIS_ALERT_SCORE_THRESHOLD", 7)
    monkeypatch.setattr(settings, "IRIS_ALERT_CONFIDENCE_THRESHOLD", 0.80)

    captured: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    next_response: dict[str, Any] = {
        "urgency_score": "3",
        "is_transient_noise": "true",
        "destination_channel": "metrics",
        "_confidence": 0.91,
    }

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        captured.append((url, body, headers))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(next_response)}}]},
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_post)
    svc = TriageService()

    # 1. Calibrated high-confidence transient noise -> suppressed
    suppressed = await svc.evaluate_alert(
        service="apollo",
        title="Container restarting",
        details="Rolling deploy",
        severity="warning",
        node="gaia",
    )
    assert suppressed.provider == "muninn-jev"
    assert suppressed.urgency_score == 3
    assert suppressed.is_transient_noise is True
    assert suppressed.destination_channel == "metrics"
    assert suppressed.suppress is True
    assert suppressed.confidence == pytest.approx(0.91)

    url, req_body, hdrs = captured[0]
    assert url == "http://muninn.local/v1/chat/completions"
    assert hdrs["Authorization"] == "Bearer muninn-master-key"
    assert hdrs["User-Agent"] == "Yggdrasil-Iris/1.0"
    assert req_body["model"] == "jev-decision"
    assert req_body["temperature"] == 0.0
    assert req_body["response_format"] == {"type": "json_object"}
    assert '"include_meta": true' in req_body["messages"][0]["content"]
    assert '"questions"' in req_body["messages"][0]["content"]

    # 2. Missing _confidence -> defaults to 0.0 and escalates fail-open (suppress=False)
    next_response = {
        "urgency_score": 2,
        "is_transient_noise": True,
        "destination_channel": "alerts",
    }
    escalated = await svc.evaluate_alert(
        service="apollo",
        title="Container restarting",
        details="Rolling deploy",
        severity="warning",
    )
    assert escalated.provider == "muninn-jev"
    assert escalated.confidence == 0.0
    assert escalated.suppress is False


@pytest.mark.asyncio
async def test_muninn_jev_alert_triage_fails_open_when_unconfigured_or_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", None)

    svc = TriageService()
    res_unconf = await svc.evaluate_alert(
        service="plutus",
        title="DB error",
        details="Connection reset",
        severity="error",
    )
    assert res_unconf.provider == "fail-open-fallback"
    assert res_unconf.suppress is False
    assert res_unconf.urgency_score == 8

    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    async def fake_err(url: str, body: dict[str, Any], headers: dict[str, str]):
        return httpx.Response(503, text="down", request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_err)
    res_503 = await svc.evaluate_alert(
        service="plutus",
        title="Slow query",
        details="Timeout",
        severity="warning",
    )
    assert res_503.provider == "fail-open-fallback"
    assert res_503.suppress is False
    assert res_503.urgency_score == 5


@pytest.mark.asyncio
async def test_muninn_jev_email_routing_rejects_bad_json_or_unknown_target(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    bad_payloads = ["not-json", '{"route_target":"unknown_route"}']
    idx = {"i": 0}

    async def fake_bad(url: str, body: dict[str, Any], headers: dict[str, str]):
        content = bad_payloads[idx["i"]]
        idx["i"] += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_bad)
    svc = TriageService()

    with pytest.raises(JevRouterUnavailableError, match="Invalid Muninn Jev JSON"):
        await svc.route_inbound_email("a@b.com", "iris@b.com", "Subj", "2026-11-20T00:00:00Z", "body", [])

    with pytest.raises(JevRouterUnavailableError, match="Invalid or missing route_target"):
        await svc.route_inbound_email("a@b.com", "iris@b.com", "Subj", "2026-11-20T00:00:00Z", "body", [])


@pytest.mark.asyncio
async def test_muninn_jev_email_routing_prompt_criteria_and_compact_currency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    captured: list[dict[str, Any]] = []

    async def fake_ok(url: str, body: dict[str, Any], headers: dict[str, str]):
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "route_target": "plutus_dbs_alert",
                                    "has_actionable_amount": "true",
                                    "significance_score": "8",
                                    "_confidence": 0.94,
                                }
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_ok)
    svc = TriageService()

    verdict = await svc.route_inbound_email(
        from_address="ibanking.alert@dbs.com",
        to_address="iris@example.com",
        subject="iBanking Alerts - FAST Transfer",
        received_at="2026-11-20T08:00:00Z",
        raw_excerpt="Amount: SGD2000.00 to Example Recipient",
        attachments_meta=[],
    )
    assert verdict.route_target == "plutus_dbs_alert"
    assert verdict.has_actionable_amount is True
    assert verdict.confidence == pytest.approx(0.94)
    assert verdict.urgency_score == 8

    system_content = captured[0]["messages"][0]["content"]
    assert "iBanking Alerts" in system_content
    assert "digibank Alert" in system_content
    assert "ibanking.alert@dbs.com" in system_content
    assert "bill payment" in system_content
    assert "E-commerce order confirmation" in system_content
    assert "Amazon" in system_content
    assert "forwarded purchase receipt" in system_content
    assert "SGD2000.00" in system_content
    assert "$179.74" in system_content
    assert "AUD" in system_content


def test_inbound_email_questions_cover_travel_receipts_and_attachment_amounts() -> None:
    route_criteria = _INBOUND_EMAIL_QUESTIONS["route_target"]["criteria"]
    receipt_criteria = route_criteria["plutus_purchase_receipt"]
    notice_criteria = route_criteria["actionable_notice"]
    amount_instructions = _INBOUND_EMAIL_QUESTIONS["has_actionable_amount"]["instructions"]

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

    assert "TaxInvoice.pdf" in amount_instructions
    assert "SI_*.jpg" in amount_instructions
    assert "attachment" in amount_instructions.lower()
    assert "quoted thread" in amount_instructions.lower()


@pytest.mark.asyncio
async def test_muninn_jev_email_routing_includes_travel_receipt_and_attachment_criteria(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    captured: list[dict[str, Any]] = []

    async def fake_ok(url: str, body: dict[str, Any], headers: dict[str, str]):
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "route_target": "plutus_purchase_receipt",
                                    "has_actionable_amount": True,
                                    "significance_score": 7,
                                    "_question_confidence": {
                                        "route_target": 0.95,
                                        "has_actionable_amount": 0.91,
                                        "significance_score": 0.90,
                                    },
                                }
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_ok)
    verdict = await TriageService().route_inbound_email(
        from_address="reservations@example-hotel.test",
        to_address="iris@example.com",
        subject="Hotel Reservation Deposit Confirmation",
        received_at="2026-11-21T02:00:00Z",
        raw_excerpt="Thank you for your deposit payment. See attached invoice image.",
        attachments_meta=[
            {
                "filename": "SI_99881.jpg",
                "content_type": "image/jpeg",
                "size_bytes": 480000,
            }
        ],
    )

    assert verdict.route_target == "plutus_purchase_receipt"
    assert verdict.has_actionable_amount is True
    assert verdict.confidence == pytest.approx(0.91)

    system_content = captured[0]["messages"][0]["content"]
    user_content = captured[0]["messages"][1]["content"]
    assert "TaxInvoice.pdf" in system_content
    assert "SI_*.jpg" in system_content
    assert "SI_99881.jpg" in user_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_confidence"),
    [
        # 1. High route_target + has_actionable_amount, low significance_score & _confidence -> 0.98
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
        # 2. Low route_target with high significance_score -> uses low route_target (0.72)
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
        # 3. Low has_actionable_amount with high significance_score -> uses low has_actionable_amount (0.65)
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
    ],
)
async def test_muninn_jev_email_routing_uses_routing_question_confidences(
    monkeypatch, payload: dict[str, Any], expected_confidence: float
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    async def fake_ok(url: str, body: dict[str, Any], headers: dict[str, str]):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_ok)
    verdict = await TriageService().route_inbound_email(
        from_address="alerts@example.com",
        to_address="iris@example.com",
        subject="Inbound alert",
        received_at="2026-11-20T08:00:00Z",
        raw_excerpt="Amount: SGD2000.00",
        attachments_meta=[],
    )
    assert verdict.confidence == pytest.approx(expected_confidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_confidence"),
    [
        # 1. Absent _question_confidence -> falls back cleanly to _confidence
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
                "_confidence": 0.88,
            },
            0.88,
        ),
        # 2. Non-dict _question_confidence -> falls back to _confidence
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
        # 3. Partial _question_confidence (missing has_actionable_amount) -> falls back to _confidence
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
        # 4. Partial _question_confidence (None value) -> falls back to _confidence
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
        # 5. Missing both _question_confidence and _confidence -> defaults to 0.0
        (
            {
                "route_target": "plutus_dbs_alert",
                "has_actionable_amount": True,
                "significance_score": 7,
            },
            0.0,
        ),
        # 6. Non-numeric routing confidence -> 0.0
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
        # 7. Non-finite (inf / -inf / nan) routing confidences -> 0.0
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
        # 8. Zero and out-of-range finite routing confidences clamp cleanly to [0.0, 1.0]
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
async def test_muninn_jev_email_routing_confidence_fallbacks_and_edge_cases(
    monkeypatch, payload: dict[str, Any], expected_confidence: float
) -> None:
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn.local/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", "muninn-master-key")

    async def fake_ok(url: str, body: dict[str, Any], headers: dict[str, str]):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=httpx.Request("POST", url),
        )

    _mock_async_client(monkeypatch, fake_ok)
    verdict = await TriageService().route_inbound_email(
        from_address="sender@example.com",
        to_address="iris@example.com",
        subject="Subject",
        received_at="2026-11-20T08:00:00Z",
        raw_excerpt="Body",
        attachments_meta=[],
    )
    assert verdict.confidence == pytest.approx(expected_confidence)
