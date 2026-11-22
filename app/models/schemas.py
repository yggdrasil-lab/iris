import base64
import binascii
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


RouteTargetType = Literal[
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_payslip",
    "plutus_bank_statement",
    "actionable_notice",
    "ignore",
]


class TriageResult(BaseModel):
    urgency_score: int
    confidence: float
    is_transient_noise: bool
    destination_channel: Literal["alerts", "decisions", "metrics"]
    suppress: bool
    reason: str
    provider: str


class EmailRouteVerdict(BaseModel):
    route_target: RouteTargetType
    has_actionable_amount: bool
    confidence: float
    urgency_score: int
    reason: str
    provider: str


class AlertTriageRequest(BaseModel):
    service: str
    title: str
    details: str
    severity: Literal["error", "warning", "info"] = "error"
    node: Optional[str] = None


class AlertmanagerWebhookAlert(BaseModel):
    status: str
    labels: Dict[str, str] = {}
    annotations: Dict[str, str] = {}
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerWebhookPayload(BaseModel):
    version: Optional[str] = "4"
    groupKey: Optional[str] = None
    status: str
    receiver: Optional[str] = None
    groupLabels: Dict[str, str] = {}
    commonLabels: Dict[str, str] = {}
    commonAnnotations: Dict[str, str] = {}
    externalURL: Optional[str] = None
    alerts: List[AlertmanagerWebhookAlert] = []


class DiscordAlertRequest(BaseModel):
    service: str
    title: str
    details: str
    severity: Literal["error", "warning", "info"] = "error"
    bot_name: Optional[str] = None
    avatar_url: Optional[str] = None
    user_id: Optional[str] = None
    channel_id: Optional[str] = None
    destination_channel: Optional[str] = None
    cooldown_seconds: Optional[int] = None
    node: Optional[str] = None
    triage: Optional[bool] = None


class NotificationRequest(BaseModel):
    channel: Literal["email", "discord"]
    # Email fields
    recipient: Optional[str] = None
    sender: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    # Discord alert fields
    service: Optional[str] = None
    title: Optional[str] = None
    details: Optional[str] = None
    severity: Optional[Literal["error", "warning", "info"]] = "error"
    bot_name: Optional[str] = None
    avatar_url: Optional[str] = None
    user_id: Optional[str] = None
    channel_id: Optional[str] = None
    destination_channel: Optional[str] = None
    cooldown_seconds: Optional[int] = None
    node: Optional[str] = None
    triage: Optional[bool] = None


class InboundEmailAttachmentInput(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field(..., min_length=1, max_length=255)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    content_base64: str = Field(..., min_length=1)

    @field_validator("content_base64")
    @classmethod
    def validate_base64_payload(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid base64 attachment payload") from exc
        return cleaned


class InboundEmailWebhookPayload(BaseModel):
    message_id: Optional[str] = Field(default=None, max_length=512)
    from_address: str = Field(..., min_length=1, max_length=512)
    to_address: str = Field(..., min_length=1, max_length=512)
    subject: str = ""
    received_at: Optional[datetime] = None
    text_body: Optional[str] = None
    html_body: Optional[str] = None
    attachments: List[InboundEmailAttachmentInput] = Field(default_factory=list)


class InboundEmailAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content_base64: str
    created_at: datetime


class InboundEmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: str
    from_address: str
    to_address: str
    subject: str
    received_at: datetime
    raw_excerpt: Optional[str] = None
    attachments_meta: List[Dict[str, Any]] = Field(default_factory=list)
    jev_verdict: Optional[Dict[str, Any]] = None
    route_target: str
    dispatch_status: str
    consumer_ref: Optional[str] = None
    error_detail: Optional[str] = None
    retry_count: int
    created_at: datetime
    updated_at: datetime


class InboundEmailDetailResponse(InboundEmailRead):
    text_body: Optional[str] = None
    html_excerpt: Optional[str] = None
    attachments: List[InboundEmailAttachmentRead] = Field(default_factory=list)


class InboundEmailIngestResponse(BaseModel):
    status: str
    duplicate: bool = False
    email_id: int
    message_id: str
    route_target: str
    dispatch_status: str
    consumer_ref: Optional[str] = None
    error_detail: Optional[str] = None
    jev_verdict: Optional[EmailRouteVerdict] = None


class InboundEmailListResponse(BaseModel):
    items: List[InboundEmailRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class InboundEmailStatsResponse(BaseModel):
    total: int
    by_status: Dict[str, int]
    by_route_target: Dict[str, int]


class RetryEmailRequest(BaseModel):
    reclassify: bool = False


class RerouteEmailRequest(BaseModel):
    route_target: RouteTargetType
