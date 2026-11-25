from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RecordedEmail:
    source: str
    recipients: list[str]
    subject: str
    text_body: str
    html_body: str
    reply_to: Optional[list[str]] = None


class InMemorySesAdapter:
    """In-memory SesMailPort fake recording sent emails and optional injected failures."""

    def __init__(self, *, error: Optional[Exception] = None) -> None:
        self.sent: list[RecordedEmail] = []
        self.error = error

    def send_email(
        self,
        *,
        source: str,
        recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str,
        reply_to: Optional[list[str]] = None,
    ) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(
            RecordedEmail(
                source=source,
                recipients=list(recipients),
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                reply_to=list(reply_to) if reply_to is not None else None,
            )
        )
        return f"msg-{len(self.sent)}"
