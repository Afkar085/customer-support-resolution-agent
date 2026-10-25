"""Small shared helpers: money, hashing, redaction, bounded retry."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.config import get_settings
from app.services.errors import ErrorCode, ToolError

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Money -- integer minor units everywhere, formatted only at the edges.
# --------------------------------------------------------------------------- #
def format_money(amount_minor: int, currency: str = "INR") -> str:
    symbol = {"INR": "₹", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
    return f"{symbol}{amount_minor // 100:,}.{amount_minor % 100:02d}"


def to_minor(amount_major: float | int, currency: str = "INR") -> int:
    """Only used by seed scripts and fixtures; never on a request path."""
    return int(round(float(amount_major) * 100))


# --------------------------------------------------------------------------- #
# Hashing -- a proposal is identified by the exact payload a reviewer saw.
# --------------------------------------------------------------------------- #
def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def idempotency_key(proposal_id: str, payload_hash: str) -> str:
    """Stable across retries and restarts: same proposal, same key.

    Deliberately derived (not random) so a worker that crashes before it can
    persist a key will recompute exactly the same one on restart.
    """
    return f"rf_{proposal_id}_{payload_hash[:16]}"


# --------------------------------------------------------------------------- #
# Redaction -- audit rows and the event timeline must never leak PII.
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")
_SENSITIVE_KEYS = {
    "email",
    "password",
    "token",
    "authorization",
    "api_key",
    "card",
    "card_number",
    "cvv",
    "provider_payment_id",
}


def redact(value: Any, _depth: int = 0) -> Any:
    if _depth > 6:
        return "<truncated>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                out[key] = "<redacted>"
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value][:50]
    if isinstance(value, str):
        text = _EMAIL_RE.sub("<email>", value)
        text = _LONG_DIGITS_RE.sub("<number>", text)
        return text if len(text) <= 2000 else text[:2000] + "..."
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; normalise before comparing."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    aware = as_aware(value)
    return aware.isoformat() if aware else None


def days_between(start: datetime, end: datetime) -> int:
    a, b = as_aware(start), as_aware(end)
    assert a is not None and b is not None
    return (b - a).days


# --------------------------------------------------------------------------- #
# Bounded retry for *read* calls only. Writes that move money never go
# through this -- they go through the refund reconciliation path.
# --------------------------------------------------------------------------- #
def retry_read(
    operation: Callable[[], T],
    *,
    attempts: int | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    settings = get_settings()
    attempts = attempts or settings.max_read_retries
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except ToolError as exc:
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(settings.read_retry_base_delay_seconds * (2 ** (attempt - 1)))
    raise last or ToolError(ErrorCode.INTERNAL_ERROR, "retry_read exhausted with no error")
