"""Demo-only endpoints.

These exist so the dashboard can show the failure behaviour of the system
without a terminal: inject a provider fault, trigger recovery, inspect the
mock provider's ledger. They are mounted under /demo and are gated by
`settings.environment` so they cannot be enabled by accident in a deployment.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.auth.deps import CurrentPrincipal, DbSession
from app.config import get_settings
from app.db.models import AuthToken, Customer, Order, RefundOperation
from app.schemas.api import FaultRequest
from app.services import refunds
from app.services.common import format_money, iso
from app.workflows import runner
from mock_services.payments import provider as payment_provider

router = APIRouter(prefix="/demo", tags=["demo"])


def _guard() -> None:
    if get_settings().environment not in ("local", "demo", "test"):
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND"})


@router.get("/identities")
def identities(session: DbSession) -> dict[str, Any]:
    """The demo's bearer tokens, so the dashboard can offer a persona switch."""
    _guard()
    rows = session.scalars(select(AuthToken).order_by(AuthToken.role, AuthToken.token)).all()
    out = []
    for row in rows:
        customer = session.get(Customer, row.customer_id) if row.customer_id else None
        out.append(
            {
                "token": row.token,
                "role": row.role,
                "display_name": row.display_name,
                "customer_id": row.customer_id,
                "reviewer_id": row.reviewer_id,
                "scopes": row.scopes,
                "email": customer.email if customer else None,
            }
        )
    return {"identities": out}


@router.get("/orders")
def orders(principal: CurrentPrincipal, session: DbSession) -> dict[str, Any]:
    """Orders for the signed-in customer, with the seed note explaining the case."""
    _guard()
    from scripts.seed_data import ORDERS as SEED_ORDERS

    notes = {spec.id: spec.note for spec in SEED_ORDERS}
    if not principal.is_customer:
        rows = session.scalars(select(Order).order_by(Order.placed_at.desc())).all()
    else:
        rows = session.scalars(
            select(Order)
            .where(Order.customer_id == principal.customer_id)
            .order_by(Order.placed_at.desc())
        ).all()
    out = []
    for order in rows:
        balance = refunds.refundable_balance(session, order.id)
        out.append(
            {
                "order_id": order.id,
                "status": order.status,
                "paid_amount_minor": order.paid_amount_minor,
                "amount_display": format_money(order.paid_amount_minor, order.currency),
                "currency": order.currency,
                "placed_at": iso(order.placed_at),
                "delivered_at": iso(order.delivered_at),
                "refundable_minor": balance.available_minor,
                "items": [item.product_name for item in order.items],
                "seed_note": notes.get(order.id, ""),
            }
        )
    return {"orders": out}


@router.get("/faults")
def get_faults() -> dict[str, Any]:
    _guard()
    return {
        "faults": [payment_provider.get_fault(s) for s in ("payments", "shipping", "ticketing")],
        "modes": list(payment_provider.FaultMode.ALL),
    }


@router.post("/faults")
def set_fault(payload: FaultRequest) -> dict[str, Any]:
    _guard()
    return payment_provider.set_fault(payload.service, payload.mode, payload.remaining_uses)


@router.post("/faults/clear")
def clear_faults() -> dict[str, Any]:
    _guard()
    payment_provider.clear_faults()
    return {"cleared": True}


@router.post("/recover")
def recover() -> dict[str, Any]:
    """What a worker does on restart: reconcile money, then resume workflows."""
    _guard()
    return runner.recover_unfinished()


@router.get("/ledger")
def ledger(session: DbSession) -> dict[str, Any]:
    """Side-by-side view of our refund operations and the provider's records.

    This is the panel that proves no duplicate refund was created: one row per
    idempotency key on both sides.
    """
    _guard()
    operations = [
        refunds.get_operation(session, row.id)
        for row in session.scalars(
            select(RefundOperation).order_by(RefundOperation.created_at.desc())
        ).all()
    ]
    provider_rows = payment_provider.list_refunds()
    keys = [op["idempotency_key"] for op in operations]
    return {
        "operations": operations,
        "provider_refunds": provider_rows,
        "duplicate_keys": sorted({key for key in keys if keys.count(key) > 1}),
        "totals": {
            "our_operations": len(operations),
            "provider_records": len(provider_rows),
            "succeeded_minor": sum(
                op["amount_minor"] for op in operations if op["status"] == "succeeded"
            ),
        },
    }


@router.post("/settle-pending")
def settle_pending(idempotency_key: str, succeed: bool = True) -> dict[str, Any]:
    """Stands in for the provider's settlement webhook."""
    _guard()
    record = payment_provider.settle_pending(idempotency_key, succeed=succeed)
    return {"settled": record.as_dict() if record else None}
