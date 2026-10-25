"""Deterministic business rules.

This module contains no LLM calls and no retrieval. A model may *propose* a
refund; whether one is permitted is decided here, in ordinary code, from rows
in the database. Retrieval similarity is never evidence of eligibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Order, Payment, Return
from app.services.common import as_aware, days_between, utcnow
from app.services.errors import ErrorCode
from app.services.refunds import refundable_balance


@dataclass(slots=True)
class Blocker:
    code: str
    message: str
    customer_visible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(slots=True)
class RefundDecision:
    order_id: str
    eligible: bool
    max_refundable_minor: int
    currency: str
    requires_approval: bool
    blockers: list[Blocker] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    escalate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "eligible": self.eligible,
            "max_refundable_minor": self.max_refundable_minor,
            "currency": self.currency,
            "requires_approval": self.requires_approval,
            "blockers": [b.as_dict() for b in self.blockers],
            "missing_information": list(self.missing_information),
            "facts": self.facts,
            "escalate": self.escalate,
        }

    @property
    def blocking_codes(self) -> list[str]:
        return [b.code for b in self.blockers]


def evaluate_refund(session: Session, order_id: str) -> RefundDecision:
    settings = get_settings()
    order = session.get(Order, order_id)
    if order is None:
        return RefundDecision(
            order_id=order_id,
            eligible=False,
            max_refundable_minor=0,
            currency=settings.currency,
            requires_approval=True,
            blockers=[Blocker(ErrorCode.ORDER_NOT_FOUND, "Order not found.")],
        )

    payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
    returns = session.scalars(
        select(Return).where(Return.order_id == order_id).order_by(Return.requested_at)
    ).all()
    balance = refundable_balance(session, order_id)

    blockers: list[Blocker] = []
    missing: list[str] = []
    escalate = False

    if payment is None or payment.status in ("failed", "voided"):
        blockers.append(
            Blocker(
                ErrorCode.PAYMENT_NOT_CAPTURED,
                "There is no captured payment on this order to refund.",
            )
        )

    # Pick the return that is furthest along, so one cancelled attempt does not
    # mask a completed one.
    rank = {"received": 4, "in_transit": 3, "label_issued": 2, "requested": 1, "closed": 4}
    active = [r for r in returns if r.status != "cancelled"]
    chosen = max(active, key=lambda r: rank.get(r.status, 0), default=None)

    if chosen is None:
        blockers.append(
            Blocker(
                ErrorCode.NO_RETURN_ON_RECORD,
                "No return has been started for this order, so there is nothing to refund yet.",
            )
        )
    else:
        if chosen.status not in ("received", "closed"):
            blockers.append(
                Blocker(
                    ErrorCode.RETURN_NOT_RECEIVED,
                    "The returned item has not been marked as received at the warehouse.",
                )
            )
        elif chosen.inspection_status == "pending":
            blockers.append(
                Blocker(
                    ErrorCode.INSPECTION_NOT_PASSED,
                    "The return has been received but inspection is still pending.",
                )
            )
        elif chosen.inspection_status == "failed":
            blockers.append(
                Blocker(
                    ErrorCode.INSPECTION_FAILED,
                    "Inspection of the returned item did not pass.",
                )
            )
            # A failed inspection is a judgement call, not a flat refusal.
            escalate = True

        if order.delivered_at is not None:
            elapsed = days_between(order.delivered_at, as_aware(chosen.requested_at) or utcnow())
            if elapsed > settings.return_window_days:
                blockers.append(
                    Blocker(
                        ErrorCode.RETURN_WINDOW_EXPIRED,
                        f"The return was requested {elapsed} days after delivery, outside the "
                        f"{settings.return_window_days}-day return window.",
                    )
                )

    if order.status == "cancelled":
        pass  # a cancelled order with a captured payment is refundable
    elif order.delivered_at is None and chosen is not None:
        blockers.append(
            Blocker(
                ErrorCode.ORDER_NOT_DELIVERED,
                "This order has not been recorded as delivered.",
            )
        )

    if balance.captured_minor > 0 and balance.committed_minor >= balance.captured_minor:
        blockers.append(
            Blocker(
                ErrorCode.ALREADY_FULLY_REFUNDED,
                "This order has already been refunded in full.",
            )
        )
    elif balance.available_minor <= 0 and balance.reserved_minor > 0:
        blockers.append(
            Blocker(
                ErrorCode.REFUND_ALREADY_IN_FLIGHT,
                "A refund for this order is already being processed.",
            )
        )

    facts = {
        "order_status": order.status,
        "delivered_at": as_aware(order.delivered_at).isoformat() if order.delivered_at else None,
        "payment_status": payment.status if payment else None,
        "return_id": chosen.id if chosen else None,
        "return_status": chosen.status if chosen else None,
        "inspection_status": chosen.inspection_status if chosen else None,
        "return_received_at": (
            as_aware(chosen.received_at).isoformat() if chosen and chosen.received_at else None
        ),
        "balance": balance.as_dict(),
        "return_window_days": settings.return_window_days,
    }

    return RefundDecision(
        order_id=order_id,
        eligible=not blockers,
        max_refundable_minor=balance.available_minor,
        currency=order.currency,
        # Section 3 of the brief: every refund needs a human, full stop.
        requires_approval=True,
        blockers=blockers,
        missing_information=missing,
        facts=facts,
        escalate=escalate,
    )


# --------------------------------------------------------------------------- #
# Damaged-item intake: what the policy requires before anything can be decided
# --------------------------------------------------------------------------- #
REQUIRED_DAMAGE_FIELDS = ("damage_description", "photo_provided")


def evaluate_damage_intake(
    session: Session, order_id: str, collected: dict[str, Any] | None = None
) -> dict[str, Any]:
    settings = get_settings()
    collected = collected or {}
    order = session.get(Order, order_id)
    missing: list[str] = []

    if not collected.get("damage_description"):
        missing.append("a short description of the damage")
    if not collected.get("photo_provided"):
        missing.append("a photo of the damaged item and its packaging")

    within_window = True
    if order is not None and order.delivered_at is not None:
        elapsed = days_between(order.delivered_at, utcnow())
        within_window = elapsed <= settings.return_window_days
    return {
        "order_id": order_id,
        "missing_information": missing,
        "report_within_window": within_window,
        "ready_for_resolution": not missing and within_window,
        "window_days": settings.return_window_days,
    }


def validate_proposed_amount(
    session: Session, order_id: str, amount_minor: int
) -> tuple[bool, list[Blocker]]:
    """Amount arithmetic is never delegated to the model."""
    problems: list[Blocker] = []
    if not isinstance(amount_minor, int) or amount_minor <= 0:
        problems.append(
            Blocker(ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE, "Refund amount must be a positive integer.")
        )
        return False, problems
    balance = refundable_balance(session, order_id)
    if amount_minor > balance.available_minor:
        problems.append(
            Blocker(
                ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE,
                "Requested amount exceeds the refundable balance for this order.",
            )
        )
    return not problems, problems
