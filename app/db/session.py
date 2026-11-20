from collections.abc import Generator
from typing import Any, Optional
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from app.core.config import settings

_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}


def get_engine(database_url: Optional[str] = None) -> Engine:
    """Return a cached SQLAlchemy engine for the resolved database URL."""
    url = database_url or settings.resolved_database_url()
    engine = _engines.get(url)
    if engine is None:
        connect_kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            connect_kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **connect_kwargs)
        _engines[url] = engine
    return engine


def get_session_factory(database_url: Optional[str] = None) -> sessionmaker[Session]:
    """Return a cached SQLAlchemy sessionmaker bound to the resolved engine."""
    url = database_url or settings.resolved_database_url()
    factory = _session_factories.get(url)
    if factory is None:
        factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(url),
        )
        _session_factories[url] = factory
    return factory


def dispose_engines() -> None:
    """Dispose and clear all cached engines and session factories (primarily for tests)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _session_factories.clear()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding an active SQLAlchemy session."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
