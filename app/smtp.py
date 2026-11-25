import asyncio
import logging
from typing import Optional

from aiosmtpd.controller import Controller

from app.container import build_container
from app.mail import OutboundMailer

logger = logging.getLogger(__name__)


class IrisSMTPHandler:
    """SMTP DATA handler delegating MIME parsing and SES relay to OutboundMailer."""

    def __init__(self, mailer: OutboundMailer) -> None:
        self.mailer = mailer

    async def handle_DATA(self, server, session, envelope) -> str:
        try:
            recipients = list(envelope.rcpt_tos)
            logger.info(
                "Relaying SMTP email to %d recipient(s) via OutboundMailer",
                len(recipients),
            )
            await asyncio.to_thread(
                self.mailer.relay_mime_bytes,
                envelope.content,
                mail_from=envelope.mail_from,
                recipients=recipients,
            )
            return "250 Message accepted for delivery"
        except Exception as exc:
            logger.error("Error processing SMTP message: %s", exc, exc_info=True)
            return "500 Internal Server Error"


def create_smtp_controller(
    host: str = "0.0.0.0",
    port: int = 2525,
    mailer: Optional[OutboundMailer] = None,
) -> Controller:
    resolved_mailer = mailer or build_container().mailer
    handler = IrisSMTPHandler(resolved_mailer)
    return Controller(handler, hostname=host, port=port)
