from dataclasses import dataclass
from typing import Optional

from app.alerting import (
    AlertingConfig,
    ClockPort,
    DiscordTransportPort,
    HttpDiscordTransportAdapter,
    OutboundAlerter,
    SystemClockAdapter,
)
from app.core.config import Settings, settings
from app.jev import JevClientPort, JevConfig, MuninnJevAdapter
from app.mail import (
    Boto3SesAdapter,
    MailConfig,
    OutboundMailer,
    SesConfig,
    SesMailPort,
)


@dataclass(frozen=True)
class IrisContainer:
    settings: Settings
    mailer: OutboundMailer
    jev: JevClientPort
    alerter: OutboundAlerter


def _build_alerting_config(resolved_settings: Settings) -> AlertingConfig:
    return AlertingConfig(
        bot_token=resolved_settings.DISCORD_BOT_TOKEN,
        alert_channel_id=resolved_settings.DISCORD_CHANNEL_ALERT_ID,
        info_channel_id=resolved_settings.DISCORD_CHANNEL_INFO_ID,
        health_channel_id=resolved_settings.DISCORD_CHANNEL_HEALTH_ID,
        wealth_channel_id=resolved_settings.DISCORD_CHANNEL_WEALTH_ID,
        default_user_id=resolved_settings.DISCORD_USER_ID,
        default_cooldown_seconds=resolved_settings.DISCORD_DEFAULT_COOLDOWN_SECONDS,
        alert_triage_enabled=resolved_settings.IRIS_ALERT_TRIAGE_ENABLED,
        score_threshold=resolved_settings.IRIS_ALERT_SCORE_THRESHOLD,
        confidence_threshold=resolved_settings.IRIS_ALERT_CONFIDENCE_THRESHOLD,
    )


def build_container(
    settings_obj: Optional[Settings] = None,
    *,
    ses_adapter: Optional[SesMailPort] = None,
    jev_client: Optional[JevClientPort] = None,
    discord_transport: Optional[DiscordTransportPort] = None,
    clock: Optional[ClockPort] = None,
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
        config=MailConfig(default_sender=resolved_settings.EMAIL_SENDER, env=resolved_settings.ENV),
        ses=adapter,
    )
    jev = jev_client or MuninnJevAdapter(
        JevConfig(
            muninn_base=resolved_settings.MUNINN_API_BASE,
            muninn_api_key=resolved_settings.MUNINN_API_KEY,
            muninn_jev_model=resolved_settings.MUNINN_JEV_MODEL,
            score_threshold=resolved_settings.IRIS_ALERT_SCORE_THRESHOLD,
            confidence_threshold=resolved_settings.IRIS_ALERT_CONFIDENCE_THRESHOLD,
        )
    )
    alerter = OutboundAlerter(
        config=_build_alerting_config(resolved_settings),
        discord=discord_transport or HttpDiscordTransportAdapter(),
        jev=jev,
        clock=clock or SystemClockAdapter(),
    )
    return IrisContainer(settings=resolved_settings, mailer=mailer, jev=jev, alerter=alerter)
