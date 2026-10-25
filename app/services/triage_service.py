import logging
from typing import Any, Dict, Optional
import httpx
from app.core.config import settings
from app.models.schemas import TriageResult

logger = logging.getLogger("iris.triage")

class TriageService:
    """TypeSafe Jev decision triage service for operational alerts.

    Implements the three core System One decision primitives:
    1. Score (1-10): Operational urgency scoring
    2. Noul (boolean): Notification suppression gate for transient noise
    3. Choice: Webhook destination channel routing (alerts, decisions, metrics)
    """

    def __init__(self):
        self.endpoint = settings.TYPESAFE_ENDPOINT
        self.api_key = settings.TYPESAFE_API_KEY
        self.muninn_base = settings.MUNINN_API_BASE
        self.score_threshold = settings.IRIS_ALERT_SCORE_THRESHOLD
        self.confidence_threshold = settings.IRIS_ALERT_CONFIDENCE_THRESHOLD

    async def evaluate_alert(
        self,
        service: str,
        title: str,
        details: str,
        severity: str = "error",
        node: Optional[str] = None
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

        # 1. Attempt evaluation via native TypeSafe System One API if key is present
        if self.api_key:
            try:
                return await self._call_typesafe_systemone(state_text, severity)
            except Exception as e:
                logger.warning(
                    "TypeSafe Jev evaluation failed (%s). Falling back to fail-open policy.",
                    e
                )

        # 2. Attempt evaluation via Muninn proxy if available
        if self.muninn_base:
            try:
                return await self._call_muninn_shim(state_text, severity)
            except Exception as e:
                logger.warning(
                    "Muninn triage evaluation failed (%s). Falling back to fail-open policy.",
                    e
                )

        # 3. Fail-open fallback: Never drop alerts when AI layer is unreachable
        return self._fail_open_fallback(severity, reason="Decision engine unreachable; fail-open default applied")

    async def _call_typesafe_systemone(self, state_text: str, severity: str) -> TriageResult:
        questions = {
            "urgency_score": {
                "type": "score",
                "criteria": [
                    "Routine observation, transient deploy or restart blip, scheduled retry, or self-clearing healthcheck hiccup",
                    "Low priority non-actionable warning or benign status notification",
                    "Moderate operational issue requiring standard inspection within normal working hours",
                    "High-severity failure, service outage, data corruption, or critical threshold breach requiring immediate attention"
                ]
            },
            "is_transient_noise": {
                "type": "noul",
                "instructions": (
                    "True if this alert is transient deployment noise, rolling restart blip, "
                    "benign heartbeat jitter, or self-clearing routine event that does not require human intervention; "
                    "False if it is a real operational issue requiring action."
                )
            },
            "destination_channel": {
                "type": "choice",
                "options": ["alerts", "decisions", "metrics"]
            }
        }

        payload = {
            "model": "jev-latest",
            "state": state_text,
            "questions": questions
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Iris-Triage-Service/1.0"
        }

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        answers = data.get("answers", {})

        # 1. Parse Urgency Score (0.0 to 3.0 scaled to 1 to 10)
        score_data = answers.get("urgency_score", {})
        raw_score = float(score_data.get("score", 1.5))
        scaled_score = max(1, min(10, int(round(1 + (raw_score / 3.0) * 9))))
        score_conf = float(score_data.get("confidence", 0.8))

        # 2. Parse Transient Noise (Noul probability)
        noul_data = answers.get("is_transient_noise", {})
        noul_prob = float(noul_data.get("noul", 0.0))
        is_transient = noul_prob >= 0.5
        noul_conf = float(noul_data.get("confidence", 0.8))

        # 3. Parse Channel Routing (Choice)
        choice_data = answers.get("destination_channel", {})
        choice = choice_data.get("choice", "alerts")
        if choice not in ("alerts", "decisions", "metrics"):
            choice = "alerts"
        choice_conf = float(choice_data.get("confidence", 0.8))

        # Overall calibrated confidence
        overall_confidence = float(data.get("confidence", min(score_conf, noul_conf, choice_conf)))

        # Suppression Gate Decision
        # Suppress if confidence >= threshold AND (score < threshold OR is_transient is True)
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
            # Low confidence: Fail-open to avoid missing ambiguous real incidents
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
            suppress
        )

        return TriageResult(
            urgency_score=scaled_score,
            confidence=overall_confidence,
            is_transient_noise=is_transient,
            destination_channel=choice,
            suppress=suppress,
            reason=reason,
            provider="typesafe-jev"
        )

    async def _call_muninn_shim(self, state_text: str, severity: str) -> TriageResult:
        """Secondary triage route through Muninn LiteLLM router."""
        prompt = (
            f"{state_text}\n\n"
            f"Rate the operational urgency on a scale of 1-10. Output only the integer score."
        )
        payload = {
            "model": "agent-monitor",
            "messages": [
                {"role": "system", "content": "You are an operational alert triage scorer. Output only an integer 1-10."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 10,
            "temperature": 0.0
        }

        url = f"{self.muninn_base.rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json=payload)
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
            provider="muninn-shim"
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
            provider="fail-open-fallback"
        )
