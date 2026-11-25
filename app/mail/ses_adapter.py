from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional, Protocol, runtime_checkable

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class MailConfigError(RuntimeError):
    """Raised when required outbound mail configuration or credentials are missing."""


class MailDeliveryError(RuntimeError):
    """Raised when an outbound mail provider fails to deliver a message."""


@runtime_checkable
class SesMailPort(Protocol):
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
        """Send an outbound email and return the provider MessageId."""


@dataclass(frozen=True)
class SesConfig:
    region: str
    access_key_id: Optional[str]
    secret_access_key: Optional[str]


class Boto3SesAdapter:
    """Lazy boto3 SES client adapter translating SDK errors into MailDeliveryError."""

    def __init__(
        self,
        config: SesConfig,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or boto3.client
        self._client: Any = None

    def _get_client(self) -> Any:
        if not self._config.access_key_id or not self._config.secret_access_key:
            raise MailConfigError(
                "AWS SES credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) are not configured"
            )
        if self._client is None:
            self._client = self._client_factory(
                "ses",
                region_name=self._config.region,
                aws_access_key_id=self._config.access_key_id,
                aws_secret_access_key=self._config.secret_access_key,
            )
        return self._client

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
        charset = "UTF-8"
        send_args: dict[str, Any] = {
            "Destination": {"ToAddresses": list(recipients)},
            "Message": {
                "Body": {
                    "Text": {"Charset": charset, "Data": text_body},
                    "Html": {"Charset": charset, "Data": html_body},
                },
                "Subject": {"Charset": charset, "Data": subject},
            },
            "Source": source,
        }
        if reply_to:
            send_args["ReplyToAddresses"] = list(reply_to)

        client = self._get_client()
        try:
            response = client.send_email(**send_args)
        except ClientError as exc:
            error_msg = exc.response.get("Error", {}).get("Message") or str(exc)
            logger.error("Error sending email via SES: %s", error_msg)
            raise MailDeliveryError(f"SES delivery failed: {error_msg}") from exc

        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not message_id:
            raise MailDeliveryError("SES response did not include MessageId")
        return str(message_id)
