import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.alerting.alertmanager import build_alertmanager_request
from app.alerting.ports import AlertingConfig, ClockPort, DiscordTransportPort
from app.alerting.triage import evaluate_alert_triage
from app.jev import JevClientPort
from app.models.schemas import (
    AlertmanagerWebhookAlert,
    AlertmanagerWebhookPayload,
    DiscordAlertRequest,
    NotificationRequest,
    TriageResult,
)

logger = logging.getLogger("iris.alerting")

SEVERITY_COLORS: Dict[str, int] = {
    "error": 15158332,    # 0xE74C3C (Red)
    "warning": 15844351,  # 0xF1C40F (Amber)
    "info": 3066993,      # 0x2ECC71 (Green)
}


@dataclass
class _IncidentState:
    last_sent: float
    thread_id: Optional[str]
    count: int


def _build_discord_message_payload(
    *,
    bot_name: str,
    avatar_url: Optional[str],
    mention: str,
    service: str,
    node: Optional[str],
    severity: str,
    title: str,
    details: str,
    timestamp_iso: str,
    triage_result: Optional[TriageResult] = None,
) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = [
        {"name": "Service", "value": f"`{service}`", "inline": True},
        {"name": "Node", "value": f"`{node or 'yggdrasil'}`", "inline": True},
        {"name": "Severity", "value": f"`{severity}`", "inline": True},
    ]
    if triage_result is not None:
        gating = f"Score `{triage_result.urgency_score}/10` · Conf `{triage_result.confidence:.2f}` · `{triage_result.destination_channel}`"
        fields.append({"name": "Jev Gating", "value": gating, "inline": True})
    fields.append({"name": "Details", "value": f"```text\n{details[:980]}\n```"})

    author: Dict[str, Any] = {"name": bot_name[:256]}
    if avatar_url:
        author["icon_url"] = avatar_url
    icon = "✅" if severity == "info" else "🚨"
    embed = {
        "author": author,
        "title": f"{icon} {service} Alert: {title}"[:256],
        "color": SEVERITY_COLORS.get(severity, SEVERITY_COLORS["error"]),
        "fields": fields,
        "footer": {"text": "Yggdrasil Monitoring · Iris Gateway"},
        "timestamp": timestamp_iso,
    }
    return {"content": mention[:1900], "embeds": [embed]}


class OutboundAlerter:
    """Deep outbound alerting domain module handling Jev triage, cooldowns, threads, and Alertmanager webhooks."""

    def __init__(
        self,
        *,
        config: AlertingConfig,
        discord: DiscordTransportPort,
        jev: JevClientPort,
        clock: ClockPort,
    ) -> None:
        self._config = config
        self._discord = discord
        self._jev = jev
        self._clock = clock
        self._lock = asyncio.Lock()
        self._incidents: Dict[str, _IncidentState] = {}

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
            "decisions": self._config.info_channel_id,
            "info": self._config.info_channel_id,
            "health": self._config.health_channel_id,
            "wealth": self._config.wealth_channel_id,
            "alerts": self._config.alert_channel_id,
        }
        raw_channel = channel_map.get(dest, self._config.alert_channel_id)
        resolved = (raw_channel or "").strip()
        if not resolved:
            raise ValueError(f"Discord channel ID is not configured for destination '{dest}'")
        return resolved

    def _check_triage_gate_result(
        self,
        request: DiscordAlertRequest,
        triage_result: Optional[TriageResult],
    ) -> Optional[Dict[str, Any]]:
        if triage_result is None:
            return None
        if triage_result.suppress:
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
        if triage_result.destination_channel == "metrics":
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
        return None

    async def triage_alert(
        self,
        *,
        service: str,
        title: str,
        details: str,
        severity: str = "error",
        node: Optional[str] = None,
    ) -> TriageResult:
        return await evaluate_alert_triage(
            self._jev,
            service=service,
            title=title,
            details=details,
            severity=severity,
            node=node,
            score_threshold=self._config.score_threshold,
            confidence_threshold=self._config.confidence_threshold,
        )

    async def notify_discord(self, request: DiscordAlertRequest) -> Dict[str, Any]:
        should_triage = (
            request.triage
            if request.triage is not None
            else self._config.alert_triage_enabled
        )
        triage_result: Optional[TriageResult] = None
        if should_triage and request.severity != "info":
            triage_result = await self.triage_alert(
                service=request.service,
                title=request.title,
                details=request.details,
                severity=request.severity,
                node=request.node,
            )
        return await self.send_alert(request, triage_result=triage_result)

    async def notify_notification(self, request: NotificationRequest) -> Dict[str, Any]:
        if not request.service or not request.title or not request.details:
            raise ValueError("service, title, and details are required for discord channel")
        alert_req = DiscordAlertRequest(
            service=request.service,
            title=request.title,
            details=request.details,
            severity=request.severity or "error",
            bot_name=request.bot_name,
            avatar_url=request.avatar_url,
            user_id=request.user_id,
            channel_id=request.channel_id,
            destination_channel=request.destination_channel,
            cooldown_seconds=request.cooldown_seconds,
            node=request.node,
            triage=request.triage,
        )
        return await self.notify_discord(alert_req)

    async def send_operator_notice(
        self,
        *,
        service: str,
        bot_name: str,
        title: str,
        details: str,
        severity: Literal["error", "warning"] = "warning",
        destination_channel: str = "decisions",
    ) -> Optional[Dict[str, Any]]:
        """Deliver a best-effort non-triaged operator notice without raising into callers."""
        req = DiscordAlertRequest(
            service=service,
            bot_name=bot_name,
            title=title[:120],
            details=details[:1500],
            severity=severity,
            destination_channel=destination_channel,
            triage=False,
        )
        try:
            return await self.send_alert(req)
        except Exception as exc:
            logger.warning("Best-effort operator notice skipped/failed (%s): %s", title, exc)
            return None

    async def _send_resolution(
        self,
        *,
        slug: str,
        bot_token: str,
        channel_id: str,
        bot_name: str,
        request: DiscordAlertRequest,
    ) -> Dict[str, Any]:
        async with self._lock:
            incident = self._incidents.get(slug)
            thread_id = incident.thread_id if incident else None
        payload = _build_discord_message_payload(
            bot_name=bot_name,
            avatar_url=request.avatar_url,
            mention="",
            service=request.service,
            node=request.node,
            severity="info",
            title=f"Resolved: {request.title}",
            details=request.details,
            timestamp_iso=self._clock.now_utc().isoformat(),
        )
        result, resolved_thread_id = await self._discord.post_message(
            bot_token=bot_token,
            channel_id=channel_id,
            thread_name=f"✅ {request.service} Resolved: {request.title}",
            message_payload=payload,
            thread_id=thread_id,
        )
        async with self._lock:
            self._incidents.pop(slug, None)
        return {
            "status": "resolved",
            "channel_id": channel_id,
            "thread_id": resolved_thread_id,
            "discord_response": result,
        }

    async def _reserve_incident_slot(
        self,
        *,
        slug: str,
        cooldown: int,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
        now = self._clock.now_seconds()
        async with self._lock:
            incident = self._incidents.get(slug)
            if incident is None:
                return None, None, 1
            elapsed = now - incident.last_sent
            if elapsed < cooldown:
                incident.count += 1
                remaining = int(cooldown - elapsed)
                logger.info(
                    "Alert throttled for %s (cooldown %ds remaining, suppressed count: %d)",
                    slug,
                    remaining,
                    incident.count,
                )
                return (
                    {
                        "status": "throttled",
                        "reason": f"Alert throttled. Cooldown active ({remaining}s remaining).",
                        "suppressed_count": incident.count,
                        "thread_id": incident.thread_id,
                    },
                    incident.thread_id,
                    incident.count,
                )
            return None, incident.thread_id, incident.count + 1

    async def _dispatch_active_alert(
        self, *, slug: str, bot_token: str, channel_id: str, bot_name: str,
        request: DiscordAlertRequest, triage_result: Optional[TriageResult],
        thread_id: Optional[str], incident_count: int,
    ) -> Dict[str, Any]:
        user_id = request.user_id or self._config.default_user_id
        mention = f"<@{user_id}>" if (user_id and not thread_id) else ""
        title = request.title if not thread_id else f"{request.title} (Recurring)"
        payload = _build_discord_message_payload(
            bot_name=bot_name, avatar_url=request.avatar_url, mention=mention,
            service=request.service, node=request.node, severity=request.severity,
            title=title, details=request.details,
            timestamp_iso=self._clock.now_utc().isoformat(), triage_result=triage_result,
        )
        _, new_thread_id = await self._discord.post_message(
            bot_token=bot_token, channel_id=channel_id,
            thread_name=f"🚨 {request.service} Alert: {request.title}",
            message_payload=payload, thread_id=thread_id,
        )
        async with self._lock:
            self._incidents[slug] = _IncidentState(
                last_sent=self._clock.now_seconds(), thread_id=new_thread_id, count=incident_count
            )
        return {
            "status": "delivered",
            "channel_id": channel_id,
            "thread_id": new_thread_id,
            "incident_count": incident_count,
            "triage": triage_result.model_dump() if triage_result else None,
        }

    async def send_alert(
        self, request: DiscordAlertRequest, triage_result: Optional[TriageResult] = None
    ) -> Dict[str, Any]:
        gated = self._check_triage_gate_result(request, triage_result)
        if gated is not None:
            return gated
        bot_token = (self._config.bot_token or "").strip()
        if not bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is not configured")
        channel_id = self._resolve_channel_id(request, triage_result=triage_result)
        bot_name = request.bot_name or f"{request.service.capitalize()} Alerts"
        slug = f"{request.service.lower()}:{request.title.strip().lower()}"
        if request.severity == "info":
            return await self._send_resolution(
                slug=slug, bot_token=bot_token, channel_id=channel_id, bot_name=bot_name, request=request
            )
        cooldown = (
            request.cooldown_seconds
            if request.cooldown_seconds is not None
            else self._config.default_cooldown_seconds
        )
        throttled, thread_id, incident_count = await self._reserve_incident_slot(
            slug=slug, cooldown=cooldown
        )
        if throttled is not None:
            return throttled
        return await self._dispatch_active_alert(
            slug=slug, bot_token=bot_token, channel_id=channel_id, bot_name=bot_name,
            request=request, triage_result=triage_result, thread_id=thread_id, incident_count=incident_count,
        )

    async def _process_alertmanager_alert(
        self,
        alert: AlertmanagerWebhookAlert,
        is_overall_resolved: bool,
    ) -> Tuple[str, Dict[str, Any]]:
        alertname, mapped_severity, alert_req = build_alertmanager_request(
            alert,
            is_overall_resolved,
        )
        try:
            if mapped_severity == "info":
                res = await self.send_alert(alert_req)
                return "resolved", {"alertname": alertname, "status": "resolved", "detail": res}

            res = await self.notify_discord(alert_req)
            delivery_status = res.get("status")
            outcome = "suppressed" if delivery_status == "suppressed" else "delivered"
            return outcome, {"alertname": alertname, "status": delivery_status, "detail": res}
        except Exception as exc:
            logger.error("Failed to process Alertmanager alert '%s': %s", alertname, exc)
            return "error", {"alertname": alertname, "status": "error", "error": str(exc)}

    async def process_alertmanager_webhook(
        self,
        payload: AlertmanagerWebhookPayload,
    ) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        delivered_count = 0
        suppressed_count = 0
        is_overall_resolved = payload.status.lower() == "resolved"

        for alert in payload.alerts:
            outcome, entry = await self._process_alertmanager_alert(alert, is_overall_resolved)
            if outcome == "suppressed":
                suppressed_count += 1
            elif outcome == "delivered":
                delivered_count += 1
            results.append(entry)

        return {
            "status": "processed",
            "total_alerts": len(payload.alerts),
            "delivered": delivered_count,
            "suppressed": suppressed_count,
            "results": results,
        }
