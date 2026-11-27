from typing import Any, Dict, List, Optional

import httpx

from app.inbound.ports import (
    ConsumerConfig,
    ConsumerDispatchRequest,
    ConsumerDispatchResult,
    PlutusConfig,
)

CONSUMER_USER_AGENT = "Yggdrasil-Iris/1.0"
PLUTUS_USER_AGENT = CONSUMER_USER_AGENT


def _serialize_payload(request: ConsumerDispatchRequest) -> Dict[str, Any]:
    attachments_payload: List[Dict[str, Any]] = [
        {
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": att.size_bytes,
            "sha256": att.sha256,
            "content_base64": att.content_base64,
        }
        for att in request.attachments
    ]
    return {
        "email_id": request.email_id,
        "message_id": request.message_id,
        "route_target": request.route_target,
        "from_address": request.from_address,
        "to_address": request.to_address,
        "subject": request.subject,
        "received_at": request.received_at.isoformat(),
        "text_body": request.text_body,
        "html_excerpt": request.html_excerpt,
        "raw_excerpt": request.raw_excerpt,
        "jev_verdict": request.jev_verdict,
        "attachments": attachments_payload,
    }


_CONSUMER_REF_KEYS = (
    "consumer_ref",
    "booking_id",
    "event_id",
    "transaction_id",
    "receipt_id",
    "status",
)


def _extract_consumer_ref(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return "dispatched"
    for key in _CONSUMER_REF_KEYS:
        value = body.get(key)
        if value:
            return str(value)[:256]
    return "dispatched"


class HttpConsumerDispatchAdapter:
    """HTTP adapter dispatching routed emails to a downstream consumer POST /integrations/email."""

    def __init__(
        self,
        config: ConsumerConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._prefix = (config.consumer_name or "consumer").strip().lower()

    def _build_client(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                transport=self._transport,
            )
        return httpx.AsyncClient(timeout=self._config.timeout_seconds)

    def _validate_config(self) -> Optional[str]:
        api_url = (self._config.api_url or "").strip()
        webhook_secret = (self._config.webhook_secret or "").strip()
        url_env = self._config.url_env_name or f"{self._prefix.upper()}_API_URL"
        secret_env = self._config.secret_env_name or f"{self._prefix.upper()}_WEBHOOK_SECRET"
        missing: List[str] = []
        if not api_url:
            missing.append(url_env)
        if not webhook_secret:
            missing.append(secret_env)
        if missing:
            return f"{self._prefix}_dispatch_misconfigured: missing {', '.join(missing)}"
        return None

    async def dispatch_email(self, request: ConsumerDispatchRequest) -> ConsumerDispatchResult:
        """Dispatch email payload to downstream consumer and map HTTP outcome to typed status."""
        config_error = self._validate_config()
        if config_error is not None:
            return ConsumerDispatchResult(status="unavailable", error_detail=config_error)

        api_url = self._config.api_url.strip()
        webhook_secret = (self._config.webhook_secret or "").strip()
        headers = {
            "Authorization": f"Bearer {webhook_secret}",
            "Content-Type": "application/json",
            "User-Agent": CONSUMER_USER_AGENT,
        }
        target_url = f"{api_url.rstrip('/')}/integrations/email"
        payload = _serialize_payload(request)

        try:
            async with self._build_client() as client:
                resp = await client.post(target_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return ConsumerDispatchResult(
                status="unavailable",
                error_detail=f"{self._prefix}_network_error: {exc}",
            )

        if resp.status_code in (200, 201):
            return ConsumerDispatchResult(
                status="dispatched",
                consumer_ref=_extract_consumer_ref(resp),
            )
        if resp.status_code == 422:
            return ConsumerDispatchResult(
                status="rejected",
                error_detail=f"{self._prefix}_validation_422: {resp.text[:500]}",
            )
        return ConsumerDispatchResult(
            status="unavailable",
            error_detail=f"{self._prefix}_upstream_{resp.status_code}: {resp.text[:500]}",
        )


class HttpPlutusDispatchAdapter(HttpConsumerDispatchAdapter):
    """HTTP adapter dispatching routed financial emails to Plutus POST /integrations/email."""

    def __init__(
        self,
        config: PlutusConfig | ConsumerConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        resolved = (
            config
            if isinstance(config, ConsumerConfig)
            else ConsumerConfig(
                consumer_name="plutus",
                api_url=config.api_url,
                webhook_secret=config.webhook_secret,
                timeout_seconds=config.timeout_seconds,
            )
        )
        super().__init__(resolved, transport=transport)
