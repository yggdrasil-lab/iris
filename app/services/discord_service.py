import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import httpx
from app.core.config import settings
from app.models.schemas import DiscordAlertRequest, TriageResult

logger = logging.getLogger("iris.discord")

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "Yggdrasil-Iris/1.0 (DiscordBot)"

# Color mapping
SEVERITY_COLORS = {
    "error": 15158332,    # 0xE74C3C (Red)
    "warning": 15844351,  # 0xF1C40F (Amber)
    "info": 3066993,      # 0x2ECC71 (Green)
}


class DiscordService:
    def __init__(self):
        self._lock = asyncio.Lock()
        # Key: "service:title", Value: {"last_sent": float, "thread_id": Optional[str], "count": int}
        self._incidents: Dict[str, Dict[str, Any]] = {}

    def _resolve_channel_id(
        self,
        request: DiscordAlertRequest,
        triage_result: Optional[TriageResult] = None,
    ) -> str:
        explicit = (request.channel_id or "").strip()
        if explicit:
            return explicit

        dest_raw = (
            (triage_result.destination_channel if triage_result else None)
            or request.destination_channel
            or "alerts"
        )
        dest = dest_raw.strip().lower()

        channel_map = {
            "decisions": settings.DISCORD_CHANNEL_INFO_ID,
            "info": settings.DISCORD_CHANNEL_INFO_ID,
            "health": settings.DISCORD_CHANNEL_HEALTH_ID,
            "wealth": settings.DISCORD_CHANNEL_WEALTH_ID,
            "alerts": settings.DISCORD_CHANNEL_ALERT_ID,
        }
        if dest in channel_map:
            resolved = (channel_map[dest] or "").strip()
        else:
            resolved = (settings.DISCORD_CHANNEL_ALERT_ID or "").strip()
        if not resolved:
            raise ValueError(
                f"Discord channel ID is not configured for destination '{dest}'"
            )
        return resolved

    async def send_alert(
        self,
        request: DiscordAlertRequest,
        triage_result: Optional[TriageResult] = None,
        kanban_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Handle Jev suppression gate
        if triage_result and triage_result.suppress:
            logger.info(
                "Alert '%s:%s' suppressed by Jev triage: %s",
                request.service,
                request.title,
                triage_result.reason,
            )
            return {
                "status": "suppressed",
                "reason": triage_result.reason,
                "urgency_score": triage_result.urgency_score,
                "confidence": triage_result.confidence,
                "destination_channel": triage_result.destination_channel,
                "thread_id": None,
            }

        # Handle Jev Choice metrics routing (silent operational telemetry)
        if triage_result and triage_result.destination_channel == "metrics":
            logger.info(
                "Alert '%s:%s' routed to silent metrics by Jev Choice primitive",
                request.service,
                request.title,
            )
            return {
                "status": "metrics_logged",
                "reason": "Routed to silent metrics by Jev Choice primitive",
                "urgency_score": triage_result.urgency_score,
                "confidence": triage_result.confidence,
                "destination_channel": "metrics",
                "thread_id": None,
            }

        bot_token = (settings.DISCORD_BOT_TOKEN or "").strip()
        if not bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is not configured")

        channel_id = self._resolve_channel_id(request, triage_result=triage_result)

        user_id = request.user_id or settings.DISCORD_USER_ID
        bot_name = request.bot_name or f"{request.service.capitalize()} Alerts"
        cooldown = (
            request.cooldown_seconds
            if request.cooldown_seconds is not None
            else settings.DISCORD_DEFAULT_COOLDOWN_SECONDS
        )
        slug = f"{request.service.lower()}:{request.title.strip().lower()}"
        now = time.time()

        async with self._lock:
            incident = self._incidents.get(slug)

            # 1. Handle Resolution (severity == "info")
            if request.severity == "info":
                thread_id = incident.get("thread_id") if incident else None
                resolution_thread_name = f"✅ {request.service} Resolved: {request.title}"
                result, resolved_thread_id = await self._dispatch_message(
                    bot_token=bot_token,
                    channel_id=channel_id,
                    bot_name=bot_name,
                    avatar_url=request.avatar_url,
                    user_id=None,  # Do not ping on resolution
                    service=request.service,
                    node=request.node,
                    severity="info",
                    title=f"Resolved: {request.title}",
                    details=request.details,
                    thread_name=resolution_thread_name,
                    thread_id=thread_id,
                )
                if slug in self._incidents:
                    del self._incidents[slug]
                return {
                    "status": "resolved",
                    "channel_id": channel_id,
                    "thread_id": resolved_thread_id,
                    "discord_response": result,
                }

            # 2. Handle Errors and Warnings (Anti-Spam Check)
            if incident:
                elapsed = now - incident["last_sent"]
                if elapsed < cooldown:
                    incident["count"] += 1
                    logger.info(
                        "Alert throttled for %s (cooldown %ds remaining, suppressed count: %d)",
                        slug,
                        int(cooldown - elapsed),
                        incident["count"],
                    )
                    return {
                        "status": "throttled",
                        "reason": f"Alert throttled. Cooldown active ({int(cooldown - elapsed)}s remaining).",
                        "suppressed_count": incident["count"],
                        "thread_id": incident.get("thread_id"),
                    }
                # Cooldown expired, reuse thread if one was established
                thread_id = incident.get("thread_id")
                incident_count = incident.get("count", 1) + 1
            else:
                thread_id = None
                incident_count = 1

        # 3. Dispatch to Discord via Bot REST API v10
        mention = f"<@{user_id}>" if (user_id and not thread_id) else ""
        thread_name = f"🚨 {request.service} Alert: {request.title}"

        result, new_thread_id = await self._dispatch_message(
            bot_token=bot_token,
            channel_id=channel_id,
            bot_name=bot_name,
            avatar_url=request.avatar_url,
            user_id=mention,
            service=request.service,
            node=request.node,
            severity=request.severity,
            title=request.title if not thread_id else f"{request.title} (Recurring)",
            details=request.details,
            thread_name=thread_name,
            thread_id=thread_id,
            triage_result=triage_result,
            kanban_task_id=kanban_task_id,
        )

        # 4. Update Incident Cache
        async with self._lock:
            self._incidents[slug] = {
                "last_sent": time.time(),
                "thread_id": new_thread_id,
                "count": incident_count,
            }

        return {
            "status": "delivered",
            "channel_id": channel_id,
            "thread_id": new_thread_id,
            "incident_count": incident_count,
            "triage": triage_result.model_dump() if triage_result else None,
            "kanban_task_id": kanban_task_id,
        }

    async def _dispatch_message(
        self,
        bot_token: str,
        channel_id: str,
        bot_name: str,
        avatar_url: Optional[str],
        user_id: Optional[str],
        service: str,
        node: Optional[str],
        severity: str,
        title: str,
        details: str,
        thread_name: str,
        thread_id: Optional[str] = None,
        triage_result: Optional[TriageResult] = None,
        kanban_task_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["error"])
        icon = "🚨" if severity != "info" else "✅"
        timestamp = datetime.now(timezone.utc).isoformat()

        fields = [
            {"name": "Service", "value": f"`{service}`", "inline": True},
            {"name": "Node", "value": f"`{node or 'yggdrasil'}`", "inline": True},
            {"name": "Severity", "value": f"`{severity}`", "inline": True},
        ]

        if triage_result:
            fields.append(
                {
                    "name": "Jev Gating",
                    "value": (
                        f"Score `{triage_result.urgency_score}/10` · "
                        f"Conf `{triage_result.confidence:.2f}` · `{triage_result.destination_channel}`"
                    ),
                    "inline": True,
                }
            )

        if kanban_task_id:
            fields.append(
                {
                    "name": "Remediation Task",
                    "value": f"`{kanban_task_id}` (`swarm-worker`)",
                    "inline": True,
                }
            )

        fields.append({"name": "Details", "value": f"```text\n{details[:980]}\n```"})

        author: Dict[str, Any] = {"name": bot_name[:256]}
        if avatar_url:
            author["icon_url"] = avatar_url

        embed: Dict[str, Any] = {
            "author": author,
            "title": f"{icon} {service} Alert: {title}"[:256],
            "color": color,
            "fields": fields,
            "footer": {"text": "Yggdrasil Monitoring · Iris Gateway"},
            "timestamp": timestamp,
        }

        message_payload: Dict[str, Any] = {
            "content": (user_id or "")[:1900],
            "embeds": [embed],
        }

        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            # Case A: Reply inside existing thread
            if thread_id:
                reply_url = f"{DISCORD_API_BASE}/channels/{thread_id}/messages"
                resp = await client.post(reply_url, json=message_payload)
                if resp.status_code in (200, 201):
                    return resp.json(), thread_id
                if resp.status_code in (400, 403, 404):
                    logger.warning(
                        "Posting to thread_id %s returned status %d. Creating a new thread.",
                        thread_id,
                        resp.status_code,
                    )
                else:
                    resp.raise_for_status()

            # Case B: Create new Forum thread
            threads_url = f"{DISCORD_API_BASE}/channels/{channel_id}/threads"
            thread_payload = {
                "name": thread_name[:100],
                "message": message_payload,
            }
            resp = await client.post(threads_url, json=thread_payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                created_thread_id = str(data["id"]) if data.get("id") is not None else None
                return data, created_thread_id

            # Case C: Non-Forum standard text channel fallback on 400
            if resp.status_code == 400:
                logger.info(
                    "Channel %s rejected Forum thread payload (likely standard text channel). Retrying /messages.",
                    channel_id,
                )
                msg_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
                resp = await client.post(msg_url, json=message_payload)

            resp.raise_for_status()
            return resp.json(), None
