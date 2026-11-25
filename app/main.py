import asyncio
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, status

from app.container import IrisContainer, build_container
from app.core.config import settings
from app.mail import MailConfigError, MailDeliveryError, OutboundMailer
from app.models.schemas import (
    AlertTriageRequest,
    AlertmanagerWebhookPayload,
    DiscordAlertRequest,
    NotificationRequest,
    TriageResult,
)
from app.routers import api_emails, webhook_email


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


def _register_notification_routes(
    fastapi_app: FastAPI,
    resolved_container: IrisContainer,
) -> None:
    @fastapi_app.post("/notify", status_code=status.HTTP_200_OK)
    async def send_notification(request: NotificationRequest):
        if request.channel == "email":
            return await _dispatch_email_notification(resolved_container.mailer, request)
        if request.channel == "discord":
            try:
                return await resolved_container.alerter.notify_notification(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Discord notification failed: {exc}") from exc
        raise HTTPException(status_code=400, detail="Unsupported channel")

    @fastapi_app.post("/notify/discord", status_code=status.HTTP_200_OK)
    async def send_discord_alert(request: DiscordAlertRequest):
        try:
            return await resolved_container.alerter.notify_discord(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Discord notification failed: {exc}") from exc


def _register_alert_routes(
    fastapi_app: FastAPI,
    resolved_container: IrisContainer,
) -> None:
    @fastapi_app.post("/triage/alert", response_model=TriageResult, status_code=status.HTTP_200_OK)
    async def triage_alert(request: AlertTriageRequest):
        return await resolved_container.alerter.triage_alert(
            service=request.service,
            title=request.title,
            details=request.details,
            severity=request.severity,
            node=request.node,
        )

    @fastapi_app.post("/webhook/alertmanager", status_code=status.HTTP_200_OK)
    async def handle_alertmanager_webhook(payload: AlertmanagerWebhookPayload):
        return await resolved_container.alerter.process_alertmanager_webhook(payload)

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
    _register_alert_routes(fastapi_app, resolved_container)
    return fastapi_app


container = build_container(settings)
app = create_app(container)
