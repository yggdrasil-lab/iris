import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status

from app.container import IrisContainer, build_container
from app.core.config import settings
from app.mail import MailConfigError, MailDeliveryError, OutboundMailer
from app.models.schemas import (
    AlertTriageRequest,
    AlertmanagerWebhookAlert,
    AlertmanagerWebhookPayload,
    DiscordAlertRequest,
    NotificationRequest,
    TriageResult,
)
from app.routers import api_emails, webhook_email
from app.services.discord_service import DiscordService
from app.services.triage_service import TriageService

logger = logging.getLogger("iris")

# Retained at module scope until Slice 2 moves Discord and Triage into IrisContainer.
discord_service = DiscordService()
triage_service = TriageService()


async def _dispatch_email_notification(
    mailer: OutboundMailer,
    request: NotificationRequest,
) -> Dict[str, str]:
    if not request.subject:
        raise HTTPException(status_code=400, detail="Subject is required for email channel")
    if not request.recipient:
        raise HTTPException(status_code=400, detail="Recipient is required for email channel")
    if not request.body:
        raise HTTPException(status_code=400, detail="Body is required for email channel")

    try:
        result = await asyncio.to_thread(
            mailer.send,
            recipient=request.recipient,
            subject=request.subject,
            body=request.body,
            sender=request.sender,
        )
        return {"status": "success", "message_id": result.message_id}
    except (MailConfigError, MailDeliveryError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _dispatch_discord_alert(request: DiscordAlertRequest) -> Dict[str, Any]:
    try:
        should_triage = (
            request.triage
            if request.triage is not None
            else settings.IRIS_ALERT_TRIAGE_ENABLED
        )
        triage_result = None
        if should_triage and request.severity != "info":
            triage_result = await triage_service.evaluate_alert(
                service=request.service,
                title=request.title,
                details=request.details,
                severity=request.severity,
                node=request.node,
            )
        return await discord_service.send_alert(request, triage_result=triage_result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Discord notification failed: {exc}") from exc


async def _dispatch_notify_discord(request: NotificationRequest) -> Dict[str, Any]:
    if not request.service or not request.title or not request.details:
        raise HTTPException(
            status_code=400,
            detail="service, title, and details are required for discord channel",
        )
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
    return await _dispatch_discord_alert(alert_req)


def _map_alertmanager_severity(
    alert_status: str,
    raw_severity: str,
    is_overall_resolved: bool,
) -> str:
    if is_overall_resolved or alert_status.lower() == "resolved":
        return "info"
    normalized = raw_severity.lower()
    if normalized in ("critical", "error", "fatal"):
        return "error"
    if normalized in ("warning", "warn"):
        return "warning"
    return "info"


def _build_alertmanager_request(
    alert: AlertmanagerWebhookAlert,
    is_overall_resolved: bool,
) -> tuple[str, str, DiscordAlertRequest]:
    alertname = alert.labels.get("alertname", "UnknownAlert")
    service = alert.labels.get("job") or alert.labels.get("service") or alertname
    instance = alert.labels.get("instance", "yggdrasil")
    node = alert.labels.get("node") or instance.split(":")[0]
    summary = alert.annotations.get("summary") or alert.annotations.get("description") or alertname
    description = (
        alert.annotations.get("description")
        or alert.annotations.get("summary")
        or f"Alertmanager alert: {alertname}"
    )
    mapped_severity = _map_alertmanager_severity(
        alert.status,
        alert.labels.get("severity", "warning"),
        is_overall_resolved,
    )
    alert_req = DiscordAlertRequest(
        service=service,
        title=summary,
        details=description,
        severity=mapped_severity,
        node=node,
        bot_name=f"{service.capitalize()} Monitor",
    )
    return alertname, mapped_severity, alert_req


async def _process_alertmanager_alert(
    alert: AlertmanagerWebhookAlert,
    is_overall_resolved: bool,
) -> tuple[str, Dict[str, Any]]:
    alertname, mapped_severity, alert_req = _build_alertmanager_request(
        alert, is_overall_resolved
    )
    try:
        if mapped_severity == "info":
            res = await discord_service.send_alert(alert_req)
            return "resolved", {"alertname": alertname, "status": "resolved", "detail": res}

        triage_res = await triage_service.evaluate_alert(
            service=alert_req.service,
            title=alert_req.title,
            details=alert_req.details,
            severity=mapped_severity,
            node=alert_req.node,
        )
        res = await discord_service.send_alert(alert_req, triage_result=triage_res)
        delivery_status = res.get("status")
        outcome = "suppressed" if delivery_status == "suppressed" else "delivered"
        return outcome, {"alertname": alertname, "status": delivery_status, "detail": res}
    except Exception as exc:
        logger.error("Failed to process Alertmanager alert '%s': %s", alertname, exc)
        return "error", {"alertname": alertname, "status": "error", "error": str(exc)}


def _register_notification_routes(
    fastapi_app: FastAPI,
    resolved_container: IrisContainer,
) -> None:
    @fastapi_app.post("/notify", status_code=status.HTTP_200_OK)
    async def send_notification(request: NotificationRequest):
        if request.channel == "email":
            return await _dispatch_email_notification(resolved_container.mailer, request)
        if request.channel == "discord":
            return await _dispatch_notify_discord(request)
        raise HTTPException(status_code=400, detail="Unsupported channel")

    @fastapi_app.post("/notify/discord", status_code=status.HTTP_200_OK)
    async def send_discord_alert(request: DiscordAlertRequest):
        return await _dispatch_discord_alert(request)


def _register_alert_routes(fastapi_app: FastAPI) -> None:
    @fastapi_app.post("/triage/alert", response_model=TriageResult, status_code=status.HTTP_200_OK)
    async def triage_alert(request: AlertTriageRequest):
        return await triage_service.evaluate_alert(
            service=request.service,
            title=request.title,
            details=request.details,
            severity=request.severity,
            node=request.node,
        )

    @fastapi_app.post("/webhook/alertmanager", status_code=status.HTTP_200_OK)
    async def handle_alertmanager_webhook(payload: AlertmanagerWebhookPayload):
        results: List[Dict[str, Any]] = []
        delivered_count = 0
        suppressed_count = 0
        is_overall_resolved = payload.status.lower() == "resolved"

        for alert in payload.alerts:
            outcome, entry = await _process_alertmanager_alert(alert, is_overall_resolved)
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

    @fastapi_app.get("/health")
    def health_check():
        return {"status": "ok"}


def create_app(container: Optional[IrisContainer] = None) -> FastAPI:
    resolved_container = container or build_container(settings)
    fastapi_app = FastAPI(title="Iris Notification Gateway")
    fastapi_app.state.container = resolved_container
    fastapi_app.include_router(webhook_email.router)
    fastapi_app.include_router(api_emails.router)
    _register_notification_routes(fastapi_app, resolved_container)
    _register_alert_routes(fastapi_app)
    return fastapi_app


container = build_container(settings)
app = create_app(container)
