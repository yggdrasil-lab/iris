from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
import pytest

from app.container import IrisContainer, build_container
from app.core.config import Settings
from app.mail import (
    Boto3SesAdapter,
    MailConfig,
    MailConfigError,
    MailDeliveryError,
    OutboundMailer,
    SesConfig,
    SesMailPort,
)
from app.main import create_app
from app.smtp import IrisSMTPHandler, create_smtp_controller
from app.tests.fakes import InMemorySesAdapter


def _build_test_settings(**overrides) -> Settings:
    defaults = {
        "AWS_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": None,
        "AWS_SECRET_ACCESS_KEY": None,
        "EMAIL_SENDER": "no-reply@example.com",
        "ENV": "test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_in_memory_ses_adapter_satisfies_protocol():
    adapter = InMemorySesAdapter()
    assert isinstance(adapter, SesMailPort)


def test_outbound_mailer_send_tags_env_and_formats_html():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env="production"),
        ses=ses,
    )

    result = mailer.send(
        recipient="recipient@example.com",
        subject="Disk Alert",
        body="Line 1\nLine 2",
    )

    assert result.message_id == "msg-1"
    assert result.sender == "no-reply@example.com"
    assert result.recipients == ("recipient@example.com",)
    assert len(ses.sent) == 1
    recorded = ses.sent[0]
    assert recorded.source == "no-reply@example.com"
    assert recorded.recipients == ["recipient@example.com"]
    assert recorded.subject == "[production] Disk Alert"
    assert recorded.text_body == "Line 1\nLine 2"
    assert recorded.html_body == "Line 1<br>Line 2"


def test_outbound_mailer_send_defaults_and_sender_override():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env=None),
        ses=ses,
    )

    result = mailer.send(
        recipient="recipient@example.com",
        subject="",
        body="",
        sender="alerts@example.com",
    )

    assert result.sender == "alerts@example.com"
    assert ses.sent[0].subject == "No Subject"
    assert ses.sent[0].text_body == " "
    assert ses.sent[0].html_body == " "


def test_outbound_mailer_missing_sender_raises_config_error():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender=None, env="test"),
        ses=ses,
    )

    with pytest.raises(MailConfigError):
        mailer.send(recipient="recipient@example.com", subject="Hi", body="Hello")


def test_resolve_verified_sender_matches_domain_case_insensitively():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env=None),
        ses=ses,
    )

    assert mailer.resolve_verified_sender("Alerts@EXAMPLE.COM") == (
        "Alerts@EXAMPLE.COM",
        None,
    )
    assert mailer.resolve_verified_sender("user@external.example.org") == (
        "no-reply@example.com",
        ["user@external.example.org"],
    )
    assert mailer.resolve_verified_sender(None) == ("no-reply@example.com", None)


def test_resolve_verified_sender_missing_default_raises_config_error():
    mailer = OutboundMailer(
        config=MailConfig(default_sender=None, env=None),
        ses=InMemorySesAdapter(),
    )

    with pytest.raises(MailConfigError):
        mailer.resolve_verified_sender("sender@example.com")


def test_relay_mime_bytes_multi_recipient_and_attachment_exclusion():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env="prod"),
        ses=ses,
    )

    msg = MIMEMultipart()
    msg["Subject"] = "=?utf-8?q?System_=E2=9C=93_OK?="
    msg.attach(MIMEText("Plain text body\nSecond line", "plain", "utf-8"))
    msg.attach(MIMEText("<p>HTML version</p>", "html", "utf-8"))
    attachment = MIMEApplication(b"ignored-binary", Name="report.bin")
    attachment["Content-Disposition"] = 'attachment; filename="report.bin"'
    msg.attach(attachment)

    results = mailer.relay_mime_bytes(
        msg.as_bytes(),
        mail_from="external@other.example.org",
        recipients=["one@example.com", "two@example.com"],
    )

    assert len(results) == 2
    assert len(ses.sent) == 2
    assert [item.recipients[0] for item in ses.sent] == [
        "one@example.com",
        "two@example.com",
    ]
    for item in ses.sent:
        assert item.source == "no-reply@example.com"
        assert item.reply_to == ["external@other.example.org"]
        assert item.subject == "[prod] System \u2713 OK"
        assert item.text_body == "Plain text body\nSecond line"


def test_relay_mime_bytes_html_fallback_non_utf8_and_empty_payload():
    ses = InMemorySesAdapter()
    mailer = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env=None),
        ses=ses,
    )

    html_only = MIMEMultipart()
    html_only["Subject"] = "HTML Fallback"
    html_only.attach(MIMEText("<p>Caf\xe9</p>", "html", "iso-8859-1"))
    mailer.relay_mime_bytes(
        html_only.as_bytes(),
        mail_from="alerts@example.com",
        recipients=["recipient@example.com"],
    )
    assert ses.sent[0].source == "alerts@example.com"
    assert ses.sent[0].reply_to is None
    assert ses.sent[0].text_body == "<p>Caf\u00e9</p>"

    non_utf8 = EmailMessage()
    non_utf8["Subject"] = "Latin1 Raw"
    non_utf8.set_payload("R\xe9sum\xe9".encode("iso-8859-1"))
    non_utf8["Content-Type"] = 'text/plain; charset="iso-8859-1"'
    mailer.relay_mime_bytes(
        non_utf8.as_bytes(),
        mail_from="alerts@example.com",
        recipients=["recipient@example.com"],
    )
    assert ses.sent[1].text_body == "R\u00e9sum\u00e9"

    empty_msg = EmailMessage()
    mailer.relay_mime_bytes(
        empty_msg.as_bytes(),
        mail_from=None,
        recipients=["recipient@example.com"],
    )
    assert ses.sent[2].subject == "No Subject"
    assert ses.sent[2].text_body == " "


def test_boto3_ses_adapter_missing_creds_and_client_error_translation():
    calls = []

    def fail_factory(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("client_factory should not be called without credentials")

    missing_creds = Boto3SesAdapter(
        SesConfig(region="ap-southeast-2", access_key_id=None, secret_access_key=None),
        client_factory=fail_factory,
    )
    with pytest.raises(MailConfigError):
        missing_creds.send_email(
            source="no-reply@example.com",
            recipients=["recipient@example.com"],
            subject="Test",
            text_body="Body",
            html_body="Body",
        )
    assert calls == []

    class FakeClient:
        def __init__(self) -> None:
            self.invocations = 0

        def send_email(self, **kwargs):
            self.invocations += 1
            if self.invocations == 1:
                return {"MessageId": "ses-123"}
            raise ClientError(
                {"Error": {"Code": "MessageRejected", "Message": "Rejected"}},
                "SendEmail",
            )

    fake_client = FakeClient()
    created = []

    def ok_factory(service_name, **kwargs):
        created.append((service_name, kwargs))
        return fake_client

    adapter = Boto3SesAdapter(
        SesConfig(
            region="ap-southeast-2",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret-example",
        ),
        client_factory=ok_factory,
    )

    msg_id = adapter.send_email(
        source="no-reply@example.com",
        recipients=["recipient@example.com"],
        subject="Hello",
        text_body="World",
        html_body="World",
    )
    assert msg_id == "ses-123"
    assert len(created) == 1

    with pytest.raises(MailDeliveryError, match="Rejected"):
        adapter.send_email(
            source="no-reply@example.com",
            recipients=["recipient@example.com"],
            subject="Hello",
            text_body="World",
            html_body="World",
        )
    assert len(created) == 1


@pytest.mark.asyncio
async def test_smtp_handler_relays_and_handles_errors():
    ses_ok = InMemorySesAdapter()
    mailer_ok = OutboundMailer(
        config=MailConfig(default_sender="no-reply@example.com", env="test"),
        ses=ses_ok,
    )
    handler_ok = IrisSMTPHandler(mailer_ok)
    envelope = SimpleNamespace(
        content=b"Subject: Backup Complete\r\n\r\nAll snapshots synced.",
        mail_from="cron@other.example.org",
        rcpt_tos=["recipient@example.com"],
    )

    status_ok = await handler_ok.handle_DATA(None, None, envelope)
    assert status_ok == "250 Message accepted for delivery"
    assert len(ses_ok.sent) == 1
    assert ses_ok.sent[0].subject == "[test] Backup Complete"
    assert ses_ok.sent[0].reply_to == ["cron@other.example.org"]

    ses_fail = InMemorySesAdapter(error=MailDeliveryError("SES outage"))
    handler_fail = IrisSMTPHandler(
        OutboundMailer(
            config=MailConfig(default_sender="no-reply@example.com", env="test"),
            ses=ses_fail,
        )
    )
    status_fail = await handler_fail.handle_DATA(None, None, envelope)
    assert status_fail == "500 Internal Server Error"


def test_create_app_notify_email_routes_and_errors():
    ses = InMemorySesAdapter()
    app_container = build_container(_build_test_settings(), ses_adapter=ses)
    test_app = create_app(app_container)
    assert isinstance(test_app.state.container, IrisContainer)
    assert test_app.state.container is app_container

    smtp_controller = create_smtp_controller(mailer=app_container.mailer)
    assert smtp_controller.handler.mailer is app_container.mailer

    with TestClient(test_app) as test_client:
        res_ok = test_client.post(
            "/notify",
            json={
                "channel": "email",
                "recipient": "recipient@example.com",
                "subject": "Daily Digest",
                "body": "Everything is green",
            },
        )
        assert res_ok.status_code == 200
        assert res_ok.json() == {"status": "success", "message_id": "msg-1"}
        assert ses.sent[0].subject == "[test] Daily Digest"

        for payload, detail in (
            (
                {"channel": "email", "recipient": "r@example.com", "body": "B"},
                "Subject is required for email channel",
            ),
            (
                {"channel": "email", "subject": "S", "body": "B"},
                "Recipient is required for email channel",
            ),
            (
                {"channel": "email", "recipient": "r@example.com", "subject": "S"},
                "Body is required for email channel",
            ),
        ):
            res_bad = test_client.post("/notify", json=payload)
            assert res_bad.status_code == 400
            assert res_bad.json()["detail"] == detail

    failing_ses = InMemorySesAdapter(error=MailDeliveryError("Upstream throttle"))
    failing_app = create_app(build_container(_build_test_settings(), ses_adapter=failing_ses))
    with TestClient(failing_app) as failing_client:
        res_err = failing_client.post(
            "/notify",
            json={
                "channel": "email",
                "recipient": "recipient@example.com",
                "subject": "Alert",
                "body": "Body",
            },
        )
        assert res_err.status_code == 500
        assert "Upstream throttle" in res_err.json()["detail"]

    unconfigured_app = create_app(
        build_container(
            _build_test_settings(EMAIL_SENDER=None),
            ses_adapter=InMemorySesAdapter(),
        )
    )
    with TestClient(unconfigured_app) as unconfigured_client:
        res_cfg = unconfigured_client.post(
            "/notify",
            json={
                "channel": "email",
                "recipient": "recipient@example.com",
                "subject": "Alert",
                "body": "Body",
            },
        )
        assert res_cfg.status_code == 500
        assert "EMAIL_SENDER" in res_cfg.json()["detail"]
