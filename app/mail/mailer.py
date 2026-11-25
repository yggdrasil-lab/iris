from dataclasses import dataclass
import email
from email.header import decode_header
from email.message import Message
from typing import Optional

from app.mail.ses_adapter import (
    MailConfigError,
    MailDeliveryError,
    SesMailPort,
)


@dataclass(frozen=True)
class MailConfig:
    default_sender: Optional[str]
    env: Optional[str]


@dataclass(frozen=True)
class MailSendResult:
    message_id: str
    sender: str
    recipients: tuple[str, ...]


def _decode_mime_header(raw_value: Optional[str]) -> str:
    if not raw_value:
        return "No Subject"
    parts: list[str] = []
    for chunk, charset in decode_header(str(raw_value)):
        if isinstance(chunk, bytes):
            parts.append(_decode_bytes(chunk, charset))
        else:
            parts.append(str(chunk))
    decoded = "".join(parts).strip()
    return decoded or "No Subject"


def _decode_bytes(raw: bytes, charset: Optional[str]) -> str:
    encoding = charset or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    return _decode_bytes(payload, part.get_content_charset())


def _extract_mime_body(message: Message) -> str:
    if not message.is_multipart():
        return _decode_part_payload(message)

    html_fallback = ""
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            decoded = _decode_part_payload(part)
            if decoded:
                return decoded
        elif content_type == "text/html" and not html_fallback:
            html_fallback = _decode_part_payload(part)
    return html_fallback


class OutboundMailer:
    """Deep outbound mail relay handling SES sender verification, MIME parsing, and dispatch."""

    def __init__(self, config: MailConfig, ses: SesMailPort) -> None:
        self._config = config
        self._ses = ses

    def _require_sender(self, sender: Optional[str] = None) -> str:
        resolved = (sender or self._config.default_sender or "").strip()
        if not resolved:
            raise MailConfigError("EMAIL_SENDER is not configured")
        return resolved

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        sender: Optional[str] = None,
        reply_to: Optional[list[str]] = None,
    ) -> MailSendResult:
        resolved_sender = self._require_sender(sender)

        resolved_subject = subject or "No Subject"
        if self._config.env:
            resolved_subject = f"[{self._config.env}] {resolved_subject}"

        resolved_body = body or " "
        html_body = resolved_body.replace("\n", "<br>")
        message_id = self._ses.send_email(
            source=resolved_sender,
            recipients=[recipient],
            subject=resolved_subject,
            text_body=resolved_body,
            html_body=html_body,
            reply_to=reply_to,
        )
        return MailSendResult(
            message_id=message_id,
            sender=resolved_sender,
            recipients=(recipient,),
        )

    def resolve_verified_sender(
        self,
        mail_from: Optional[str],
    ) -> tuple[str, Optional[list[str]]]:
        default_sender = self._require_sender()
        verified_domain = (
            default_sender.split("@")[-1].lower() if "@" in default_sender else ""
        )
        cleaned_from = (mail_from or "").strip()
        if (
            cleaned_from
            and verified_domain
            and cleaned_from.lower().endswith(f"@{verified_domain}")
        ):
            return cleaned_from, None
        return default_sender, [cleaned_from] if cleaned_from else None

    def relay_mime_bytes(
        self,
        raw_bytes: bytes,
        *,
        mail_from: Optional[str],
        recipients: list[str],
    ) -> list[MailSendResult]:
        message = email.message_from_bytes(raw_bytes)
        subject = _decode_mime_header(message.get("subject"))
        body = _extract_mime_body(message)
        sender, reply_to = self.resolve_verified_sender(mail_from)

        results: list[MailSendResult] = []
        for recipient in recipients:
            results.append(
                self.send(
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    sender=sender,
                    reply_to=reply_to,
                )
            )
        return results
