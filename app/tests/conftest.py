from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.container import IrisContainer, build_container
from app.core.config import Settings
from app.db.session import dispose_engines, get_session_factory
from app.inbound.ports import ConsumerDispatchPort, ConsumerDispatchResult, PlutusDispatchPort
from app.jev import JevClientPort
from app.main import create_app
from app.tests.fakes import (
    FakeClock,
    InMemoryConsumerDispatcher,
    InMemoryDiscordTransport,
    InMemoryJevClient,
    InMemoryPlutusDispatcher,
    InMemorySesAdapter,
)


@dataclass
class IrisTestHarness:
    app: FastAPI
    client: TestClient
    container: IrisContainer
    settings: Settings
    jev: JevClientPort
    plutus: PlutusDispatchPort
    chronos: ConsumerDispatchPort
    discord: InMemoryDiscordTransport
    ses: InMemorySesAdapter
    clock: FakeClock


def build_test_settings(database_url: str, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "DATABASE_URL": database_url,
        "IRIS_DB_PASSWORD": None,
        "IRIS_WEBHOOK_SECRET": "test-iris-webhook-secret",
        "ENV": "test",
        "MUNINN_API_BASE": "http://muninn.local/v1",
        "MUNINN_API_KEY": "muninn-key",
        "MUNINN_JEV_MODEL": "jev-decision",
        "DISCORD_BOT_TOKEN": None,
        "DISCORD_CHANNEL_ALERT_ID": None,
        "DISCORD_CHANNEL_INFO_ID": None,
        "DISCORD_CHANNEL_HEALTH_ID": None,
        "DISCORD_CHANNEL_WEALTH_ID": None,
        "DISCORD_USER_ID": None,
        "IRIS_PLUTUS_DISPATCH_ENABLED": False,
        "PLUTUS_API_URL": "http://plutus-api:8000",
        "PLUTUS_WEBHOOK_SECRET": "plutus-secret",
        "IRIS_CHRONOS_DISPATCH_ENABLED": False,
        "CHRONOS_API_URL": "http://chronos-api:8000",
        "CHRONOS_WEBHOOK_SECRET": "chronos-secret",
        "IRIS_ALERT_CONFIDENCE_THRESHOLD": 0.80,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def isolated_db_url(tmp_path: Path) -> Iterator[str]:
    db_file = tmp_path / "iris_test.db"
    db_url = f"sqlite:///{db_file}"

    dispose_engines()

    app_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(app_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")

    try:
        yield db_url
    finally:
        dispose_engines()


@pytest.fixture()
def db_session(isolated_db_url: str) -> Iterator[Session]:
    factory = get_session_factory(isolated_db_url)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def make_harness(isolated_db_url: str) -> Iterator[Callable[..., IrisTestHarness]]:
    stack = ExitStack()

    def _factory(
        *,
        settings_overrides: Optional[dict[str, Any]] = None,
        jev_client: Optional[JevClientPort] = None,
        plutus_dispatcher: Optional[PlutusDispatchPort] = None,
        chronos_dispatcher: Optional[ConsumerDispatchPort] = None,
        discord_transport: Optional[InMemoryDiscordTransport] = None,
        ses_adapter: Optional[InMemorySesAdapter] = None,
        clock: Optional[FakeClock] = None,
    ) -> IrisTestHarness:
        cfg = build_test_settings(isolated_db_url, **(settings_overrides or {}))
        jev = jev_client if jev_client is not None else InMemoryJevClient()
        plutus = plutus_dispatcher if plutus_dispatcher is not None else InMemoryPlutusDispatcher()
        chronos = (
            chronos_dispatcher
            if chronos_dispatcher is not None
            else InMemoryConsumerDispatcher(
                default_result=ConsumerDispatchResult(
                    status="dispatched",
                    consumer_ref="chronos-evt-1",
                )
            )
        )
        discord = discord_transport if discord_transport is not None else InMemoryDiscordTransport()
        ses = ses_adapter if ses_adapter is not None else InMemorySesAdapter()
        clk = clock if clock is not None else FakeClock()
        container = build_container(
            cfg,
            ses_adapter=ses,
            jev_client=jev,
            discord_transport=discord,
            clock=clk,
            plutus_dispatcher=plutus,
            chronos_dispatcher=chronos,
        )
        test_app = create_app(container)
        test_client = stack.enter_context(TestClient(test_app))
        return IrisTestHarness(
            app=test_app,
            client=test_client,
            container=container,
            settings=cfg,
            jev=jev,
            plutus=plutus,
            chronos=chronos,
            discord=discord,
            ses=ses,
            clock=clk,
        )

    try:
        yield _factory
    finally:
        stack.close()


@pytest.fixture()
def harness(make_harness: Callable[..., IrisTestHarness]) -> IrisTestHarness:
    return make_harness()


@pytest.fixture()
def client(harness: IrisTestHarness) -> TestClient:
    return harness.client
