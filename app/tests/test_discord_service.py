"""Unit tests for DiscordService Bot REST API v10 integration."""

from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.models.schemas import DiscordAlertRequest, TriageResult
from app.services.discord_service import DiscordService


def _mock_async_client(monkeypatch, handler) -> None:
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.init_headers = kwargs.get("headers") or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None):
            merged_headers = {**self.init_headers, **(headers or {})}
            return await handler(url, json, merged_headers)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_discord_bot_forum_thread_creation_and_followup_reply(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "111111")
    monkeypatch.setattr(settings, "DISCORD_USER_ID", "302843723715182592")

    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        calls.append((url, body, headers))
        if url.endswith("/channels/111111/threads"):
            return httpx.Response(201, json={"id": "thread-new-1"}, request=httpx.Request("POST", url))
        if url.endswith("/channels/thread-new-1/messages"):
            return httpx.Response(200, json={"id": "msg-2", "channel_id": "thread-new-1"}, request=httpx.Request("POST", url))
        raise AssertionError(f"Unexpected URL: {url}")

    _mock_async_client(monkeypatch, fake_post)
    svc = DiscordService()

    req = DiscordAlertRequest(
        service="charon",
        bot_name="Charon Bot",
        avatar_url="https://cdn.example.com/charon.png",
        title="Bisync Failed",
        details="Cache lock missing",
        severity="error",
        cooldown_seconds=0,
    )
    first = await svc.send_alert(req)
    assert first["status"] == "delivered"
    assert first["channel_id"] == "111111"
    assert first["thread_id"] == "thread-new-1"

    url1, body1, hdr1 = calls[0]
    assert url1 == "https://discord.com/api/v10/channels/111111/threads"
    assert hdr1["Authorization"] == "Bot bot-token-123"
    assert hdr1["User-Agent"] == "Yggdrasil-Iris/1.0 (DiscordBot)"
    assert hdr1["Content-Type"] == "application/json"
    assert "username" not in body1
    assert "avatar_url" not in body1
    assert body1["name"].startswith("🚨 charon Alert: Bisync Failed")
    assert body1["message"]["content"] == "<@302843723715182592>"
    embed1 = body1["message"]["embeds"][0]
    assert embed1["author"] == {
        "name": "Charon Bot",
        "icon_url": "https://cdn.example.com/charon.png",
    }

    # Second alert reuses existing thread via /channels/{thread_id}/messages
    second = await svc.send_alert(req)
    assert second["status"] == "delivered"
    assert second["thread_id"] == "thread-new-1"
    url2, body2, _ = calls[1]
    assert url2 == "https://discord.com/api/v10/channels/thread-new-1/messages"
    assert body2["content"] == ""
    assert "(Recurring)" in body2["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_discord_reply_404_recreates_forum_thread(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "111111")

    calls: list[str] = []

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        calls.append(url)
        if url.endswith("/channels/stale-thread/messages"):
            return httpx.Response(404, json={"message": "Unknown Channel"}, request=httpx.Request("POST", url))
        return httpx.Response(201, json={"id": "fresh-thread-9"}, request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_post)
    svc = DiscordService()
    svc._incidents["charon:disk full"] = {
        "last_sent": 0.0,
        "thread_id": "stale-thread",
        "count": 1,
    }

    res = await svc.send_alert(
        DiscordAlertRequest(
            service="charon",
            title="Disk Full",
            details="/backups at 95%",
            severity="warning",
            cooldown_seconds=0,
        )
    )
    assert res["status"] == "delivered"
    assert res["thread_id"] == "fresh-thread-9"
    assert calls == [
        "https://discord.com/api/v10/channels/stale-thread/messages",
        "https://discord.com/api/v10/channels/111111/threads",
    ]


@pytest.mark.asyncio
async def test_discord_channel_routing_decisions_health_wealth_and_override(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "ch-alert")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_INFO_ID", "ch-info")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_HEALTH_ID", "ch-health")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_WEALTH_ID", "ch-wealth")

    seen_urls: list[str] = []

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        seen_urls.append(url)
        return httpx.Response(201, json={"id": f"th-{len(seen_urls)}"}, request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_post)
    svc = DiscordService()

    # Triage decisions -> DISCORD_CHANNEL_INFO_ID
    triage_decisions = TriageResult(
        urgency_score=7,
        confidence=0.9,
        is_transient_noise=False,
        destination_channel="decisions",
        suppress=False,
        reason="Operator review",
        provider="typesafe-jev",
    )
    await svc.send_alert(
        DiscordAlertRequest(service="iris", title="Review", details="Check", severity="warning"),
        triage_result=triage_decisions,
    )
    # Health destination -> DISCORD_CHANNEL_HEALTH_ID
    await svc.send_alert(
        DiscordAlertRequest(service="eir", title="Sleep", details="Sync ok", severity="warning", destination_channel="health")
    )
    # Wealth destination -> DISCORD_CHANNEL_WEALTH_ID
    await svc.send_alert(
        DiscordAlertRequest(service="plutus", title="Spend", details="Digest", severity="warning", destination_channel="wealth")
    )
    # Explicit channel_id override
    await svc.send_alert(
        DiscordAlertRequest(service="custom", title="Override", details="Msg", severity="warning", channel_id="ch-custom")
    )

    assert seen_urls == [
        "https://discord.com/api/v10/channels/ch-info/threads",
        "https://discord.com/api/v10/channels/ch-health/threads",
        "https://discord.com/api/v10/channels/ch-wealth/threads",
        "https://discord.com/api/v10/channels/ch-custom/threads",
    ]


@pytest.mark.asyncio
async def test_discord_standalone_info_creates_forum_thread_and_active_info_replies(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "ch-alert")

    calls: list[str] = []

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        calls.append(url)
        if url.endswith("/threads"):
            return httpx.Response(201, json={"id": "info-thread-1"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"id": "msg-ok"}, request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_post)
    svc = DiscordService()

    standalone = await svc.send_alert(
        DiscordAlertRequest(service="charon", title="Job Done", details="OK", severity="info")
    )
    assert standalone["status"] == "resolved"
    assert standalone["thread_id"] == "info-thread-1"

    # Active incident -> info reply into incident's thread
    svc._incidents["charon:job done"] = {
        "last_sent": 100.0,
        "thread_id": "active-thread-55",
        "count": 1,
    }
    resolved = await svc.send_alert(
        DiscordAlertRequest(service="charon", title="Job Done", details="Recovered", severity="info")
    )
    assert resolved["status"] == "resolved"
    assert resolved["thread_id"] == "active-thread-55"
    assert "charon:job done" not in svc._incidents
    assert calls == [
        "https://discord.com/api/v10/channels/ch-alert/threads",
        "https://discord.com/api/v10/channels/active-thread-55/messages",
    ]


@pytest.mark.asyncio
async def test_discord_text_channel_400_fallback_to_messages(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "text-chan-77")

    calls: list[str] = []

    async def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]):
        calls.append(url)
        if url.endswith("/threads"):
            return httpx.Response(400, json={"message": "Channel is not a forum"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"id": "plain-msg-1", "channel_id": "text-chan-77"}, request=httpx.Request("POST", url))

    _mock_async_client(monkeypatch, fake_post)
    svc = DiscordService()

    res = await svc.send_alert(
        DiscordAlertRequest(service="iris", title="Plain Text Channel", details="Test", severity="error")
    )
    assert res["status"] == "delivered"
    assert res["channel_id"] == "text-chan-77"
    assert res["thread_id"] is None
    assert calls == [
        "https://discord.com/api/v10/channels/text-chan-77/threads",
        "https://discord.com/api/v10/channels/text-chan-77/messages",
    ]


@pytest.mark.asyncio
async def test_discord_fails_loud_when_token_or_channel_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", "ch-alert")
    svc = DiscordService()

    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        await svc.send_alert(
            DiscordAlertRequest(service="iris", title="Missing Token", details="Fail", severity="error")
        )

    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token-123")
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_HEALTH_ID", None)

    with pytest.raises(ValueError, match="channel ID"):
        await svc.send_alert(
            DiscordAlertRequest(
                service="eir",
                title="Missing Channel",
                details="Fail",
                severity="warning",
                destination_channel="health",
            )
        )
