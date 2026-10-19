import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx
from app.core.config import settings
from app.models.schemas import DiscordAlertRequest

logger = logging.getLogger("iris.discord")

# Color mapping
SEVERITY_COLORS = {
    "error": 15158332,   # 0xE74C3C (Red)
    "warning": 15844351, # 0xF1C40F (Amber)
    "info": 3066993,     # 0x2ECC71 (Green)
}

class DiscordService:
    def __init__(self):
        self._lock = asyncio.Lock()
        # Key: "service:title", Value: {"last_sent": float, "thread_id": Optional[str], "count": int}
        self._incidents: Dict[str, Dict[str, Any]] = {}

    async def send_alert(self, request: DiscordAlertRequest) -> Dict[str, Any]:
        webhook_url = request.webhook_url or settings.DISCORD_ALERTS_WEBHOOK_URL
        if not webhook_url:
            raise ValueError("No Discord webhook URL provided and DISCORD_ALERTS_WEBHOOK_URL is not configured")

        user_id = request.user_id or settings.DISCORD_USER_ID
        bot_name = request.bot_name or f"{request.service.capitalize()} Alerts"
        cooldown = request.cooldown_seconds or settings.DISCORD_DEFAULT_COOLDOWN_SECONDS
        slug = f"{request.service.lower()}:{request.title.strip().lower()}"
        now = time.time()

        async with self._lock:
            incident = self._incidents.get(slug)

            # 1. Handle Resolution (severity == "info")
            if request.severity == "info":
                thread_id = incident.get("thread_id") if incident else None
                result = await self._dispatch_message(
                    webhook_url=webhook_url,
                    bot_name=bot_name,
                    avatar_url=request.avatar_url,
                    user_id=None, # Do not ping on resolution
                    service=request.service,
                    node=request.node,
                    severity="info",
                    title=f"Resolved: {request.title}",
                    details=request.details,
                    thread_name=None,
                    thread_id=thread_id
                )
                if slug in self._incidents:
                    del self._incidents[slug]
                return {"status": "resolved", "thread_id": thread_id, "discord_response": result}

            # 2. Handle Errors and Warnings (Anti-Spam Check)
            if incident:
                elapsed = now - incident["last_sent"]
                if elapsed < cooldown:
                    incident["count"] += 1
                    logger.info(
                        "Alert throttled for %s (cooldown %ds remaining, suppressed count: %d)",
                        slug,
                        int(cooldown - elapsed),
                        incident["count"]
                    )
                    return {
                        "status": "throttled",
                        "reason": f"Alert throttled. Cooldown active ({int(cooldown - elapsed)}s remaining).",
                        "suppressed_count": incident["count"],
                        "thread_id": incident.get("thread_id")
                    }
                # Cooldown expired, reuse thread if one was established
                thread_id = incident.get("thread_id")
                incident_count = incident.get("count", 1) + 1
            else:
                thread_id = None
                incident_count = 1

        # 3. Dispatch to Discord
        # On first alert in a thread, mention user to subscribe them. On follow-ups, silence content.
        mention = f"<@{user_id}>" if (user_id and not thread_id) else ""
        thread_name = f"🚨 {request.service} Alert: {request.title}" if not thread_id else None

        result = await self._dispatch_message(
            webhook_url=webhook_url,
            bot_name=bot_name,
            avatar_url=request.avatar_url,
            user_id=mention,
            service=request.service,
            node=request.node,
            severity=request.severity,
            title=request.title if not thread_id else f"{request.title} (Recurring)",
            details=request.details,
            thread_name=thread_name,
            thread_id=thread_id
        )

        # 4. Update Incident Cache
        new_thread_id = result.get("channel_id") or thread_id
        async with self._lock:
            self._incidents[slug] = {
                "last_sent": time.time(),
                "thread_id": new_thread_id,
                "count": incident_count
            }

        return {
            "status": "delivered",
            "thread_id": new_thread_id,
            "incident_count": incident_count
        }

    async def _dispatch_message(
        self,
        webhook_url: str,
        bot_name: str,
        avatar_url: Optional[str],
        user_id: Optional[str],
        service: str,
        node: Optional[str],
        severity: str,
        title: str,
        details: str,
        thread_name: Optional[str] = None,
        thread_id: Optional[str] = None
    ) -> Dict[str, Any]:
        color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["error"])
        icon = "🚨" if severity != "info" else "✅"
        timestamp = datetime.now(timezone.utc).isoformat()

        embed = {
            "title": f"{icon} {service} Alert: {title}",
            "color": color,
            "fields": [
                {"name": "Service", "value": f"`{service}`", "inline": True},
                {"name": "Node", "value": f"`{node or 'yggdrasil'}`", "inline": True},
                {"name": "Severity", "value": f"`{severity}`", "inline": True},
                {"name": "Details", "value": f"```text\n{details}\n```"}
            ],
            "footer": {"text": "Yggdrasil Monitoring · Iris Gateway"},
            "timestamp": timestamp
        }

        payload: Dict[str, Any] = {
            "username": bot_name,
            "content": user_id or "",
            "embeds": [embed]
        }
        if avatar_url:
            payload["avatar_url"] = avatar_url

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Case A: Post into existing thread
            if thread_id:
                url = f"{webhook_url}?thread_id={thread_id}&wait=true"
                resp = await client.post(url, json=payload)
                if resp.status_code in (200, 201):
                    return resp.json()
                logger.warning(
                    "Posting to thread_id %s failed with status %d. Falling back to fresh post.",
                    thread_id,
                    resp.status_code
                )

            # Case B: Post as a new thread (or standard message)
            if thread_name:
                payload["thread_name"] = thread_name

            url = f"{webhook_url}?wait=true"
            resp = await client.post(url, json=payload)

            # Case C: If 400 Bad Request (channel is not a Forum channel and rejected thread_name)
            if resp.status_code == 400 and "thread_name" in payload:
                logger.info("Channel rejected thread_name (likely standard text channel). Retrying without thread_name.")
                payload.pop("thread_name", None)
                resp = await client.post(url, json=payload)

            resp.raise_for_status()
            return resp.json()
