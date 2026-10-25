"""Mock payment provider.

It implements the contract a real PSP gives you, including the parts that make
refunds hard:

* ``idempotency_key`` is authoritative. The same key with the same payload
  returns the *same* refund. The same key with a different payload is rejected
  with ``IDEMPOTENCY_PAYLOAD_MISMATCH`` -- it does not silently create another.
* It can accept a refund and then fail to answer (``accept_then_timeout``).
  That is the case the application has to survive without double-refunding.
* It exposes a lookup by idempotency key, which is the only honest way to find
  out what happened after an uncertain response.

The provider keeps its own ledger (``mock_provider_refunds``) and never reads
application tables.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import FaultInjection, ProviderRefund, utcnow
from app.services.common import iso


class FaultMode:
    NONE = "none"
    TIMEOUT = "timeout"                      # never reaches the provider
    ACCEPT_THEN_TIMEOUT = "accept_then_timeout"  # recorded, response lost
    UNAVAILABLE = "unavailable"              # 503, safe to retry
    REJECT = "reject"                        # provider declines
    SLOW = "slow"                            # succeeds, but late
    PENDING = "pending"                       # accepted, settles later

    ALL = (NONE, TIMEOUT, ACCEPT_THEN_TIMEOUT, UNAVAILABLE, REJECT, SLOW, PENDING)


class ProviderTimeout(Exception):
    """The call may or may not have been applied. Reconcile, never retry blind."""


class ProviderUnavailable(Exception):
    """The call definitely did not reach the ledger. Safe to retry."""


class ProviderRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PayloadMismatch(Exception):
    """Same idempotency key, different payload."""


@dataclass(slots=True)
class RefundRecord:
    provider_reference: str
    status: str  # processing | succeeded | failed
    amount_minor: int
    currency: str
    idempotency_key: str
    created_at: str | None
    settled_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_reference": self.provider_reference,
            "status": self.status,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
        }


def _to_record(row: ProviderRefund) -> RefundRecord:
    return RefundRecord(
        provider_reference=row.id,
        status=row.status,
        amount_minor=row.amount_minor,
        currency=row.currency,
        idempotency_key=row.idempotency_key,
        created_at=iso(row.created_at),
        settled_at=iso(row.settled_at),
    )


# --------------------------------------------------------------------------- #
# Fault switchboard (demo control surface)
# --------------------------------------------------------------------------- #
def set_fault(service: str, mode: str, remaining_uses: int = 1) -> dict[str, Any]:
    if mode not in FaultMode.ALL:
        raise ValueError(f"unknown fault mode: {mode}")
    with session_scope() as session:
        row = session.get(FaultInjection, service)
        if row is None:
            row = FaultInjection(id=service, service=service)
            session.add(row)
        row.mode = mode
        row.remaining_uses = 0 if mode == FaultMode.NONE else remaining_uses
        session.flush()
        return {"service": service, "mode": row.mode, "remaining_uses": row.remaining_uses}


def get_fault(service: str) -> dict[str, Any]:
    with session_scope() as session:
        row = session.get(FaultInjection, service)
        if row is None:
            return {"service": service, "mode": FaultMode.NONE, "remaining_uses": 0}
        return {"service": service, "mode": row.mode, "remaining_uses": row.remaining_uses}


def clear_faults() -> None:
    with session_scope() as session:
        for row in session.scalars(select(FaultInjection)).all():
            row.mode = FaultMode.NONE
            row.remaining_uses = 0


def _consume_fault(service: str) -> str:
    """Pop one use of the configured fault, if any."""
    with session_scope() as session:
        row = session.get(FaultInjection, service)
        if row is None or row.mode == FaultMode.NONE or row.remaining_uses <= 0:
            return FaultMode.NONE
        mode = row.mode
        row.remaining_uses -= 1
        if row.remaining_uses <= 0:
            row.mode = FaultMode.NONE
        session.flush()
        return mode


# --------------------------------------------------------------------------- #
# Provider API
# --------------------------------------------------------------------------- #
SERVICE = "payments"


def create_refund(
    *,
    payment_reference: str,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    request_hash: str,
) -> RefundRecord:
    if amount_minor <= 0:
        raise ProviderRejected("amount_must_be_positive")

    # Replay check comes first: a repeated call must never be affected by a
    # fault mode configured after the original succeeded.
    with session_scope() as session:
        existing = session.scalar(
            select(ProviderRefund).where(ProviderRefund.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise PayloadMismatch(
                    f"idempotency key {idempotency_key} was used with a different payload"
                )
            return _to_record(existing)

    fault = _consume_fault(SERVICE)

    if fault == FaultMode.UNAVAILABLE:
        raise ProviderUnavailable("refund service temporarily unavailable")
    if fault == FaultMode.TIMEOUT:
        # Nothing written: the request never landed. Still *unknown* to the
        # caller, which is why the caller must reconcile rather than assume.
        raise ProviderTimeout("no response from refund service")
    if fault == FaultMode.REJECT:
        raise ProviderRejected("refund_declined_by_issuer")
    if fault == FaultMode.SLOW:
        time.sleep(0.4)

    settles_now = fault != FaultMode.PENDING
    reference = f"prv_rf_{uuid.uuid4().hex[:16]}"
    with session_scope() as session:
        row = ProviderRefund(
            id=reference,
            idempotency_key=idempotency_key,
            payment_reference=payment_reference,
            amount_minor=amount_minor,
            currency=currency,
            request_hash=request_hash,
            status="succeeded" if settles_now else "processing",
            settled_at=utcnow() if settles_now else None,
        )
        session.add(row)
        session.flush()
        record = _to_record(row)

    if fault == FaultMode.ACCEPT_THEN_TIMEOUT:
        # The money moved. The caller will never see this response.
        raise ProviderTimeout("connection reset after refund was accepted")

    return record


def get_refund_by_key(idempotency_key: str) -> RefundRecord | None:
    """The reconciliation entry point."""
    with session_scope() as session:
        row = session.scalar(
            select(ProviderRefund).where(ProviderRefund.idempotency_key == idempotency_key)
        )
        return _to_record(row) if row else None


def get_refund(provider_reference: str) -> RefundRecord | None:
    with session_scope() as session:
        row = session.get(ProviderRefund, provider_reference)
        return _to_record(row) if row else None


def settle_pending(idempotency_key: str, *, succeed: bool = True) -> RefundRecord | None:
    """Simulates the provider's async settlement webhook."""
    with session_scope() as session:
        row = session.scalar(
            select(ProviderRefund).where(ProviderRefund.idempotency_key == idempotency_key)
        )
        if row is None:
            return None
        if row.status == "processing":
            row.status = "succeeded" if succeed else "failed"
            row.settled_at = utcnow()
            session.flush()
        return _to_record(row)


def list_refunds(payment_reference: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as session:
        stmt = select(ProviderRefund).order_by(ProviderRefund.created_at)
        if payment_reference:
            stmt = stmt.where(ProviderRefund.payment_reference == payment_reference)
        return [_to_record(row).as_dict() for row in session.scalars(stmt).all()]
