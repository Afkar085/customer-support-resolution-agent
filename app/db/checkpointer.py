"""LangGraph checkpointer.

Durable by default and kept logically separate from the application tables:
on SQLite it is its own file, on Postgres the saver creates its own tables.
A checkpoint is how a paused workflow resumes -- it is never the refund ledger.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, get_settings

_LOCK = threading.Lock()
_SAVER: Any | None = None


def _sqlite_path(url: str) -> Path:
    raw = url.split("///", 1)[1] if "///" in url else "checkpoints.db"
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def build_checkpointer() -> Any:
    settings = get_settings()
    url = settings.checkpoint_database_url or (
        f"sqlite:///{(REPO_ROOT / 'checkpoints.db').as_posix()}"
        if settings.database_url.startswith("sqlite")
        else settings.database_url
    )

    if url.startswith("sqlite"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = _sqlite_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(connection)
        saver.setup()
        return saver

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    conninfo = url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    pool = ConnectionPool(conninfo=conninfo, max_size=10, kwargs={"autocommit": True})
    saver = PostgresSaver(pool)
    saver.setup()
    return saver


def get_checkpointer() -> Any:
    """Process-wide singleton; the graph is rebuilt per request, the saver is not."""
    global _SAVER
    with _LOCK:
        if _SAVER is None:
            _SAVER = build_checkpointer()
        return _SAVER


def reset_checkpointer() -> None:
    """Used by tests that need a clean checkpoint store."""
    global _SAVER
    with _LOCK:
        _SAVER = None
