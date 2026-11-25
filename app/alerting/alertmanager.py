from typing import Literal, Tuple

from app.models.schemas import AlertmanagerWebhookAlert, DiscordAlertRequest


def map_alertmanager_severity(
    alert_status: str,
    raw_severity: str,
    is_overall_resolved: bool,
) -> Literal["error", "warning", "info"]:
    if is_overall_resolved or alert_status.lower() == "resolved":
        return "info"
    normalized = raw_severity.lower()
    if normalized in ("critical", "error", "fatal"):
        return "error"
    if normalized in ("warning", "warn"):
        return "warning"
    return "info"


def build_alertmanager_request(
    alert: AlertmanagerWebhookAlert,
    is_overall_resolved: bool,
) -> Tuple[str, Literal["error", "warning", "info"], DiscordAlertRequest]:
    alertname = alert.labels.get("alertname", "UnknownAlert")
    service = alert.labels.get("job") or alert.labels.get("service") or alertname
    instance = alert.labels.get("instance", "yggdrasil")
    node = alert.labels.get("node") or instance.split(":")[0]
    summary = alert.annotations.get("summary") or alert.annotations.get("description") or alertname
    description = (
        alert.annotations.get("description")
        or alert.annotations.get("summary")
        or f"Alertmanager alert: {alertname}"
    )
    mapped_severity = map_alertmanager_severity(
        alert.status,
        alert.labels.get("severity", "warning"),
        is_overall_resolved,
    )
    alert_req = DiscordAlertRequest(
        service=service,
        title=summary,
        details=description,
        severity=mapped_severity,
        node=node,
        bot_name=f"{service.capitalize()} Monitor",
    )
    return alertname, mapped_severity, alert_req
