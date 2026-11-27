from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.db.models import InboundEmail

ConsumerName = Literal["plutus", "chronos", "odysseus"]
ConsumerDispatchStatus = Literal["dispatched", "rejected", "unavailable"]
PlutusDispatchStatus = ConsumerDispatchStatus
InboundDisposition = Literal["dispatched", "duplicate", "ignored", "needs_review", "failed"]


class InboundEmailNotFoundError(LookupError):
    """Raised when an inbound email record does not exist."""


class InvalidRouteTargetError(ValueError):
    """Raised when a route_target is not present in ROUTE_CATALOG."""


@dataclass(frozen=True)
class InboundConfig:
    confidence_threshold: float = 0.80
    plutus_dispatch_enabled: bool = False
    chronos_dispatch_enabled: bool = False
    odysseus_dispatch_enabled: bool = False


@dataclass(frozen=True)
class ConsumerConfig:
    consumer_name: str
    api_url: str
    webhook_secret: Optional[str]
    url_env_name: str = ""
    secret_env_name: str = ""
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class PlutusConfig:
    api_url: str
    webhook_secret: Optional[str]
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class ConsumerAttachmentPayload:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content_base64: str


PlutusAttachmentPayload = ConsumerAttachmentPayload


@dataclass(frozen=True)
class ConsumerDispatchRequest:
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
    attachments: tuple[ConsumerAttachmentPayload, ...]


PlutusDispatchRequest = ConsumerDispatchRequest


@dataclass(frozen=True)
class ConsumerDispatchResult:
    status: ConsumerDispatchStatus
    consumer_ref: Optional[str] = None
    error_detail: Optional[str] = None


PlutusDispatchResult = ConsumerDispatchResult


@dataclass(frozen=True)
class InboundActionResult:
    email: "InboundEmail"
    disposition: InboundDisposition
    duplicate: bool = False
    delivery_unavailable: bool = False


@runtime_checkable
class ConsumerDispatchPort(Protocol):
    async def dispatch_email(self, request: ConsumerDispatchRequest) -> ConsumerDispatchResult:
        """Dispatch a routed inbound email payload to a downstream consumer."""


PlutusDispatchPort = ConsumerDispatchPort
TravelDispatchPort = ConsumerDispatchPort
