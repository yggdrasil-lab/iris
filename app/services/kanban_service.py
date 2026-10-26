import logging
from typing import Any, Dict, Optional
import httpx
from app.core.config import settings

logger = logging.getLogger("iris.kanban")

USER_AGENT = "Yggdrasil-Iris/1.0 (DiscordBot)"


class KanbanService:
    """Dispatches severe operational incidents to the autonomous Kanban Swarm."""

    def __init__(self):
        self.gateway_url = settings.KANBAN_GATEWAY_URL
        self.enabled = settings.IRIS_KANBAN_AUTO_TRIAGE
        self.min_score = settings.IRIS_KANBAN_MIN_SCORE

    async def maybe_dispatch_alert(
        self,
        service: str,
        title: str,
        details: str,
        severity: str,
        node: Optional[str] = None,
        urgency_score: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Evaluates whether an alert qualifies for autonomous Kanban remediation and dispatches it.

        Triggers when:
        - Auto-triage is enabled
        - Gateway URL is configured
        - Severity is 'error' or 'critical', or urgency_score >= IRIS_KANBAN_MIN_SCORE
        """
        if not self.enabled or not self.gateway_url:
            return None

        # Only trigger autonomous work on severe, actionable alerts
        is_severe = (severity in ("error", "critical")) or (urgency_score >= self.min_score)
        if not is_severe:
            return None

        full_context = (
            f"Automated incident alert from Iris Monitoring Gateway.\n"
            f"Service: {service}\n"
            f"Node: {node or 'yggdrasil'}\n"
            f"Severity: {severity}\n"
            f"Urgency Score: {urgency_score}/10\n\n"
            f"Details:\n{details}"
        )

        task_title = f"Fix {service} Alert: {title}"

        payload = {
            "text": full_context,
            "title": task_title[:60],
            "context": f"Iris alertmanager hook for {service}",
        }

        url = f"{self.gateway_url.rstrip('/')}/triage"

        try:
            headers = {"User-Agent": USER_AGENT}
            async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    task_id = data.get("task_id")
                    if task_id:
                        logger.info(
                            "Dispatched severe alert for %s to Kanban: %s (thread: %s)",
                            service,
                            task_id,
                            data.get("thread_id"),
                        )
                        return data
                    else:
                        logger.info(
                            "Alert for %s evaluated by Kanban Jev gateway as non-actionable (suppressed).",
                            service,
                        )
                        return data
                else:
                    logger.warning("Kanban gateway returned status %d: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Failed to dispatch alert to Kanban gateway (%s): %s", url, e)

        return None
