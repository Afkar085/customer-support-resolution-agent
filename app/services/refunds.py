"""Refund accounting and execution.

The execution pattern, in order, and why each step is where it is:

1. Reserve inside a short transaction holding the order row (Postgres) or the
   order mutex (SQLite). The reservation is what stops two concurrent runs
   from both seeing the full refundable balance.
2. Commit *before* calling the provider. A row that says "we are about to
   move money with key K" is what makes recovery possible.
3. Call the provider with a derived, stable idempotency key. No transaction is
   open during the network call.
4. Persist whatever came back. If nothing came back, the operation becomes
   ``unknown`` -- never ``failed`` and never silently retried.
5. Reconcile by asking the provider about key K. Only the provider can say
   whether the money moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import lock_order, session_scope
from app.db.models import (
    ActionProposal,
    Approval,
    Order,
    Payment,
    RefundOperation,
    utcnow,
)
from app.services import audit
from app.services.common import as_aware, idempotency_key, stable_hash
from app.services.errors import ErrorCode, ToolError
from mock_services.payments import provider as payment_provider
from mock_services.payments.provider import (
    PayloadMismatch,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)

MAX_PROVIDER_ATTEMPTS = 3
MAX_RECONCILE_ATTEMPTS = 5


@dataclass(slots=True)
class RefundableBalance:
    order_id: str
    captured_minor: int
    committed_minor: int
    reserved_minor: int
    currency: str

    @property
    def available_minor(self) -> int:
        return max(0, self.captured_minor - self.committed_minor - self.reserved_minor)

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "captured_minor": self.captured_minor,
            "committed_minor": self.committed_minor,
            "reserved_minor": self.reserved_minor,
            "available_minor": self.available_minor,
            "currency": self.currency,
        }


def refundable_balance(session: Session, order_id: str) -> RefundableBalance:
    order = session.get(Order, order_id)
    if order is None:
        raise ToolError(ErrorCode.ORDER_NOT_FOUND, "Order not found.", order_id=order_id)
    payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
    captured = payment.captured_amount_minor if payment else order.paid_amount_minor

    def _sum(statuses: tuple[str, ...]) -> int:
        total = session.scalar(
            select(func.coalesce(func.sum(RefundOperation.amount_minor), 0)).where(
                RefundOperation.order_id == order_id,
                RefundOperation.status.in_(statuses),
            )
        )
        return int(total or 0)

    return RefundableBalance(
        order_id=order_id,
        captured_minor=captured,
        committed_minor=_sum(RefundOperation.COMMITTED),
        reserved_minor=_sum(RefundOperation.IN_FLIGHT),
        currency=order.currency,
    )


def _request_hash(proposal: ActionProposal, amount_minor: int) -> str:
    return stable_hash(
        {
            "order_id": proposal.payload.get("order_id"),
            "amount_minor": amount_minor,
            "currency": proposal.payload.get("currency"),
            "proposal_id": proposal.id,
        }
    )


# --------------------------------------------------------------------------- #
# Step 1-2: reserve, then commit before any network call
# --------------------------------------------------------------------------- #
def reserve_operation(
    session: Session, *, proposal: ActionProposal, approval: Approval
) -> RefundOperation:
    """Create (or return) the durable operation row for an approved proposal.

    Safe to call again after a crash: the derived idempotency key makes the
    second call find the first row instead of creating a second one.
    """
    amount_minor = int(proposal.payload["amount_minor"])
    order_id = str(proposal.payload["order_id"])
    key = idempotency_key(proposal.id, proposal.payload_hash)

    existing = session.scalar(
        select(RefundOperation).where(RefundOperation.idempotency_key == key)
    )
    if existing is not None:
        return existing

    with lock_order(session, order_id):
        balance = refundable_balance(session, order_id)
        if amount_minor > balance.available_minor:
            raise ToolError(
                ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE,
                "The requested amount exceeds the refundable balance for this order.",
                requested_minor=amount_minor,
                available_minor=balance.available_minor,
            )
        operation = RefundOperation(
            order_id=order_id,
            proposal_id=proposal.id,
            approval_id=approval.id,
            run_id=proposal.run_id,
            amount_minor=amount_minor,
            currency=str(proposal.payload.get("currency", get_settings().currency)),
            idempotency_key=key,
            request_payload_hash=_request_hash(proposal, amount_minor),
            status="reserved",
        )
        session.add(operation)
        try:
            session.flush()
        except IntegrityError:
            # Another worker won the race; its row is the one that counts.
            session.rollback()
            found = session.scalar(
                select(RefundOperation).where(RefundOperation.idempotency_key == key)
            )
            if found is None:
                raise
            return found
    return operation


# --------------------------------------------------------------------------- #
# Step 3-5: call the provider with no transaction open, then persist
# --------------------------------------------------------------------------- #
def execute_operation(operation_id: str) -> dict[str, Any]:
    """Drive one refund operation to a settled or explicitly-unknown state."""
    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        if operation is None:
            raise ToolError(
                ErrorCode.INTERNAL_ERROR, "Refund operation not found.", operation_id=operation_id
            )
        if operation.status in ("succeeded", "failed", "cancelled"):
            return _operation_view(operation)
        if operation.attempt_count >= MAX_PROVIDER_ATTEMPTS and operation.status != "reserved":
            return reconcile_operation(operation_id)

        payment = session.scalar(select(Payment).where(Payment.order_id == operation.order_id))
        payment_reference = payment.provider_payment_id if payment else operation.order_id
        operation.status = "submitted"
        operation.attempt_count += 1
        run_id, ticket_amount = operation.run_id, operation.amount_minor
        key = operation.idempotency_key
        request_hash = operation.request_payload_hash
        amount_minor = operation.amount_minor
        currency = operation.currency
        audit.record(
            session,
            actor="system:refund_executor",
            event_type="refund.submitted",
            run_id=run_id,
            message=f"Submitting refund attempt {operation.attempt_count}.",
            payload={
                "operation_id": operation_id,
                "amount_minor": ticket_amount,
                "idempotency_key": key,
            },
        )
    # transaction closed -- now the network call

    try:
        record = payment_provider.create_refund(
            payment_reference=payment_reference,
            amount_minor=amount_minor,
            currency=currency,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except ProviderTimeout as exc:
        _mark(operation_id, status="unknown", error=str(exc), event="refund.outcome_unknown")
        return reconcile_operation(operation_id)
    except ProviderUnavailable as exc:
        # Definitively did not land: release the submission but keep the
        # reservation and the key, so a retry is the same refund.
        _mark(operation_id, status="reserved", error=str(exc), event="refund.provider_unavailable")
        return {
            **_load_view(operation_id),
            "retryable": True,
            "error_code": ErrorCode.PROVIDER_UNAVAILABLE,
        }
    except PayloadMismatch as exc:
        _mark(
            operation_id,
            status="failed",
            error=str(exc),
            event="refund.idempotency_conflict",
            severity="error",
        )
        raise ToolError(ErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH, str(exc)) from exc
    except ProviderRejected as exc:
        _mark(operation_id, status="failed", error=exc.reason, event="refund.rejected")
        return {**_load_view(operation_id), "error_code": ErrorCode.PROVIDER_REJECTED}

    return _apply_provider_record(operation_id, record)


def _apply_provider_record(
    operation_id: str, record: payment_provider.RefundRecord
) -> dict[str, Any]:
    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        assert operation is not None
        operation.provider_reference = record.provider_reference
        operation.provider_status = record.status
        operation.provider_response = record.as_dict()
        if record.status == "succeeded":
            operation.status = "succeeded"
            operation.settled_at = utcnow()
            _refresh_payment_status(session, operation.order_id)
        elif record.status == "failed":
            operation.status = "failed"
        else:
            operation.status = "pending_reconciliation"
        audit.record(
            session,
            actor="system:refund_executor",
            event_type=f"refund.{operation.status}",
            run_id=operation.run_id,
            severity="info" if operation.status != "failed" else "warning",
            message=f"Provider reported refund status '{record.status}'.",
            payload={
                "operation_id": operation_id,
                "provider_reference": record.provider_reference,
                "provider_status": record.status,
            },
        )
        return _operation_view(operation)


def reconcile_operation(operation_id: str) -> dict[str, Any]:
    """Ask the provider what actually happened to this idempotency key.

    This is the only thing allowed to resolve an ``unknown`` operation. It
    never issues a new refund.
    """
    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        if operation is None:
            raise ToolError(ErrorCode.INTERNAL_ERROR, "Refund operation not found.")
        if operation.status in ("succeeded", "failed", "cancelled"):
            return _operation_view(operation)
        operation.reconcile_attempts += 1
        key = operation.idempotency_key
        attempts = operation.reconcile_attempts
        run_id = operation.run_id

    record = payment_provider.get_refund_by_key(key)

    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        assert operation is not None
        if record is None:
            # The provider has no record, so no money moved. The same key can
            # safely be reused, which is why the status goes back to reserved.
            if attempts >= MAX_RECONCILE_ATTEMPTS:
                operation.status = "failed"
                operation.last_error = "provider has no record of this refund"
            else:
                operation.status = "reserved"
            event = "refund.reconciled_absent"
            message = "Provider has no record for this idempotency key; no money moved."
        else:
            operation.provider_reference = record.provider_reference
            operation.provider_status = record.status
            operation.provider_response = record.as_dict()
            if record.status == "succeeded":
                operation.status = "succeeded"
                operation.settled_at = utcnow()
                _refresh_payment_status(session, operation.order_id)
            elif record.status == "failed":
                operation.status = "failed"
            else:
                operation.status = "pending_reconciliation"
            event = "refund.reconciled"
            message = f"Reconciliation found provider status '{record.status}'."
        audit.record(
            session,
            actor="system:reconciler",
            event_type=event,
            run_id=run_id,
            message=message,
            payload={"operation_id": operation_id, "status": operation.status},
        )
        return _operation_view(operation)


def reconcile_unsettled(limit: int = 50) -> list[dict[str, Any]]:
    """Startup / worker-recovery sweep over anything still in flight."""
    with session_scope() as session:
        ids = [
            row.id
            for row in session.scalars(
                select(RefundOperation)
                .where(RefundOperation.status.in_(("unknown", "pending_reconciliation", "submitted")))
                .limit(limit)
            ).all()
        ]
    return [reconcile_operation(op_id) for op_id in ids]


def _mark(
    operation_id: str,
    *,
    status: str,
    error: str | None,
    event: str,
    severity: str = "warning",
) -> None:
    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        assert operation is not None
        operation.status = status
        operation.last_error = error
        audit.record(
            session,
            actor="system:refund_executor",
            event_type=event,
            run_id=operation.run_id,
            severity=severity,
            message=error or event,
            payload={"operation_id": operation_id, "status": status},
        )


def _refresh_payment_status(session: Session, order_id: str) -> None:
    payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None:
        return
    committed = int(
        session.scalar(
            select(func.coalesce(func.sum(RefundOperation.amount_minor), 0)).where(
                RefundOperation.order_id == order_id,
                RefundOperation.status.in_(RefundOperation.COMMITTED),
            )
        )
        or 0
    )
    if committed <= 0:
        payment.status = "captured"
    elif committed >= payment.captured_amount_minor:
        payment.status = "refunded"
    else:
        payment.status = "partially_refunded"


def _operation_view(operation: RefundOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "order_id": operation.order_id,
        "run_id": operation.run_id,
        "proposal_id": operation.proposal_id,
        "approval_id": operation.approval_id,
        "amount_minor": operation.amount_minor,
        "currency": operation.currency,
        "status": operation.status,
        "provider_status": operation.provider_status,
        "provider_reference": operation.provider_reference,
        "idempotency_key": operation.idempotency_key,
        "attempt_count": operation.attempt_count,
        "reconcile_attempts": operation.reconcile_attempts,
        "last_error": operation.last_error,
        "settled_at": operation.settled_at.isoformat() if operation.settled_at else None,
    }


def _load_view(operation_id: str) -> dict[str, Any]:
    with session_scope() as session:
        operation = session.get(RefundOperation, operation_id)
        assert operation is not None
        return _operation_view(operation)


def get_operation(session: Session, operation_id: str) -> dict[str, Any]:
    operation = session.get(RefundOperation, operation_id)
    if operation is None:
        raise ToolError(ErrorCode.INTERNAL_ERROR, "Refund operation not found.")
    return _operation_view(operation)


def operations_for_order(session: Session, order_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RefundOperation)
        .where(RefundOperation.order_id == order_id)
        .order_by(RefundOperation.created_at)
    ).all()
    return [_operation_view(row) for row in rows]


def customer_facing_status(status: str) -> str:
    """Keep submitted / processing / succeeded / failed distinct in wording."""
    return {
        "created": "not yet submitted",
        "reserved": "not yet submitted",
        "submitted": "submitted to the payment provider",
        "pending_reconciliation": "accepted by the payment provider and still processing",
        "unknown": "submitted, outcome not yet confirmed",
        "succeeded": "completed by the payment provider",
        "failed": "not completed",
        "cancelled": "cancelled",
    }.get(status, status)


__all__ = [
    "RefundableBalance",
    "refundable_balance",
    "reserve_operation",
    "execute_operation",
    "reconcile_operation",
    "reconcile_unsettled",
    "get_operation",
    "operations_for_order",
    "customer_facing_status",
    "as_aware",
]
