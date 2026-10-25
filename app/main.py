import asyncio
import logging
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException, status
from botocore.exceptions import ClientError

from app.core.config import settings
from app.models.schemas import (
    AlertTriageRequest,
    AlertmanagerWebhookPayload,
    DiscordAlertRequest,
    NotificationRequest,
    TriageResult,
)
from app.services.email_service import EmailService
from app.services.discord_service import DiscordService
from app.services.triage_service import TriageService

logger = logging.getLogger("iris")

app = FastAPI(title="Iris Notification Gateway")

# Initialize services
email_service = EmailService()
discord_service = DiscordService()
triage_service = TriageService()

@app.post("/notify", status_code=status.HTTP_200_OK)
async def send_notification(request: NotificationRequest):
    if request.channel == "email":
        if not request.subject:
            raise HTTPException(status_code=400, detail="Subject is required for email channel")
        if not request.recipient:
            raise HTTPException(status_code=400, detail="Recipient is required for email channel")
        if not request.body:
            raise HTTPException(status_code=400, detail="Body is required for email channel")

        try:
            response = await asyncio.to_thread(
                email_service.send_email,
                recipient=request.recipient,
                subject=request.subject,
                body=request.body,
                sender=request.sender
            )
            return {"status": "success", "message_id": response["MessageId"]}
        except ClientError as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif request.channel == "discord":
        if not request.service or not request.title or not request.details:
            raise HTTPException(
                status_code=400,
                detail="service, title, and details are required for discord channel"
            )

        alert_req = DiscordAlertRequest(
            service=request.service,
            title=request.title,
            details=request.details,
            severity=request.severity or "error",
            bot_name=request.bot_name,
            avatar_url=request.avatar_url,
            user_id=request.user_id,
            webhook_url=request.webhook_url,
            cooldown_seconds=request.cooldown_seconds,
            node=request.node,
            triage=request.triage
        )
        return await send_discord_alert(alert_req)

    else:
        raise HTTPException(status_code=400, detail="Unsupported channel")

@app.post("/notify/discord", status_code=status.HTTP_200_OK)
async def send_discord_alert(request: DiscordAlertRequest):
    try:
        # Determine whether Jev decision triage should be evaluated
        should_triage = (
            request.triage
            if request.triage is not None
            else settings.IRIS_ALERT_TRIAGE_ENABLED
        )

        triage_result = None
        # Only evaluate triage for active alerts; resolutions (info) bypass gating
        if should_triage and request.severity != "info":
            triage_result = await triage_service.evaluate_alert(
                service=request.service,
                title=request.title,
                details=request.details,
                severity=request.severity,
                node=request.node
            )

        return await discord_service.send_alert(request, triage_result=triage_result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discord notification failed: {e}")

@app.post("/triage/alert", response_model=TriageResult, status_code=status.HTTP_200_OK)
async def triage_alert(request: AlertTriageRequest):
    """Explicit Jev decision triage endpoint for operational alerts.

    Evaluates operational urgency (Score 1-10), noise suppression (Noul),
    and channel routing (Choice) without dispatching notifications.
    """
    return await triage_service.evaluate_alert(
        service=request.service,
        title=request.title,
        details=request.details,
        severity=request.severity,
        node=request.node
    )

@app.post("/webhook/alertmanager", status_code=status.HTTP_200_OK)
async def handle_alertmanager_webhook(payload: AlertmanagerWebhookPayload):
    """Receives Prometheus Alertmanager webhook alerts.

    Applies Jev decision triage to suppress transient deploy/restart blips,
    routes actionable incidents to Discord, and automatically resolves active threads.
    """
    results: List[Dict[str, Any]] = []
    delivered_count = 0
    suppressed_count = 0

    is_overall_resolved = payload.status.lower() == "resolved"

    for alert in payload.alerts:
        alertname = alert.labels.get("alertname", "UnknownAlert")
        service = alert.labels.get("job") or alert.labels.get("service") or alertname
        instance = alert.labels.get("instance", "yggdrasil")
        node = alert.labels.get("node") or instance.split(":")[0]

        summary = alert.annotations.get("summary") or alert.annotations.get("description") or alertname
        description = alert.annotations.get("description") or alert.annotations.get("summary") or f"Alertmanager alert: {alertname}"

        # Resolve severity mapping
        raw_sev = alert.labels.get("severity", "warning").lower()
        if is_overall_resolved or alert.status.lower() == "resolved":
            mapped_severity = "info"
        elif raw_sev in ("critical", "error", "fatal"):
            mapped_severity = "error"
        elif raw_sev in ("warning", "warn"):
            mapped_severity = "warning"
        else:
            mapped_severity = "info"

        alert_req = DiscordAlertRequest(
            service=service,
            title=summary,
            details=description,
            severity=mapped_severity,
            node=node,
            bot_name=f"{service.capitalize()} Monitor"
        )

        try:
            # Resolutions bypass suppression; firing alerts are triaged via Jev
            if mapped_severity == "info":
                res = await discord_service.send_alert(alert_req)
                results.append({"alertname": alertname, "status": "resolved", "detail": res})
            else:
                triage_res = await triage_service.evaluate_alert(
                    service=service,
                    title=summary,
                    details=description,
                    severity=mapped_severity,
                    node=node
                )
                res = await discord_service.send_alert(alert_req, triage_result=triage_res)
                if res.get("status") == "suppressed":
                    suppressed_count += 1
                else:
                    delivered_count += 1
                results.append({"alertname": alertname, "status": res.get("status"), "detail": res})
        except Exception as e:
            logger.error("Failed to process Alertmanager alert '%s': %s", alertname, e)
            results.append({"alertname": alertname, "status": "error", "error": str(e)})

    return {
        "status": "processed",
        "total_alerts": len(payload.alerts),
        "delivered": delivered_count,
        "suppressed": suppressed_count,
        "results": results
    }

@app.get("/health")
def health_check():
    return {"status": "ok"}
