import asyncio
from fastapi import FastAPI, HTTPException, status
from app.models.schemas import NotificationRequest, DiscordAlertRequest
from app.services.email_service import EmailService
from app.services.discord_service import DiscordService
from botocore.exceptions import ClientError

app = FastAPI(title="Iris Notification Gateway")

# Initialize services
email_service = EmailService()
discord_service = DiscordService()

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
            node=request.node
        )
        try:
            return await discord_service.send_alert(alert_req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Discord notification failed: {e}")

    else:
        raise HTTPException(status_code=400, detail="Unsupported channel")

@app.post("/notify/discord", status_code=status.HTTP_200_OK)
async def send_discord_alert(request: DiscordAlertRequest):
    try:
        return await discord_service.send_alert(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discord notification failed: {e}")

@app.get("/health")
def health_check():
    return {"status": "ok"}
