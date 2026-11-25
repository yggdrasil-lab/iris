from dataclasses import dataclass
from typing import Optional

from app.core.config import Settings, settings
from app.mail import (
    Boto3SesAdapter,
    MailConfig,
    OutboundMailer,
    SesConfig,
    SesMailPort,
)


@dataclass
class IrisContainer:
    settings: Settings
    mailer: OutboundMailer


def build_container(
    settings_obj: Optional[Settings] = None,
    *,
    ses_adapter: Optional[SesMailPort] = None,
) -> IrisContainer:
    resolved_settings = settings_obj or settings
    adapter = ses_adapter or Boto3SesAdapter(
        SesConfig(
            region=resolved_settings.AWS_REGION,
            access_key_id=resolved_settings.AWS_ACCESS_KEY_ID,
            secret_access_key=resolved_settings.AWS_SECRET_ACCESS_KEY,
        )
    )
    mailer = OutboundMailer(
        config=MailConfig(
            default_sender=resolved_settings.EMAIL_SENDER,
            env=resolved_settings.ENV,
        ),
        ses=adapter,
    )
    return IrisContainer(settings=resolved_settings, mailer=mailer)
