"""Order, return, payment and shipment reads.

Ownership is enforced *here*, in the service, not at the tool boundary and not
by the model. An order id that appears in a customer message or in model output
grants nothing: it is checked against the authenticated principal every time.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.db.models import Order, OrderItem, Payment, Return
from app.services import audit
from app.services.common import iso, retry_read
from app.services.errors import ErrorCode, ToolError
from mock_services.payments.provider import ProviderTimeout, ProviderUnavailable
from mock_services.shipping import provider as shipping_provider

NOT_AVAILABLE_MESSAGE = "No order with that reference is available on your account."


def load_owned_order(session: Session, principal: Principal, order_id: str) -> Order:
    """The single chokepoint for order access.

    A non-existent order and someone else's order produce the *identical*
    failure -- same code, same message, same details. Returning
    ORDER_NOT_OWNED to the caller would confirm that the id exists, which is
    exactly how order ids get enumerated. The real reason is recorded in the
    audit trail instead, where only operators see it.
    """
    customer_id = principal.require_customer()
    order = session.get(Order, order_id)
    if order is None or order.customer_id != customer_id:
        if order is not None:
            audit.record(
                session,
                actor=f"customer:{customer_id}",
                event_type="access.denied",
                severity="warning",
                message="Attempt to access an order on another account.",
                payload={"order_id": order_id, "reason": ErrorCode.ORDER_NOT_OWNED},
            )
        raise ToolError(ErrorCode.ORDER_NOT_FOUND, NOT_AVAILABLE_MESSAGE, order_id=order_id)
    return order


def list_customer_orders(session: Session, principal: Principal) -> list[dict[str, Any]]:
    customer_id = principal.require_customer()
    orders = session.scalars(
        select(Order).where(Order.customer_id == customer_id).order_by(Order.placed_at.desc())
    ).all()
    return [_order_summary(session, order) for order in orders]


def _order_summary(session: Session, order: Order) -> dict[str, Any]:
    items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    return {
        "order_id": order.id,
        "status": order.status,
        "currency": order.currency,
        "paid_amount_minor": order.paid_amount_minor,
        "placed_at": iso(order.placed_at),
        "delivered_at": iso(order.delivered_at),
        "payment_method": order.payment_method,
        "items": [
            {
                "order_item_id": item.id,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "category": item.category,
                "quantity": item.quantity,
                "paid_amount_minor": item.paid_amount_minor,
            }
            for item in items
        ],
    }


def get_order(session: Session, principal: Principal, order_id: str) -> dict[str, Any]:
    order = load_owned_order(session, principal, order_id)
    return _order_summary(session, order)


def get_return_status(session: Session, principal: Principal, order_id: str) -> dict[str, Any]:
    order = load_owned_order(session, principal, order_id)
    returns = session.scalars(
        select(Return).where(Return.order_id == order.id).order_by(Return.requested_at)
    ).all()
    return {
        "order_id": order.id,
        "returns": [
            {
                "return_id": row.id,
                "order_item_id": row.order_item_id,
                "status": row.status,
                "reason": row.reason,
                "requested_at": iso(row.requested_at),
                "received_at": iso(row.received_at),
                "inspection_status": row.inspection_status,
            }
            for row in returns
        ],
    }


def get_payment_status(session: Session, principal: Principal, order_id: str) -> dict[str, Any]:
    from app.services.refunds import refundable_balance  # local import: cycle

    order = load_owned_order(session, principal, order_id)
    payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
    if payment is None:
        raise ToolError(
            ErrorCode.PAYMENT_NOT_CAPTURED,
            "No captured payment is recorded against this order.",
            order_id=order.id,
        )
    balance = refundable_balance(session, order.id)
    return {
        "order_id": order.id,
        "payment_id": payment.id,
        "status": payment.status,
        "captured_amount_minor": payment.captured_amount_minor,
        "currency": payment.currency,
        "refunded_amount_minor": balance.committed_minor,
        "reserved_amount_minor": balance.reserved_minor,
        "refundable_amount_minor": balance.available_minor,
    }


def get_shipment_status(session: Session, principal: Principal, order_id: str) -> dict[str, Any]:
    order = load_owned_order(session, principal, order_id)

    def _call() -> dict[str, Any] | None:
        try:
            return shipping_provider.get_tracking(order.id)
        except (ProviderTimeout, ProviderUnavailable) as exc:
            raise ToolError(
                ErrorCode.PROVIDER_UNAVAILABLE, str(exc), retryable=True, service="shipping"
            ) from exc

    tracking = retry_read(_call)
    return {
        "order_id": order.id,
        "order_status": order.status,
        "tracking": tracking,
        "tracking_available": tracking is not None,
    }
