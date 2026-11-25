import logging
import math
from typing import Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.auth import require_authentik_operator
from app.db.session import get_db
from app.inbound import InboundEmailNotFoundError, InvalidRouteTargetError
from app.models.schemas import (
    InboundEmailDetailResponse,
    InboundEmailListResponse,
    InboundEmailRead,
    InboundEmailStatsResponse,
    RerouteEmailRequest,
    RetryEmailRequest,
)

logger = logging.getLogger("iris.api.emails")

router = APIRouter(prefix="/api/emails", tags=["Inbound Emails"])


@router.get("", response_model=InboundEmailListResponse)
def list_inbound_emails(
    request: Request,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    route_target: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    _operator: str = Depends(require_authentik_operator),
):
    """List captured inbound emails with optional status, route, search, and pagination."""
    pipeline = request.app.state.container.inbound
    items, total = pipeline.list_emails(
        db=db,
        status_filter=status_filter,
        route_target=route_target,
        query_text=q,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1
    return InboundEmailListResponse(
        items=[InboundEmailRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/stats", response_model=InboundEmailStatsResponse)
def get_inbound_email_stats(
    request: Request,
    db: Session = Depends(get_db),
    _operator: str = Depends(require_authentik_operator),
):
    """Return aggregate counts grouped by dispatch_status and route_target."""
    pipeline = request.app.state.container.inbound
    total, by_status, by_route_target = pipeline.get_email_stats(db)
    return InboundEmailStatsResponse(
        total=total,
        by_status=by_status,
        by_route_target=by_route_target,
    )


@router.get("/{email_id}", response_model=InboundEmailDetailResponse)
def get_inbound_email_detail(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
    _operator: str = Depends(require_authentik_operator),
):
    """Return full details and stored attachments for a single inbound email."""
    pipeline = request.app.state.container.inbound
    email_row = pipeline.get_email_with_attachments(db, email_id)
    if email_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbound email not found",
        )
    return InboundEmailDetailResponse.model_validate(email_row)


@router.post("/{email_id}/retry", response_model=InboundEmailDetailResponse)
async def retry_inbound_email(
    request: Request,
    email_id: int,
    payload: Optional[RetryEmailRequest] = Body(default=None),
    db: Session = Depends(get_db),
    operator: str = Depends(require_authentik_operator),
):
    """Retry dispatching an email or optionally reclassify via Jev."""
    pipeline = request.app.state.container.inbound
    reclassify = bool(payload.reclassify) if payload is not None else False
    logger.info("Operator %s triggered retry for email_id=%d (reclassify=%s)", operator, email_id, reclassify)
    try:
        result = await pipeline.retry(db=db, email_id=email_id, reclassify=reclassify)
    except InboundEmailNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbound email not found",
        ) from exc
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE if result.delivery_unavailable else status.HTTP_200_OK
    )
    detail = InboundEmailDetailResponse.model_validate(result.email)
    return JSONResponse(status_code=http_status, content=detail.model_dump(mode="json"))


@router.post("/{email_id}/reroute", response_model=InboundEmailDetailResponse)
async def reroute_inbound_email(
    request: Request,
    email_id: int,
    payload: RerouteEmailRequest,
    db: Session = Depends(get_db),
    operator: str = Depends(require_authentik_operator),
):
    """Manually override route_target and dispatch the inbound email."""
    pipeline = request.app.state.container.inbound
    logger.info(
        "Operator %s rerouting email_id=%d to route_target=%s",
        operator,
        email_id,
        payload.route_target,
    )
    try:
        result = await pipeline.reroute(
            db=db,
            email_id=email_id,
            new_target=payload.route_target,
        )
    except InvalidRouteTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except InboundEmailNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbound email not found",
        ) from exc
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE if result.delivery_unavailable else status.HTTP_200_OK
    )
    detail = InboundEmailDetailResponse.model_validate(result.email)
    return JSONResponse(status_code=http_status, content=detail.model_dump(mode="json"))


@router.post("/{email_id}/ignore", response_model=InboundEmailDetailResponse)
def ignore_inbound_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
    operator: str = Depends(require_authentik_operator),
):
    """Mark an inbound email as ignored without downstream dispatch."""
    pipeline = request.app.state.container.inbound
    logger.info("Operator %s marked email_id=%d as ignored", operator, email_id)
    try:
        result = pipeline.ignore(db=db, email_id=email_id)
    except InboundEmailNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbound email not found",
        ) from exc
    return InboundEmailDetailResponse.model_validate(result.email)
