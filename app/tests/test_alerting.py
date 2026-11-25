import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from app.alerting import (
    ClockPort,
    DiscordTransportPort,
    HttpDiscordTransportAdapter,
    USER_AGENT,
)
from app.container import build_container
from app.core.config import Settings
from app.jev import JevRouterUnavailableError
from app.main import create_app
from app.tests.fakes import (
    FakeClock,
    InMemoryDiscordTransport,
    InMemoryJevClient,
    InMemorySesAdapter,
)


def _build_test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "AWS_REGION": "ap-southeast-2",
        "EMAIL_SENDER": "no-reply@example.com",
        "ENV": "test",
        "DISCORD_BOT_TOKEN": "bot-token-123",
        "DISCORD_CHANNEL_ALERT_ID": "ch-alert",
        "DISCORD_CHANNEL_INFO_ID": "ch-info",
        "DISCORD_CHANNEL_HEALTH_ID": "ch-health",
        "DISCORD_CHANNEL_WEALTH_ID": "ch-wealth",
        "DISCORD_USER_ID": "test-user-1",
        "DISCORD_DEFAULT_COOLDOWN_SECONDS": 1800,
        "MUNINN_API_BASE": "http://muninn.local/v1",
        "MUNINN_API_KEY": "muninn-key",
        "IRIS_ALERT_TRIAGE_ENABLED": False,
        "IRIS_ALERT_SCORE_THRESHOLD": 7,
        "IRIS_ALERT_CONFIDENCE_THRESHOLD": 0.80,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_app(
    *,
    settings_overrides: dict[str, Any] | None = None,
    jev_client: InMemoryJevClient | None = None,
    discord_transport: InMemoryDiscordTransport | None = None,
    clock: FakeClock | None = None,
):
    cfg = _build_test_settings(**(settings_overrides or {}))
    jev = jev_client or InMemoryJevClient()
    discord = discord_transport or InMemoryDiscordTransport()
    clk = clock or FakeClock()
    container = build_container(
        cfg,
        ses_adapter=InMemorySesAdapter(),
        jev_client=jev,
        discord_transport=discord,
        clock=clk,
    )
    return create_app(container), jev, discord, clk


def test_alerting_fakes_satisfy_protocols() -> None:
    assert isinstance(InMemoryDiscordTransport(), DiscordTransportPort)
    assert isinstance(FakeClock(), ClockPort)


def test_notify_discord_without_triage_does_not_raise_unbound_error() -> None:
    test_app, jev, discord, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": False}
    )
    with TestClient(test_app) as client:
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
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["thread_id"] == "thread-1"
    assert len(jev.calls) == 0
    assert len(discord.posts) == 1


def test_notify_endpoint_discord_and_validation_errors() -> None:
    test_app, jev, discord, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": True},
        jev_client=InMemoryJevClient(
            default_response={
                "urgency_score": 9,
                "is_transient_noise": False,
                "destination_channel": "alerts",
                "_confidence": 0.94,
            }
        ),
    )
    with TestClient(test_app) as client:
        ok_resp = client.post(
            "/notify",
            json={
                "channel": "discord",
                "service": "plutus",
                "title": "Ledger Sync Failed",
                "details": "HTTP 502 from upstream",
                "severity": "error",
            },
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["status"] == "delivered"
        assert len(jev.calls) == 1
        assert len(discord.posts) == 1

        missing_resp = client.post(
            "/notify",
            json={"channel": "discord", "service": "plutus", "title": "Only Title"},
        )
        assert missing_resp.status_code == 400
        assert "required for discord channel" in missing_resp.json()["detail"]

        unsupported_resp = client.post(
            "/notify",
            json={"channel": "sms", "service": "plutus", "title": "T", "details": "D"},
        )
        assert unsupported_resp.status_code == 422


def test_notify_discord_triage_gates_suppress_metrics_fail_open_and_info_skip() -> None:
    jev = InMemoryJevClient(
        responses=[
            {
                "urgency_score": 8,
                "is_transient_noise": False,
                "destination_channel": "alerts",
                "_confidence": 0.95,
            },
            {
                "urgency_score": 2,
                "is_transient_noise": True,
                "destination_channel": "alerts",
                "_confidence": 0.92,
            },
            {
                "urgency_score": 8,
                "is_transient_noise": False,
                "destination_channel": "metrics",
                "_confidence": 0.90,
            },
        ]
    )
    test_app, _, discord, clk = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": True},
        jev_client=jev,
    )
    with TestClient(test_app) as client:
        # 1. Actionable error -> delivered with Jev Gating embed field
        r1 = client.post(
            "/notify/discord",
            json={
                "service": "plutus",
                "title": "API Down",
                "details": "HTTP 503",
                "severity": "error",
            },
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert set(b1.keys()) == {"status", "channel_id", "thread_id", "incident_count", "triage"}
        assert b1["status"] == "delivered"
        embed = discord.posts[0].message_payload["embeds"][0]
        assert [f["name"] for f in embed["fields"]] == [
            "Service",
            "Node",
            "Severity",
            "Jev Gating",
            "Details",
        ]
        assert embed["timestamp"] == clk.now_utc().isoformat()

        # 2. Suppressed transient noise -> zero Discord posts
        r2 = client.post(
            "/notify/discord",
            json={
                "service": "apollo",
                "title": "Container Restart",
                "details": "Rolling update",
                "severity": "warning",
            },
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "suppressed"
        assert len(discord.posts) == 1

        # 3. Choice destination metrics -> metrics_logged, zero Discord posts
        r3 = client.post(
            "/notify/discord",
            json={
                "service": "eir",
                "title": "Sync Metric",
                "details": "Heartrate ingested",
                "severity": "warning",
            },
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "metrics_logged"
        assert len(discord.posts) == 1

        # 4. severity == "info" -> skips Jev triage and posts resolution directly
        r4 = client.post(
            "/notify/discord",
            json={
                "service": "plutus",
                "title": "API Down",
                "details": "Recovered",
                "severity": "info",
            },
        )
        assert r4.status_code == 200
        assert r4.json()["status"] == "resolved"
        assert r4.json()["thread_id"] == "thread-1"
        assert len(jev.calls) == 3
        assert len(discord.posts) == 2

    # 5. Jev outage -> fail-open delivers alert
    failing_app, _, failing_discord, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": True},
        jev_client=InMemoryJevClient(error=JevRouterUnavailableError("Muninn timeout")),
    )
    with TestClient(failing_app) as client:
        r5 = client.post(
            "/notify/discord",
            json={
                "service": "plutus",
                "title": "DB Down",
                "details": "Connection refused",
                "severity": "error",
            },
        )
    assert r5.status_code == 200
    assert r5.json()["status"] == "delivered"
    assert r5.json()["triage"]["provider"] == "fail-open-fallback"
    assert len(failing_discord.posts) == 1


def test_triage_alert_endpoint_direct_evaluation_and_low_confidence_escalation() -> None:
    jev = InMemoryJevClient(
        responses=[
            {
                "urgency_score": 2,
                "is_transient_noise": True,
                "destination_channel": "alerts",
            },
        ]
    )
    test_app, _, _, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": False},
        jev_client=jev,
    )
    with TestClient(test_app) as client:
        # Missing _confidence -> defaults to 0.0 and escalates fail-open (suppress=False)
        resp = client.post(
            "/triage/alert",
            json={
                "service": "apollo",
                "title": "Restart blip",
                "details": "Pod restarted once",
                "severity": "warning",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "muninn-jev"
    assert body["confidence"] == 0.0
    assert body["suppress"] is False


def test_alertmanager_webhook_respects_triage_flag_and_resolved_lifecycle() -> None:
    firing_payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighLatency", "service": "plutus", "severity": "critical"},
                "annotations": {"summary": "Plutus latency", "description": "p99 > 2s"},
            }
        ],
    }

    # Case 1: IRIS_ALERT_TRIAGE_ENABLED=False -> skips Jev triage completely
    app_disabled, jev_disabled, discord_disabled, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": False}
    )
    with TestClient(app_disabled) as client:
        r_disabled = client.post("/webhook/alertmanager", json=firing_payload)
    assert r_disabled.status_code == 200
    assert r_disabled.json()["delivered"] == 1
    assert r_disabled.json()["suppressed"] == 0
    assert len(jev_disabled.calls) == 0
    assert len(discord_disabled.posts) == 1

    # Case 2: IRIS_ALERT_TRIAGE_ENABLED=True -> firing alert triages, resolved skips triage
    jev_enabled = InMemoryJevClient(
        responses=[
            {
                "urgency_score": 9,
                "is_transient_noise": False,
                "destination_channel": "alerts",
                "_confidence": 0.94,
            },
            {
                "urgency_score": 2,
                "is_transient_noise": True,
                "destination_channel": "alerts",
                "_confidence": 0.91,
            },
        ]
    )
    app_enabled, _, discord_enabled, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": True},
        jev_client=jev_enabled,
    )
    mixed_payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighLatency", "service": "plutus", "severity": "critical"},
                "annotations": {"summary": "Plutus latency", "description": "p99 > 2s"},
            },
            {
                "status": "firing",
                "labels": {"alertname": "NoisyBlip", "service": "apollo", "severity": "warning"},
                "annotations": {"summary": "Transient blip", "description": "self-cleared"},
            },
            {
                "status": "resolved",
                "labels": {"alertname": "HighLatency", "service": "plutus", "severity": "critical"},
                "annotations": {"summary": "Plutus latency", "description": "Back to normal"},
            },
        ],
    }
    with TestClient(app_enabled) as client:
        r_mixed = client.post("/webhook/alertmanager", json=mixed_payload)
    assert r_mixed.status_code == 200
    data = r_mixed.json()
    assert data["total_alerts"] == 3
    assert data["delivered"] == 1
    assert data["suppressed"] == 1
    assert [entry["status"] for entry in data["results"]] == [
        "delivered",
        "suppressed",
        "resolved",
    ]
    assert len(jev_enabled.calls) == 2
    assert len(discord_enabled.posts) == 2
    assert discord_enabled.posts[0].thread_id is None
    assert discord_enabled.posts[1].thread_id == "thread-1"
    assert discord_enabled.posts[1].message_payload["content"] == ""


def test_incident_cooldown_thread_reuse_and_stale_thread_recovery_via_fake_clock() -> None:
    clock = FakeClock(initial_seconds=5000.0)
    discord = InMemoryDiscordTransport(stale_thread_ids={"thread-1"})
    test_app, _, _, _ = _make_app(
        settings_overrides={"DISCORD_DEFAULT_COOLDOWN_SECONDS": 600},
        discord_transport=discord,
        clock=clock,
    )
    alert_body = {
        "service": "charon",
        "title": "Bisync Failed",
        "details": "Cache lock missing",
        "severity": "error",
    }
    with TestClient(test_app) as client:
        # 1. Initial alert creates thread-1 and pings user
        r1 = client.post("/notify/discord", json=alert_body)
        assert r1.status_code == 200
        assert r1.json()["status"] == "delivered"
        assert r1.json()["thread_id"] == "thread-1"
        assert discord.posts[0].message_payload["content"] == "<@test-user-1>"

        # 2. Within cooldown (120s < 600s) -> throttled, zero new Discord posts
        clock.advance(120.0)
        r2 = client.post("/notify/discord", json=alert_body)
        assert r2.status_code == 200
        assert r2.json()["status"] == "throttled"
        assert r2.json()["suppressed_count"] == 2
        assert r2.json()["thread_id"] == "thread-1"
        assert len(discord.posts) == 1

        # 3. After cooldown (advance 500s => total 620s >= 600s) -> tries thread-1 (stale) -> recovers to thread-2
        clock.advance(500.0)
        r3 = client.post("/notify/discord", json=alert_body)
        assert r3.status_code == 200
        assert r3.json()["status"] == "delivered"
        assert r3.json()["thread_id"] == "thread-2"
        assert discord.posts[1].thread_id == "thread-1"
        assert discord.posts[1].message_payload["content"] == ""
        assert "(Recurring)" in discord.posts[1].message_payload["embeds"][0]["title"]

        # 4. Subsequent alert after cooldown reuses healthy thread-2
        clock.advance(700.0)
        r4 = client.post("/notify/discord", json=alert_body)
        assert r4.status_code == 200
        assert r4.json()["thread_id"] == "thread-2"
        assert discord.posts[2].thread_id == "thread-2"

        # 5. Resolution clears incident so next firing alert creates a fresh thread and pings again
        r5 = client.post(
            "/notify/discord",
            json={**alert_body, "severity": "info", "details": "Recovered"},
        )
        assert r5.status_code == 200
        assert r5.json()["status"] == "resolved"
        assert r5.json()["thread_id"] == "thread-2"

        r6 = client.post("/notify/discord", json=alert_body)
        assert r6.status_code == 200
        assert r6.json()["thread_id"] == "thread-3"
        assert discord.posts[4].message_payload["content"] == "<@test-user-1>"


def test_discord_channel_resolution_and_fail_loud_missing_config() -> None:
    test_app, _, discord, _ = _make_app(
        settings_overrides={"IRIS_ALERT_TRIAGE_ENABLED": True},
        jev_client=InMemoryJevClient(
            default_response={
                "urgency_score": 8,
                "is_transient_noise": False,
                "destination_channel": "decisions",
                "_confidence": 0.95,
            }
        ),
    )
    with TestClient(test_app) as client:
        client.post(
            "/notify/discord",
            json={"service": "iris", "title": "Review", "details": "Check", "severity": "warning"},
        )
        client.post(
            "/notify/discord",
            json={
                "service": "eir",
                "title": "Sleep",
                "details": "Sync ok",
                "severity": "warning",
                "destination_channel": "health",
                "triage": False,
            },
        )
        client.post(
            "/notify/discord",
            json={
                "service": "plutus",
                "title": "Spend",
                "details": "Digest",
                "severity": "warning",
                "destination_channel": "wealth",
                "triage": False,
            },
        )
        client.post(
            "/notify/discord",
            json={
                "service": "custom",
                "title": "Override",
                "details": "Msg",
                "severity": "warning",
                "channel_id": "ch-custom",
                "triage": False,
            },
        )

    assert [p.channel_id for p in discord.posts] == [
        "ch-info",
        "ch-health",
        "ch-wealth",
        "ch-custom",
    ]

    # Missing token fails loud with HTTP 400
    no_token_app, _, _, _ = _make_app(settings_overrides={"DISCORD_BOT_TOKEN": None})
    with TestClient(no_token_app) as client:
        r_tok = client.post(
            "/notify/discord",
            json={"service": "iris", "title": "Fail", "details": "No token", "severity": "error"},
        )
        assert r_tok.status_code == 400
        assert "DISCORD_BOT_TOKEN" in r_tok.json()["detail"]

    # Missing explicit destination fails loud without falling back to ALERT_ID
    no_health_app, _, _, _ = _make_app(
        settings_overrides={"DISCORD_CHANNEL_HEALTH_ID": None, "DISCORD_CHANNEL_ALERT_ID": None}
    )
    with TestClient(no_health_app) as client:
        r_health = client.post(
            "/notify/discord",
            json={
                "service": "eir",
                "title": "Fail",
                "details": "No health",
                "severity": "warning",
                "destination_channel": "health",
            },
        )
        assert r_health.status_code == 400
        assert "health" in r_health.json()["detail"]

        r_default = client.post(
            "/notify/discord",
            json={"service": "iris", "title": "Fail", "details": "No alert", "severity": "error"},
        )
        assert r_default.status_code == 400
        assert "alerts" in r_default.json()["detail"]


@pytest.mark.asyncio
async def test_http_discord_transport_adapter_forum_reply_404_and_text_channel_fallback() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                str(request.url),
                dict(request.headers),
                json.loads(request.content.decode("utf-8")),
            )
        )
        url = str(request.url)
        if url.endswith("/channels/111111/threads"):
            return httpx.Response(201, json={"id": "forum-thread-1"})
        if url.endswith("/channels/forum-thread-1/messages"):
            return httpx.Response(200, json={"id": "reply-msg-2"})
        if url.endswith("/channels/stale-thread/messages"):
            return httpx.Response(404, json={"message": "Unknown Channel"})
        if url.endswith("/channels/text-chan-77/threads"):
            return httpx.Response(400, json={"message": "Channel is not a forum"})
        if url.endswith("/channels/text-chan-77/messages"):
            return httpx.Response(200, json={"id": "plain-msg-1"})
        raise AssertionError(f"Unexpected URL: {url}")

    adapter = HttpDiscordTransportAdapter(transport=httpx.MockTransport(handler))
    payload = {"content": "<@test-user-1>", "embeds": [{"title": "Alert"}]}

    _, tid1 = await adapter.post_message(
        bot_token="bot-token-123",
        channel_id="111111",
        thread_name="Alert Thread",
        message_payload=payload,
    )
    assert tid1 == "forum-thread-1"
    assert calls[0][1]["authorization"] == "Bot bot-token-123"
    assert calls[0][1]["user-agent"] == USER_AGENT

    _, tid2 = await adapter.post_message(
        bot_token="bot-token-123",
        channel_id="111111",
        thread_name="Alert Thread",
        message_payload=payload,
        thread_id="forum-thread-1",
    )
    assert tid2 == "forum-thread-1"

    _, tid3 = await adapter.post_message(
        bot_token="bot-token-123",
        channel_id="111111",
        thread_name="Recovered Thread",
        message_payload=payload,
        thread_id="stale-thread",
    )
    assert tid3 == "forum-thread-1"

    _, tid4 = await adapter.post_message(
        bot_token="bot-token-123",
        channel_id="text-chan-77",
        thread_name="Text Fallback",
        message_payload=payload,
    )
    assert tid4 is None
    assert [c[0] for c in calls] == [
        "https://discord.com/api/v10/channels/111111/threads",
        "https://discord.com/api/v10/channels/forum-thread-1/messages",
        "https://discord.com/api/v10/channels/stale-thread/messages",
        "https://discord.com/api/v10/channels/111111/threads",
        "https://discord.com/api/v10/channels/text-chan-77/threads",
        "https://discord.com/api/v10/channels/text-chan-77/messages",
    ]


@pytest.mark.asyncio
async def test_send_resolution_releases_lock_before_discord_io_and_operator_notice_is_best_effort() -> None:
    lock_states_during_post: list[bool] = []

    class LockInspectingDiscord(InMemoryDiscordTransport):
        def __init__(self) -> None:
            super().__init__()
            self.alerter_ref: Any = None

        async def post_message(
            self,
            *,
            bot_token: str,
            channel_id: str,
            thread_name: str,
            message_payload: dict[str, Any],
            thread_id: str | None = None,
        ):
            if self.alerter_ref is not None:
                lock_states_during_post.append(self.alerter_ref._lock.locked())
            await asyncio.sleep(0)
            return await super().post_message(
                bot_token=bot_token,
                channel_id=channel_id,
                thread_name=thread_name,
                message_payload=message_payload,
                thread_id=thread_id,
            )

    from app.alerting import AlertingConfig, OutboundAlerter
    from app.models.schemas import DiscordAlertRequest

    discord = LockInspectingDiscord()
    alerter = OutboundAlerter(
        config=AlertingConfig(
            bot_token="bot-token-123",
            alert_channel_id="ch-alert",
            info_channel_id="ch-info",
            health_channel_id="ch-health",
            wealth_channel_id="ch-wealth",
            default_user_id="user-1",
        ),
        discord=discord,
        jev=InMemoryJevClient(),
        clock=FakeClock(),
    )
    discord.alerter_ref = alerter

    await alerter.send_alert(
        DiscordAlertRequest(
            service="charon",
            title="Backup Check",
            details="Running",
            severity="error",
            triage=False,
        )
    )
    res = await alerter.send_alert(
        DiscordAlertRequest(
            service="charon",
            title="Backup Check",
            details="Recovered",
            severity="info",
            triage=False,
        )
    )
    assert res["status"] == "resolved"
    assert lock_states_during_post == [False, False]

    broken_alerter = OutboundAlerter(
        config=AlertingConfig(
            bot_token=None,
            alert_channel_id=None,
            info_channel_id=None,
            health_channel_id=None,
            wealth_channel_id=None,
            default_user_id=None,
        ),
        discord=InMemoryDiscordTransport(),
        jev=InMemoryJevClient(),
        clock=FakeClock(),
    )
    assert (
        await broken_alerter.send_operator_notice(
            service="iris-inbound",
            bot_name="Iris Inbound Gateway",
            title="Needs Review",
            details="Operator check",
        )
        is None
    )
