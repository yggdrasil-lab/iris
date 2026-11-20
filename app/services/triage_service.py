from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional
import httpx
from app.core.config import settings
from app.models.schemas import EmailRouteVerdict, RouteTargetType, TriageResult

logger = logging.getLogger("iris.triage")

VALID_ROUTE_TARGETS: set[str] = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_bank_statement",
    "actionable_notice",
    "ignore",
}


class JevRouterUnavailableError(RuntimeError):
    """Raised when both TypeSafe System One and Muninn email routing providers fail."""


class TriageService:
    """TypeSafe Jev decision triage and inbound email routing service.

    Implements System One decision primitives:
    1. Score (1-10): Operational urgency or financial significance scoring
    2. Noul (boolean): Binary classification (noise suppression or actionable amount)
    3. Choice: Destination channel or consumer route target selection
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        muninn_base: Optional[str] = None,
        muninn_api_key: Optional[str] = None,
        score_threshold: Optional[int] = None,
        confidence_threshold: Optional[float] = None,
    ):
        self._overrides: Dict[str, Any] = {}
        if endpoint is not None:
            self._overrides["endpoint"] = endpoint
        if api_key is not None:
            self._overrides["api_key"] = api_key
        if muninn_base is not None:
            self._overrides["muninn_base"] = muninn_base
        if muninn_api_key is not None:
            self._overrides["muninn_api_key"] = muninn_api_key
        if score_threshold is not None:
            self._overrides["score_threshold"] = score_threshold
        if confidence_threshold is not None:
            self._overrides["confidence_threshold"] = confidence_threshold

    @property
    def endpoint(self) -> str:
        return self._overrides.get("endpoint", settings.TYPESAFE_ENDPOINT)

    @endpoint.setter
    def endpoint(self, value: str) -> None:
        self._overrides["endpoint"] = value

    @property
    def api_key(self) -> Optional[str]:
        return self._overrides["api_key"] if "api_key" in self._overrides else settings.TYPESAFE_API_KEY

    @api_key.setter
    def api_key(self, value: Optional[str]) -> None:
        self._overrides["api_key"] = value

    @property
    def muninn_base(self) -> Optional[str]:
        return self._overrides["muninn_base"] if "muninn_base" in self._overrides else settings.MUNINN_API_BASE

    @muninn_base.setter
    def muninn_base(self, value: Optional[str]) -> None:
        self._overrides["muninn_base"] = value

    @property
    def muninn_api_key(self) -> Optional[str]:
        return self._overrides["muninn_api_key"] if "muninn_api_key" in self._overrides else settings.MUNINN_API_KEY

    @muninn_api_key.setter
    def muninn_api_key(self, value: Optional[str]) -> None:
        self._overrides["muninn_api_key"] = value

    @property
    def score_threshold(self) -> int:
        return self._overrides.get("score_threshold", settings.IRIS_ALERT_SCORE_THRESHOLD)

    @score_threshold.setter
    def score_threshold(self, value: int) -> None:
        self._overrides["score_threshold"] = value

    @property
    def confidence_threshold(self) -> float:
        return self._overrides.get("confidence_threshold", settings.IRIS_ALERT_CONFIDENCE_THRESHOLD)

    @confidence_threshold.setter
    def confidence_threshold(self, value: float) -> None:
        self._overrides["confidence_threshold"] = value

    async def evaluate_alert(
        self,
        service: str,
        title: str,
        details: str,
        severity: str = "error",
        node: Optional[str] = None,
    ) -> TriageResult:
        """Evaluates an operational alert against Jev decision primitives.

        Guarantees fail-open semantics: if TypeSafe API or network fails,
        returns an unsuppressed actionable alert to ensure no critical incident is lost.
        """
        state_text = (
            f"Service: {service}\n"
            f"Node: {node or 'yggdrasil-cluster'}\n"
            f"Reported Severity: {severity}\n"
            f"Alert Title: {title}\n"
            f"Details:\n{details}"
        )

        if self.api_key:
            try:
                return await self._call_typesafe_systemone(state_text)
            except Exception as e:
                logger.warning(
                    "TypeSafe Jev evaluation failed (%s). Falling back to Muninn/fail-open policy.",
                    e,
                )

        if self.muninn_base:
            try:
                return await self._call_muninn_shim(state_text, severity)
            except Exception as e:
                logger.warning(
                    "Muninn triage evaluation failed (%s). Falling back to fail-open policy.",
                    e,
                )

        return self._fail_open_fallback(severity, reason="Decision engine unreachable; fail-open default applied")

    async def route_inbound_email(
        self,
        from_address: str,
        to_address: str,
        subject: str,
        received_at: datetime | str,
        raw_excerpt: str,
        attachments_meta: List[Dict[str, Any]],
    ) -> EmailRouteVerdict:
        """Classify and route an inbound email using TypeSafe Jev or Muninn fallback.

        Strictly fail-loud: raises JevRouterUnavailableError if all providers fail
        or are unconfigured. Never fails open for inbound emails.
        """
        received_str = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at)
        if attachments_meta:
            attachment_lines = [
                f"- {att.get('filename', 'unnamed')} ({att.get('content_type', 'application/octet-stream')}, {att.get('size_bytes', 0)} bytes)"
                for att in attachments_meta
            ]
            attachments_summary = "\n".join(attachment_lines)
        else:
            attachments_summary = "None"

        state_text = (
            f"From: {from_address}\n"
            f"To: {to_address}\n"
            f"Received At: {received_str}\n"
            f"Subject: {subject}\n"
            f"Attachments:\n{attachments_summary}\n\n"
            f"Body Excerpt:\n{raw_excerpt}"
        )

        errors: List[str] = []

        if self.api_key:
            try:
                return await self._call_typesafe_email_router(state_text)
            except Exception as exc:
                logger.warning("TypeSafe email routing failed (%s). Trying Muninn fallback.", exc)
                errors.append(f"typesafe: {exc}")
        else:
            errors.append("typesafe: unconfigured")

        if self.muninn_base and self.muninn_base.strip():
            try:
                return await self._call_muninn_email_router(state_text)
            except Exception as exc:
                logger.error("Muninn email routing fallback failed (%s).", exc)
                errors.append(f"muninn: {exc}")
        else:
            errors.append("muninn: unconfigured")

        raise JevRouterUnavailableError(
            f"Inbound email routing unavailable ({'; '.join(errors)})"
        )

    async def _call_typesafe_systemone(self, state_text: str) -> TriageResult:
        questions = {
            "urgency_score": {
                "type": "score",
                "criteria": [
                    "Routine observation, transient deploy or restart blip, scheduled retry, or self-clearing healthcheck hiccup",
                    "Low priority non-actionable warning or benign status notification",
                    "Moderate operational issue requiring standard inspection within normal working hours",
                    "High-severity failure, service outage, data corruption, or critical threshold breach requiring immediate attention",
                ],
            },
            "is_transient_noise": {
                "type": "noul",
                "instructions": (
                    "True if this alert is transient deployment noise, rolling restart blip, "
                    "benign heartbeat jitter, or self-clearing routine event that does not require human intervention; "
                    "False if it is a real operational issue requiring action."
                ),
            },
            "destination_channel": {
                "type": "choice",
                "criteria": {
                    "alerts": "High-urgency technical alerts and system outages",
                    "decisions": "Incidents or changes requiring operator approval or decisions",
                    "metrics": "Benign telemetry or silent background metrics",
                },
            },
        }

        payload = {
            "model": "jev-latest",
            "state": state_text,
            "questions": questions,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Iris-Triage-Service/1.0",
        }

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        answers = data.get("answers", {})

        score_data = answers.get("urgency_score", {})
        raw_score = float(score_data.get("score", 1.5))
        scaled_score = max(1, min(10, int(round(1 + (raw_score / 3.0) * 9))))
        score_conf = float(score_data.get("confidence", 0.8))

        noul_data = answers.get("is_transient_noise", {})
        noul_prob = float(noul_data.get("noul", 0.0))
        is_transient = noul_prob >= 0.5
        noul_conf = float(noul_data.get("confidence") or (abs(noul_prob - 0.5) * 2))

        choice_data = answers.get("destination_channel", {})
        choice = choice_data.get("choice", "alerts")
        if choice not in ("alerts", "decisions", "metrics"):
            choice = "alerts"
        choice_conf = float(choice_data.get("confidence", 0.8))

        raw_conf = data.get("confidence")
        overall_confidence = float(raw_conf) if raw_conf is not None else min(score_conf, noul_conf, choice_conf)

        if overall_confidence >= self.confidence_threshold:
            if scaled_score < self.score_threshold or is_transient:
                suppress = True
                reason = (
                    f"Suppressed by Jev gate: score {scaled_score}/10 < {self.score_threshold} "
                    f"or transient noise detected (confidence: {overall_confidence:.2f})"
                )
            else:
                suppress = False
                reason = (
                    f"Actionable alert passed: score {scaled_score}/10 >= {self.score_threshold} "
                    f"(confidence: {overall_confidence:.2f})"
                )
        else:
            suppress = False
            reason = (
                f"Low confidence ({overall_confidence:.2f} < {self.confidence_threshold}): "
                f"escalating alert fail-open with score {scaled_score}/10"
            )

        logger.info(
            "Jev triage evaluated: score=%d/10, transient=%s, route=%s, conf=%.2f, suppress=%s",
            scaled_score,
            is_transient,
            choice,
            overall_confidence,
            suppress,
        )

        return TriageResult(
            urgency_score=scaled_score,
            confidence=overall_confidence,
            is_transient_noise=is_transient,
            destination_channel=choice,
            suppress=suppress,
            reason=reason,
            provider="typesafe-jev",
        )

    async def _call_typesafe_email_router(self, state_text: str) -> EmailRouteVerdict:
        questions = {
            "route_target": {
                "type": "choice",
                "criteria": {
                    "plutus_dbs_alert": "DBS Bank transaction alert, card charge, PayNow, FAST transfer, or GIRO notification",
                    "plutus_purchase_receipt": "Order confirmation, tax invoice, or purchase receipt containing an itemized or total spend",
                    "plutus_bank_statement": "Bank or credit card statement notification or attached statement PDF/CSV",
                    "actionable_notice": "Important non-bank notice such as rego/WOF reminder, utility bill notice, medical result, travel booking, or renewal requiring action",
                    "ignore": "Shipping tracking update without purchase total, marketing promo, newsletter, OTP login code, or low-value noise",
                },
            },
            "has_actionable_amount": {
                "type": "noul",
                "instructions": (
                    "True if a concrete monetary purchase or financial transaction amount with currency or dollar figure is present; "
                    "False if no concrete monetary transaction amount is present."
                ),
            },
            "significance_score": {
                "type": "score",
                "criteria": [
                    "Marketing promo, newsletter, OTP code, or shipping tracking noise with no financial or action relevance",
                    "Low-priority informational email without urgent financial or operator action",
                    "Valid bank alert, purchase receipt, or financial statement ready for ingestion",
                    "High-priority financial charge, large transfer, overdue bill, or urgent actionable notice",
                ],
            },
        }

        payload = {
            "model": "jev-latest",
            "state": state_text,
            "questions": questions,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Iris-Triage-Service/1.0",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        answers = data.get("answers", {})
        if not isinstance(answers, dict):
            raise ValueError("Missing answers dictionary in TypeSafe response")

        choice_data = answers.get("route_target", {})
        route_target = choice_data.get("choice")
        if route_target not in VALID_ROUTE_TARGETS:
            raise ValueError(f"Invalid route_target from TypeSafe: {route_target!r}")
        choice_conf = float(choice_data.get("confidence", 0.8))

        noul_data = answers.get("has_actionable_amount", {})
        noul_prob = float(noul_data.get("noul", 0.0))
        has_actionable_amount = noul_prob >= 0.5
        noul_conf = float(noul_data.get("confidence") or (abs(noul_prob - 0.5) * 2))

        score_data = answers.get("significance_score", {})
        raw_score = float(score_data.get("score", 1.5))
        scaled_score = max(1, min(10, int(round(1 + (raw_score / 3.0) * 9))))
        score_conf = float(score_data.get("confidence", 0.8))

        raw_conf = data.get("confidence")
        overall_confidence = float(raw_conf) if raw_conf is not None else min(choice_conf, noul_conf, score_conf)
        overall_confidence = max(0.0, min(1.0, overall_confidence))

        reason = (
            f"Jev classified as {route_target} "
            f"(amount={has_actionable_amount}, score={scaled_score}/10, conf={overall_confidence:.2f})"
        )

        return EmailRouteVerdict(
            route_target=route_target,
            has_actionable_amount=has_actionable_amount,
            confidence=overall_confidence,
            urgency_score=scaled_score,
            reason=reason,
            provider="typesafe-jev",
        )

    async def _call_muninn_shim(self, state_text: str, severity: str) -> TriageResult:
        """Secondary triage route through Muninn LiteLLM router."""
        prompt = (
            f"{state_text}\n\n"
            "Rate the operational urgency on a scale of 1-10. Output only the integer score."
        )
        payload = {
            "model": "agent-monitor",
            "messages": [
                {"role": "system", "content": "You are an operational alert triage scorer. Output only an integer 1-10."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 10,
            "temperature": 0.0,
        }

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Iris-Triage-Service/1.0",
        }
        if self.muninn_api_key:
            headers["Authorization"] = f"Bearer {self.muninn_api_key}"

        url = f"{self.muninn_base.rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"].strip()
        try:
            score = max(1, min(10, int("".join(filter(str.isdigit, content)) or "5")))
        except Exception:
            score = 7

        confidence = 0.85
        suppress = (score < self.score_threshold) and (severity != "error")
        reason = f"Muninn triage: score {score}/10 (threshold: {self.score_threshold})"

        return TriageResult(
            urgency_score=score,
            confidence=confidence,
            is_transient_noise=(score < self.score_threshold),
            destination_channel="alerts" if score >= self.score_threshold else "metrics",
            suppress=suppress,
            reason=reason,
            provider="muninn-shim",
        )

    async def _call_muninn_email_router(self, state_text: str) -> EmailRouteVerdict:
        """Fallback inbound email classifier routed via Muninn LiteLLM."""
        system_prompt = (
            "You classify inbound homelab emails into JSON. "
            "Valid route_target values: "
            "plutus_dbs_alert, plutus_purchase_receipt, plutus_bank_statement, actionable_notice, ignore. "
            "Return strict JSON with keys: "
            "route_target (string), has_actionable_amount (boolean), confidence (float 0..1), "
            "urgency_score (int 1..10), reason (short string)."
        )
        payload = {
            "model": "agent-monitor",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state_text},
            ],
            "max_tokens": 250,
            "temperature": 0.0,
        }

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Iris-Triage-Service/1.0",
        }
        if self.muninn_api_key:
            headers["Authorization"] = f"Bearer {self.muninn_api_key}"

        url = f"{self.muninn_base.rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        raw_content = data["choices"][0]["message"]["content"].strip()
        cleaned = raw_content
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        parsed = json.loads(cleaned)
        route_target: RouteTargetType = parsed["route_target"]
        if route_target not in VALID_ROUTE_TARGETS:
            raise ValueError(f"Invalid route_target from Muninn: {route_target!r}")

        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.75))))
        urgency_score = max(1, min(10, int(parsed.get("urgency_score", 5))))
        has_actionable_amount = bool(parsed.get("has_actionable_amount", False))
        reason = str(parsed.get("reason") or f"Muninn classified as {route_target}")

        return EmailRouteVerdict(
            route_target=route_target,
            has_actionable_amount=has_actionable_amount,
            confidence=confidence,
            urgency_score=urgency_score,
            reason=reason,
            provider="muninn-shim",
        )

    def _fail_open_fallback(self, severity: str, reason: str) -> TriageResult:
        """Deterministic fail-open fallback when external decision engines are unreachable."""
        if severity == "error":
            score = 8
            suppress = False
            channel = "alerts"
        elif severity == "warning":
            score = 5
            suppress = False
            channel = "alerts"
        else:
            score = 2
            suppress = False
            channel = "alerts"

        logger.info("Fail-open fallback applied for severity '%s': score=%d, suppress=%s", severity, score, suppress)
        return TriageResult(
            urgency_score=score,
            confidence=1.0,
            is_transient_noise=False,
            destination_channel=channel,
            suppress=suppress,
            reason=reason,
            provider="fail-open-fallback",
        )
