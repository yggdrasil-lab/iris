import json
from typing import Any

import httpx
import pytest

from app.jev import (
    JEV_USER_AGENT,
    JevClientPort,
    JevConfig,
    JevRouterUnavailableError,
    MuninnJevAdapter,
    parse_bool,
    parse_clamped_confidence,
    parse_clamped_score,
    resolve_calibrated_confidence,
)
from app.tests.fakes import InMemoryJevClient


def test_in_memory_jev_client_satisfies_protocol() -> None:
    fake = InMemoryJevClient()
    assert isinstance(fake, JevClientPort)


def test_parsing_and_calibrated_confidence_helpers() -> None:
    assert parse_bool(True) is True
    assert parse_bool("YES") is True
    assert parse_bool("false") is False
    assert parse_bool("invalid") is False

    assert parse_clamped_score("8.6") == 9
    assert parse_clamped_score("0") == 1
    assert parse_clamped_score("15") == 10
    assert parse_clamped_score(float("nan"), default=6) == 6

    assert parse_clamped_confidence(True) == 0.0
    assert parse_clamped_confidence(float("inf")) == 0.0
    assert parse_clamped_confidence("nan") == 0.0
    assert parse_clamped_confidence(-0.2) == 0.0
    assert parse_clamped_confidence(1.5) == 1.0
    assert parse_clamped_confidence("0.87") == pytest.approx(0.87)

    assert (
        resolve_calibrated_confidence(
            {
                "_confidence": 0.60,
                "_question_confidence": {"route_target": 0.99, "has_actionable_amount": 0.91},
            },
            question_keys=("route_target", "has_actionable_amount"),
        )
        == pytest.approx(0.91)
    )
    assert (
        resolve_calibrated_confidence(
            {"_confidence": 0.74, "_question_confidence": {"route_target": 0.99}},
            question_keys=("route_target", "has_actionable_amount"),
        )
        == pytest.approx(0.74)
    )
    assert resolve_calibrated_confidence({}) == 0.0


@pytest.mark.asyncio
async def test_muninn_jev_adapter_sends_include_meta_schema_and_parses_response() -> None:
    captured: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                str(request.url),
                dict(request.headers),
                json.loads(request.content.decode("utf-8")),
            )
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "urgency_score": 8,
                                    "is_transient_noise": False,
                                    "destination_channel": "alerts",
                                    "_confidence": 0.93,
                                }
                            )
                        }
                    }
                ]
            },
        )

    adapter = MuninnJevAdapter(
        JevConfig(
            muninn_base="http://muninn.local/v1/",
            muninn_api_key="muninn-test-key",
            muninn_jev_model="jev-decision",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.evaluate_questions(
        "Service: plutus\nAlert Title: DB timeout",
        {"urgency_score": {"type": "score", "criteria": ["low", "high"]}},
    )

    assert result["urgency_score"] == 8
    assert result["_confidence"] == pytest.approx(0.93)

    url, headers, payload = captured[0]
    assert url == "http://muninn.local/v1/chat/completions"
    assert headers["authorization"] == "Bearer muninn-test-key"
    assert headers["user-agent"] == JEV_USER_AGENT
    assert payload["model"] == "jev-decision"
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    assert '"include_meta": true' in payload["messages"][0]["content"]
    assert '"questions"' in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_muninn_jev_adapter_fails_loud_on_missing_config_http_error_and_bad_json() -> None:
    unconfigured = MuninnJevAdapter(
        JevConfig(muninn_base="http://muninn.local/v1", muninn_api_key=None)
    )
    with pytest.raises(JevRouterUnavailableError, match="unconfigured"):
        await unconfigured.evaluate_questions("state", {"q": {"type": "noul"}})

    responses = [
        httpx.Response(503, text="upstream down"),
        httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "[1, 2, 3]"}}]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    adapter = MuninnJevAdapter(
        JevConfig(muninn_base="http://muninn.local/v1", muninn_api_key="secret-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(JevRouterUnavailableError, match="Muninn Jev call failed"):
        await adapter.evaluate_questions("state", {"q": {"type": "noul"}})

    with pytest.raises(JevRouterUnavailableError, match="Invalid Muninn Jev JSON"):
        await adapter.evaluate_questions("state", {"q": {"type": "noul"}})

    with pytest.raises(JevRouterUnavailableError, match="JSON object"):
        await adapter.evaluate_questions("state", {"q": {"type": "noul"}})
