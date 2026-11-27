import json
import logging
import math
from typing import Any, Dict, Mapping, Optional, Sequence

import httpx

from app.jev.ports import JevConfig, JevRouterUnavailableError

logger = logging.getLogger("iris.jev")

JEV_USER_AGENT = "Yggdrasil-Iris/1.0"


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "1", "yes"):
            return True
        if cleaned in ("false", "0", "no"):
            return False
    return False


def parse_clamped_score(value: Any, default: int = 5) -> int:
    if value is None:
        return default
    try:
        numeric = float(str(value).strip())
        if not math.isfinite(numeric):
            return default
        return max(1, min(10, int(round(numeric))))
    except (TypeError, ValueError):
        return default


def parse_clamped_confidence(value: Any) -> float:
    """Parse a finite numeric confidence value and clamp it to [0.0, 1.0]."""
    if isinstance(value, bool):
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))


def extract_question_confidences(parsed: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """Extract and clamp per-question Jev confidences from _question_confidence."""
    question_conf = parsed.get("_question_confidence")
    if not isinstance(question_conf, Mapping):
        return None
    confidences = {
        key: parse_clamped_confidence(value)
        for key, value in question_conf.items()
        if isinstance(key, str) and value is not None
    }
    return confidences or None


def resolve_calibrated_confidence(
    parsed: Mapping[str, Any],
    *,
    question_keys: Sequence[str] = (),
    context_label: str = "response",
) -> float:
    """Derive calibrated confidence from _question_confidence (when requested) or _confidence."""
    question_conf = parsed.get("_question_confidence")
    if question_keys and isinstance(question_conf, Mapping):
        if all(question_conf.get(key) is not None for key in question_keys):
            return min(parse_clamped_confidence(question_conf[key]) for key in question_keys)

    raw_conf = parsed.get("_confidence")
    if raw_conf is None:
        logger.warning(
            "Muninn jev-decision %s missing _confidence (odin jev-shim include_meta update required); "
            "defaulting confidence to 0.0.",
            context_label,
        )
        return 0.0
    return parse_clamped_confidence(raw_conf)


class MuninnJevAdapter:
    """HTTP adapter calling Muninn's OpenAI-compatible /chat/completions Jev endpoint."""

    def __init__(
        self,
        config: JevConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config
        self._transport = transport

    @property
    def config(self) -> JevConfig:
        return self._config

    def _build_request_payload(
        self,
        state_text: str,
        questions: Mapping[str, Any],
    ) -> Dict[str, Any]:
        schema_json = json.dumps({"questions": dict(questions), "include_meta": True})
        system_prompt = (
            "Evaluate the state against the following Jev System One decision schema:\n"
            f"```json\n{schema_json}\n```"
        )
        return {
            "model": (self._config.muninn_jev_model or "jev-decision").strip(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state_text},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

    def _build_client(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                transport=self._transport,
            )
        return httpx.AsyncClient(timeout=self._config.timeout_seconds)

    async def evaluate_questions(
        self,
        state_text: str,
        questions: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Invoke Muninn jev-decision via /chat/completions and parse JSON verdict object."""
        base = (self._config.muninn_base or "").strip()
        api_key = (self._config.muninn_api_key or "").strip()
        if not base or not api_key:
            raise JevRouterUnavailableError(
                "Muninn Jev router unconfigured: MUNINN_API_BASE and MUNINN_API_KEY are required"
            )

        payload = self._build_request_payload(state_text, questions)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": JEV_USER_AGENT,
        }
        url = f"{base.rstrip('/')}/chat/completions"

        try:
            async with self._build_client() as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise JevRouterUnavailableError(f"Muninn Jev call failed: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Assistant message content is not a string")
            parsed = json.loads(content)
        except Exception as exc:
            raise JevRouterUnavailableError(f"Invalid Muninn Jev JSON response: {exc}") from exc

        if not isinstance(parsed, dict):
            raise JevRouterUnavailableError("Muninn Jev response payload must be a JSON object")
        return parsed
