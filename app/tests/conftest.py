from pathlib import Path
from typing import Iterator

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
import pytest

from app.core.config import settings
from app.db.session import dispose_engines, get_session_factory
from app.main import app


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    db_file = tmp_path / "iris_test.db"
    db_url = f"sqlite:///{db_file}"

    monkeypatch.setattr(settings, "DATABASE_URL", db_url)
    monkeypatch.setattr(settings, "IRIS_DB_PASSWORD", None)
    monkeypatch.setattr(settings, "IRIS_WEBHOOK_SECRET", "test-iris-webhook-secret")
    monkeypatch.setattr(settings, "ENV", "test")
    monkeypatch.setattr(settings, "MUNINN_API_BASE", "http://muninn:4000/v1")
    monkeypatch.setattr(settings, "MUNINN_API_KEY", None)
    monkeypatch.setattr(settings, "MUNINN_JEV_MODEL", "jev-decision")
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_ALERT_ID", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_INFO_ID", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_HEALTH_ID", None)
    monkeypatch.setattr(settings, "DISCORD_CHANNEL_WEALTH_ID", None)
    monkeypatch.setattr(settings, "DISCORD_USER_ID", None)
    monkeypatch.setattr(settings, "IRIS_PLUTUS_DISPATCH_ENABLED", False)
    monkeypatch.setattr(settings, "PLUTUS_API_URL", "http://plutus-api:8000")
    monkeypatch.setattr(settings, "PLUTUS_WEBHOOK_SECRET", None)
    monkeypatch.setattr(settings, "IRIS_ALERT_CONFIDENCE_THRESHOLD", 0.80)

    dispose_engines()

    app_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(app_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")

    try:
        yield db_file
    finally:
        dispose_engines()


@pytest.fixture()
def db_session(isolated_db: Path):
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(isolated_db: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
