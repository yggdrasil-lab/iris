from pydantic import BaseModel, EmailStr
from typing import Any, Dict, List, Literal, Optional

class TriageResult(BaseModel):
    urgency_score: int
    confidence: float
    is_transient_noise: bool
    destination_channel: Literal["alerts", "decisions", "metrics"]
    suppress: bool
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
    webhook_url: Optional[str] = None
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
    webhook_url: Optional[str] = None
    cooldown_seconds: Optional[int] = None
    node: Optional[str] = None
    triage: Optional[bool] = None
