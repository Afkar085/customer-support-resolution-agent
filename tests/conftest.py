"""Test harness.

Every test runs against its own SQLite file with a freshly seeded fixture set
and the deterministic reasoner, so assertions are about system behaviour
rather than model sampling.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Environment must be set before any app module reads settings.
_TMP = Path(tempfile.mkdtemp(prefix="support_agent_tests_"))
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["CHECKPOINT_DATABASE_URL"] = f"sqlite:///{(_TMP / 'checkpoints.db').as_posix()}"
os.environ["ENVIRONMENT"] = "test"
os.environ["FORCE_OFFLINE_REASONER"] = "true"
os.environ["ANTHROPIC_API_KEY"] = ""

from app.auth.principal import Principal, principal_from_token  # noqa: E402
from app.db.base import Base, engine, session_scope  # noqa: E402
from app.services import policy_store  # noqa: E402
from scripts import seed_data  # noqa: E402

CUSTOMER_TOKEN = "tok_cust_priya"
OTHER_CUSTOMER_TOKEN = "tok_cust_mallory"
REVIEWER_TOKEN = "tok_rev_kiran"
READONLY_REVIEWER_TOKEN = "tok_rev_readonly"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    Base.metadata.create_all(engine)
    with session_scope() as db:
        policy_store.ingest_all(db)


@pytest.fixture(autouse=True)
def fresh_data(_schema) -> None:
    seed_data.wipe()
    seed_data.seed()
    yield


@pytest.fixture
def session():
    with session_scope() as db:
        yield db


def principal(token: str) -> Principal:
    with session_scope() as db:
        return principal_from_token(db, token)


@pytest.fixture
def customer() -> Principal:
    return principal(CUSTOMER_TOKEN)


@pytest.fixture
def other_customer() -> Principal:
    return principal(OTHER_CUSTOMER_TOKEN)


@pytest.fixture
def reviewer() -> Principal:
    return principal(REVIEWER_TOKEN)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth():
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _headers
