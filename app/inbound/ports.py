from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.db.models import InboundEmail

PlutusDispatchStatus = Literal["dispatched", "rejected", "unavailable"]
InboundDisposition = Literal["dispatched", "duplicate", "ignored", "needs_review", "failed"]


class InboundEmailNotFoundError(LookupError):
    """Raised when an inbound email record does not exist."""


class InvalidRouteTargetError(ValueError):
    """Raised when a route_target is not present in ROUTE_CATALOG."""


@dataclass(frozen=True)
class InboundConfig:
    confidence_threshold: float = 0.80
    plutus_dispatch_enabled: bool = False


@dataclass(frozen=True)
class PlutusConfig:
    api_url: str
    webhook_secret: Optional[str]
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class PlutusAttachmentPayload:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content_base64: str


@dataclass(frozen=True)
class PlutusDispatchRequest:
    email_id: int
    message_id: str
    route_target: str
    from_address: str
    to_address: str
    subject: str
    received_at: datetime
    text_body: Optional[str]
    html_excerpt: Optional[str]
    raw_excerpt: Optional[str]
    jev_verdict: Optional[Dict[str, Any]]
    attachments: tuple[PlutusAttachmentPayload, ...]


@dataclass(frozen=True)
class PlutusDispatchResult:
    status: PlutusDispatchStatus
    consumer_ref: Optional[str] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class InboundActionResult:
    email: "InboundEmail"
    disposition: InboundDisposition
    duplicate: bool = False
    delivery_unavailable: bool = False


@runtime_checkable
class PlutusDispatchPort(Protocol):
    async def dispatch_email(self, request: PlutusDispatchRequest) -> PlutusDispatchResult:
        """Dispatch a routed financial email payload to the Plutus consumer."""
