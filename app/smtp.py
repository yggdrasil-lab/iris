import asyncio
import email
import logging
from aiosmtpd.controller import Controller
from app.services.email_service import EmailService
from app.core.config import settings

logger = logging.getLogger(__name__)

class IrisSMTPHandler:
    def __init__(self):
        self.email_service = EmailService()

    async def handle_DATA(self, server, session, envelope):
        try:
            # Parse the email content
            message = email.message_from_bytes(envelope.content)
            
            subject = message.get("subject", "No Subject")
            
            # Extract body
            body = ""
            if message.is_multipart():
                for part in message.walk():
                    content_type = part.get_content_type()
                    content_disposition = part.get("Content-Disposition")
                    if content_type == "text/plain" and "attachment" not in str(content_disposition):
                        body = part.get_payload(decode=True).decode()
                        break
            else:
                body = message.get_payload(decode=True).decode()

            # Determine safe SES sender
            # If incoming mail_from matches verified domain, use it; otherwise fallback to default EMAIL_SENDER
            verified_domain = settings.EMAIL_SENDER.split("@")[-1].lower() if "@" in settings.EMAIL_SENDER else ""
            mail_from = (envelope.mail_from or "").strip()
            
            if mail_from and verified_domain and mail_from.lower().endswith(f"@{verified_domain}"):
                sender = mail_from
                reply_to = None
            else:
                sender = settings.EMAIL_SENDER
                reply_to = [mail_from] if mail_from else None

            # Iterate over recipients (SMTP allows multiple RCPT TO)
            for recipient in envelope.rcpt_tos:
                logger.info(f"Relaying SMTP email to {recipient} via SES (sender: {sender}, reply_to: {reply_to})...")
                await asyncio.to_thread(
                    self.email_service.send_email,
                    recipient=recipient,
                    subject=subject,
                    body=body or " ", # SES requires non-empty body
                    sender=sender,
                    reply_to=reply_to,
                )
            
            return '250 Message accepted for delivery'
        except Exception as e:
            logger.error(f"Error processing SMTP message: {e}", exc_info=True)
            return '500 Internal Server Error'

def create_smtp_controller(host="0.0.0.0", port=2525):
    handler = IrisSMTPHandler()
    controller = Controller(handler, hostname=host, port=port)
    return controller
