import logging
from typing import Optional
import boto3
from botocore.exceptions import ClientError
from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
            raise RuntimeError("AWS SES credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) are not configured")
        if self._client is None:
            self._client = boto3.client(
                "ses",
                region_name=settings.AWS_REGION,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            )
        return self._client

    def send_email(
        self,
        recipient: str,
        subject: str,
        body: str,
        sender: Optional[str] = None,
        reply_to: Optional[list[str]] = None,
    ) -> dict:
        sender = sender or settings.EMAIL_SENDER
        if not sender:
            raise ValueError("EMAIL_SENDER is not configured")

        if settings.ENV:
            subject = f"[{settings.ENV}] {subject}"

        charset = "UTF-8"

        send_args = {
            "Destination": {
                "ToAddresses": [recipient],
            },
            "Message": {
                "Body": {
                    "Text": {
                        "Charset": charset,
                        "Data": body,
                    },
                    # Basic HTML support mirroring text for now
                    "Html": {
                        "Charset": charset,
                        "Data": body.replace("\n", "<br>"),
                    },
                },
                "Subject": {
                    "Charset": charset,
                    "Data": subject or "No Subject",
                },
            },
            "Source": sender,
        }

        if reply_to:
            send_args["ReplyToAddresses"] = reply_to

        client = self._get_client()
        try:
            response = client.send_email(**send_args)
        except ClientError as e:
            logger.error(f"Error sending email: {e.response['Error']['Message']}")
            raise e
        else:
            return response
