import logging
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("iris.alerting.discord")

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "Yggdrasil-Iris/1.0 (DiscordBot)"


class HttpDiscordTransportAdapter:
    """Discord REST API v10 adapter supporting Forum threads, replies, and text channels."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _build_client(self, headers: Dict[str, str]) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(
                timeout=self._timeout_seconds,
                headers=headers,
                transport=self._transport,
            )
        return httpx.AsyncClient(timeout=self._timeout_seconds, headers=headers)

    async def _try_thread_reply(
        self,
        client: httpx.AsyncClient,
        *,
        thread_id: str,
        message_payload: Dict[str, Any],
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        reply_url = f"{DISCORD_API_BASE}/channels/{thread_id}/messages"
        resp = await client.post(reply_url, json=message_payload)
        if resp.status_code in (200, 201):
            return resp.json(), thread_id
        if resp.status_code in (400, 403, 404):
            logger.warning(
                "Posting to thread_id %s returned status %d. Creating a new thread.",
                thread_id,
                resp.status_code,
            )
            return None
        resp.raise_for_status()
        return None

    async def _post_to_channel(
        self,
        client: httpx.AsyncClient,
        *,
        channel_id: str,
        thread_name: str,
        message_payload: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        threads_url = f"{DISCORD_API_BASE}/channels/{channel_id}/threads"
        resp = await client.post(
            threads_url,
            json={"name": thread_name[:100], "message": message_payload},
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            created_thread_id = str(data["id"]) if data.get("id") is not None else None
            return data, created_thread_id

        if resp.status_code == 400:
            logger.info(
                "Channel %s rejected Forum thread payload (likely standard text channel). Retrying /messages.",
                channel_id,
            )
            msg_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
            resp = await client.post(msg_url, json=message_payload)

        resp.raise_for_status()
        return resp.json(), None

    async def post_message(
        self,
        *,
        bot_token: str,
        channel_id: str,
        thread_name: str,
        message_payload: Dict[str, Any],
        thread_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with self._build_client(headers) as client:
            if thread_id:
                replied = await self._try_thread_reply(
                    client, thread_id=thread_id, message_payload=message_payload
                )
                if replied is not None:
                    return replied
            return await self._post_to_channel(
                client,
                channel_id=channel_id,
                thread_name=thread_name,
                message_payload=message_payload,
            )
