from pydantic import BaseModel, EmailStr
from typing import Literal, Optional

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
