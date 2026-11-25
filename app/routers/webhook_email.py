from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.auth import verify_webhook_secret
from app.db.session import get_db
from app.inbound import build_ingest_response
from app.models.schemas import InboundEmailIngestResponse, InboundEmailWebhookPayload

router = APIRouter(tags=["Inbound Email Webhook"])


@router.post("/webhook/email", response_model=InboundEmailIngestResponse)
async def handle_inbound_email_webhook(
    request: Request,
    payload: InboundEmailWebhookPayload,
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_webhook_secret),
):
    """Receive normalized inbound emails from Cloudflare Email Worker."""
    pipeline = request.app.state.container.inbound
    result = await pipeline.ingest(db=db, payload=payload)
    response_model = build_ingest_response(result)
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE if result.delivery_unavailable else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=http_status,
        content=response_model.model_dump(mode="json"),
    )
