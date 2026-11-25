from typing import Any, Dict, List, Optional

import httpx

from app.inbound.ports import (
    PlutusConfig,
    PlutusDispatchRequest,
    PlutusDispatchResult,
)

PLUTUS_USER_AGENT = "Yggdrasil-Iris/1.0"


def _serialize_payload(request: PlutusDispatchRequest) -> Dict[str, Any]:
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


def _extract_consumer_ref(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return "dispatched"
    consumer_ref = (
        body.get("consumer_ref")
        or body.get("transaction_id")
        or body.get("receipt_id")
        or body.get("status")
        or "dispatched"
    )
    return str(consumer_ref)[:256]


class HttpPlutusDispatchAdapter:
    """HTTP adapter dispatching routed financial emails to Plutus POST /integrations/email."""

    def __init__(
        self,
        config: PlutusConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config
        self._transport = transport

    def _build_client(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                transport=self._transport,
            )
        return httpx.AsyncClient(timeout=self._config.timeout_seconds)

    def _validate_config(self) -> Optional[str]:
        plutus_url = (self._config.api_url or "").strip()
        plutus_secret = (self._config.webhook_secret or "").strip()
        missing: List[str] = []
        if not plutus_url:
            missing.append("PLUTUS_API_URL")
        if not plutus_secret:
            missing.append("PLUTUS_WEBHOOK_SECRET")
        if missing:
            return f"plutus_dispatch_misconfigured: missing {', '.join(missing)}"
        return None

    async def dispatch_email(self, request: PlutusDispatchRequest) -> PlutusDispatchResult:
        """Dispatch email payload to Plutus and map HTTP outcome into typed domain status."""
        config_error = self._validate_config()
        if config_error is not None:
            return PlutusDispatchResult(status="unavailable", error_detail=config_error)

        plutus_url = self._config.api_url.strip()
        plutus_secret = (self._config.webhook_secret or "").strip()
        headers = {
            "Authorization": f"Bearer {plutus_secret}",
            "Content-Type": "application/json",
            "User-Agent": PLUTUS_USER_AGENT,
        }
        target_url = f"{plutus_url.rstrip('/')}/integrations/email"
        payload = _serialize_payload(request)

        try:
            async with self._build_client() as client:
                resp = await client.post(target_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return PlutusDispatchResult(
                status="unavailable",
                error_detail=f"plutus_network_error: {exc}",
            )

        if resp.status_code in (200, 201):
            return PlutusDispatchResult(
                status="dispatched",
                consumer_ref=_extract_consumer_ref(resp),
            )
        if resp.status_code == 422:
            return PlutusDispatchResult(
                status="rejected",
                error_detail=f"plutus_validation_422: {resp.text[:500]}",
            )
        return PlutusDispatchResult(
            status="unavailable",
            error_detail=f"plutus_upstream_{resp.status_code}: {resp.text[:500]}",
        )
