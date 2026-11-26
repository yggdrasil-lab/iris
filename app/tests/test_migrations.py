"""Regression tests for Iris Alembic migrations and database config resolution."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import InboundEmail, InboundEmailAttachment, InboundEmailDelivery


def test_alembic_upgrade_downgrade_roundtrip(tmp_path: Path, monkeypatch) -> None:
    db_file = tmp_path / "migrations_test.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    app_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(app_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    alembic_command.upgrade(cfg, "head")

    with sqlite3.connect(db_file) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        email_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(inbound_emails)").fetchall()
        }
        att_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(inbound_email_attachments)").fetchall()
        }
        delivery_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(inbound_email_deliveries)").fetchall()
        }
        delivery_indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(inbound_email_deliveries)").fetchall()
        }

    assert "inbound_emails" in tables
    assert "inbound_email_attachments" in tables
    assert "inbound_email_deliveries" in tables
    assert version is not None
    assert version[0] == "0002_multi_target_deliveries"
    assert {
        "id",
        "message_id",
        "from_address",
        "to_address",
        "subject",
        "received_at",
        "text_body",
        "html_excerpt",
        "raw_excerpt",
        "attachments_meta",
        "jev_verdict",
        "route_target",
        "dispatch_status",
        "consumer_ref",
        "error_detail",
        "retry_count",
        "created_at",
        "updated_at",
    }.issubset(email_cols)
    assert {
        "id",
        "email_id",
        "filename",
        "content_type",
        "size_bytes",
        "sha256",
        "content_base64",
        "created_at",
    }.issubset(att_cols)
    assert {
        "id",
        "email_id",
        "consumer",
        "dispatch_status",
        "consumer_ref",
        "error_detail",
        "retry_count",
        "dispatched_at",
        "created_at",
        "updated_at",
    }.issubset(delivery_cols)
    assert "uq_inbound_email_deliveries_email_consumer" in delivery_indexes

    # Verify downgrade -> upgrade roundtrip
    alembic_command.downgrade(cfg, "base")
    with sqlite3.connect(db_file) as conn:
        down_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "inbound_emails" not in down_tables
    assert "inbound_email_attachments" not in down_tables
    assert "inbound_email_deliveries" not in down_tables

    alembic_command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        with Session(engine) as session:
            email_row = InboundEmail(
                message_id="<mig-001@example.com>",
                from_address="sender@example.com",
                to_address="iris@example.com",
                subject="Migration Roundtrip",
                received_at=datetime.now(timezone.utc),
                attachments_meta=[],
                attachments=[
                    InboundEmailAttachment(
                        filename="invoice.pdf",
                        content_type="application/pdf",
                        size_bytes=4,
                        sha256="0" * 64,
                        content_base64="dGVzdA==",
                    )
                ],
                deliveries=[
                    InboundEmailDelivery(
                        consumer="chronos",
                        dispatch_status="dispatched",
                        consumer_ref="chronos-evt-1",
                    )
                ],
            )
            session.add(email_row)
            session.commit()
            loaded = session.scalar(
                select(InboundEmail).where(InboundEmail.message_id == "<mig-001@example.com>")
            )
            assert loaded is not None
            assert len(loaded.attachments) == 1
            assert loaded.attachments[0].filename == "invoice.pdf"
            assert len(loaded.deliveries) == 1
            assert loaded.deliveries[0].consumer == "chronos"
            assert loaded.deliveries[0].consumer_ref == "chronos-evt-1"
    finally:
        engine.dispose()


def test_resolved_database_url_requires_password_and_encodes_special_chars() -> None:
    empty_settings = Settings(_env_file=None, DATABASE_URL=None, IRIS_DB_PASSWORD=None)
    with pytest.raises(ValueError, match="IRIS_DB_PASSWORD"):
        empty_settings.resolved_database_url()

    pg_settings = Settings(
        _env_file=None,
        DATABASE_URL=None,
        IRIS_DB_USER="iris",
        IRIS_DB_PASSWORD="p@ss/w:d+1",
        IRIS_DB_HOST="iris-db",
        IRIS_DB_PORT=5432,
        IRIS_DB_NAME="iris",
    )
    assert (
        pg_settings.resolved_database_url()
        == "postgresql+psycopg://iris:p%40ss%2Fw%3Ad%2B1@iris-db:5432/iris"
    )
