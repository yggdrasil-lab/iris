from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.auth import verify_webhook_secret
from app.db.session import get_db
from app.models.schemas import InboundEmailIngestResponse, InboundEmailWebhookPayload
from app.services.inbound_email_service import ingest_email

router = APIRouter(tags=["Inbound Email Webhook"])


@router.post("/webhook/email", response_model=InboundEmailIngestResponse)
async def handle_inbound_email_webhook(
    payload: InboundEmailWebhookPayload,
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_webhook_secret),
):
    """Receive normalized inbound emails from Cloudflare Email Worker."""
    result, status_code = await ingest_email(db=db, payload=payload)
    return JSONResponse(
        status_code=status_code,
        content=result.model_dump(mode="json"),
    )
