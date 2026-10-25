"""Engine, session and dialect-aware locking helpers."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine():
    settings = get_settings()
    kwargs: dict = {"echo": settings.sql_echo, "future": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short transaction. Never hold one of these open across a network call."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


# SQLite has no SELECT ... FOR UPDATE. The durable guarantee against duplicate
# refunds is the unique constraint on refund_operations.idempotency_key; the
# lock below only narrows the race window so the balance check is serialised.
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _process_lock(key: str) -> threading.Lock:
    with _LOCK_REGISTRY_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def lock_order(session: Session, order_id: str) -> Iterator[None]:
    """Serialise refundable-balance checks for one order.

    Postgres: row lock inside the caller's transaction.
    SQLite: in-process mutex (single-process demo) + unique constraints.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT id FROM orders WHERE id = :oid FOR UPDATE"), {"oid": order_id}
        )
        yield
        return
    lock = _process_lock(f"order:{order_id}")
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
