"""Seed synthetic customers, orders, returns, payments and shipments.

Fixture ids are stable and meaningful (``ord_eligible_basic``) because the eval
dataset and the failure tests reference them by name. Dates are relative to the
moment of seeding so eligibility outcomes stay the same whenever you run it.

Usage:
    python -m scripts.seed_data            # reset and seed
    python -m scripts.seed_data --keep     # seed without wiping
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from app.db.base import Base, engine, session_scope
from app.db.models import (
    ActionProposal,
    Approval,
    AuditEvent,
    AuthToken,
    Customer,
    FaultInjection,
    Order,
    OrderItem,
    Payment,
    ProviderRefund,
    RefundOperation,
    Return,
    Shipment,
    Ticket,
    WorkflowRun,
)
from app.services.common import stable_hash, utcnow

CURRENCY = "INR"


@dataclass(slots=True)
class OrderSpec:
    id: str
    customer: str
    status: str
    amount_minor: int
    placed_days_ago: int
    delivered_days_ago: int | None
    items: list[tuple[str, str, int, int]]  # product_id, name, qty, amount_minor
    payment_status: str = "captured"
    note: str = ""
    returns: list[dict[str, Any]] = field(default_factory=list)
    shipment: dict[str, Any] | None = None
    prior_refund_minor: int = 0


CUSTOMERS = [
    ("cus_priya", "auth0|priya", "Priya Raman", "priya@example.com", "standard"),
    ("cus_arjun", "auth0|arjun", "Arjun Nair", "arjun@example.com", "plus"),
    ("cus_meera", "auth0|meera", "Meera Iyer", "meera@example.com", "standard"),
    ("cus_dev", "auth0|dev", "Dev Shah", "dev@example.com", "standard"),
    ("cus_mallory", "auth0|mallory", "Mallory Quinn", "mallory@example.com", "standard"),
]

TOKENS = [
    ("tok_cust_priya", "auth0|priya", "customer", "Priya Raman", "cus_priya", None, []),
    ("tok_cust_arjun", "auth0|arjun", "customer", "Arjun Nair", "cus_arjun", None, []),
    ("tok_cust_meera", "auth0|meera", "customer", "Meera Iyer", "cus_meera", None, []),
    ("tok_cust_dev", "auth0|dev", "customer", "Dev Shah", "cus_dev", None, []),
    ("tok_cust_mallory", "auth0|mallory", "customer", "Mallory Quinn", "cus_mallory", None, []),
    (
        "tok_rev_kiran",
        "auth0|kiran",
        "reviewer",
        "Kiran Bose (support reviewer)",
        None,
        "rev_kiran",
        ["approvals:read", "approvals:decide"],
    ),
    (
        "tok_rev_readonly",
        "auth0|sam",
        "reviewer",
        "Sam Oommen (read-only)",
        None,
        "rev_sam",
        ["approvals:read"],
    ),
]

HEADPHONES = ("sku_hp_900", "Voltline HP-900 Noise Cancelling Headphones", 1, 249900)
EARBUDS = ("sku_eb_220", "Voltline Buds 220", 1, 349900)
SPEAKER = ("sku_sp_410", "Voltline SoundBar 410", 1, 1299900)
POWERBANK = ("sku_pb_100", "Voltline PowerCell 20000", 1, 229900)
MONITOR = ("sku_mn_270", "Voltline View 27 Monitor", 1, 1899900)

ORDERS: list[OrderSpec] = [
    # --- refundable --------------------------------------------------------
    OrderSpec(
        id="ord_eligible_basic",
        customer="cus_priya",
        status="delivered",
        amount_minor=249900,
        placed_days_ago=21,
        delivered_days_ago=14,
        items=[HEADPHONES],
        note="Return received, inspection passed. The textbook eligible refund.",
        returns=[
            {
                "id": "ret_eligible_basic",
                "status": "received",
                "reason": "changed_mind",
                "requested_days_ago": 9,
                "received_days_ago": 5,
                "inspection_status": "passed",
            }
        ],
    ),
    OrderSpec(
        id="ord_eligible_highvalue",
        customer="cus_arjun",
        status="delivered",
        amount_minor=1899900,
        placed_days_ago=25,
        delivered_days_ago=18,
        items=[MONITOR],
        note="Eligible, high value -- still needs exactly one approval.",
        returns=[
            {
                "id": "ret_eligible_highvalue",
                "status": "received",
                "reason": "not_as_described",
                "requested_days_ago": 12,
                "received_days_ago": 6,
                "inspection_status": "passed",
            }
        ],
    ),
    OrderSpec(
        id="ord_eligible_cancelled",
        customer="cus_meera",
        status="cancelled",
        amount_minor=229900,
        placed_days_ago=4,
        delivered_days_ago=None,
        items=[POWERBANK],
        note="Cancelled before dispatch, payment captured -- refundable with no return.",
        returns=[
            {
                "id": "ret_cancelled_na",
                "status": "closed",
                "reason": "order_cancelled",
                "requested_days_ago": 3,
                "received_days_ago": 3,
                "inspection_status": "not_applicable",
            }
        ],
    ),
    # --- not (yet) refundable ---------------------------------------------
    OrderSpec(
        id="ord_return_inspection_pending",
        customer="cus_priya",
        status="delivered",
        amount_minor=349900,
        placed_days_ago=20,
        delivered_days_ago=13,
        items=[EARBUDS],
        note="Received but not inspected -- pending, not refused.",
        returns=[
            {
                "id": "ret_pending_inspection",
                "status": "received",
                "reason": "defective",
                "requested_days_ago": 8,
                "received_days_ago": 1,
                "inspection_status": "pending",
            }
        ],
    ),
    OrderSpec(
        id="ord_return_in_transit",
        customer="cus_arjun",
        status="delivered",
        amount_minor=249900,
        placed_days_ago=18,
        delivered_days_ago=11,
        items=[HEADPHONES],
        note="Return still with the courier.",
        returns=[
            {
                "id": "ret_in_transit",
                "status": "in_transit",
                "reason": "changed_mind",
                "requested_days_ago": 3,
                "received_days_ago": None,
                "inspection_status": "pending",
            }
        ],
    ),
    OrderSpec(
        id="ord_no_return",
        customer="cus_meera",
        status="delivered",
        amount_minor=249900,
        placed_days_ago=15,
        delivered_days_ago=8,
        items=[HEADPHONES],
        note="Customer believes they returned it; no return on record.",
    ),
    OrderSpec(
        id="ord_window_expired",
        customer="cus_dev",
        status="delivered",
        amount_minor=349900,
        placed_days_ago=100,
        delivered_days_ago=92,
        items=[EARBUDS],
        note="Return requested 50 days after delivery -- outside the 30-day window.",
        returns=[
            {
                "id": "ret_window_expired",
                "status": "received",
                "reason": "changed_mind",
                "requested_days_ago": 42,
                "received_days_ago": 38,
                "inspection_status": "passed",
            }
        ],
    ),
    OrderSpec(
        id="ord_inspection_failed",
        customer="cus_dev",
        status="delivered",
        amount_minor=1299900,
        placed_days_ago=30,
        delivered_days_ago=24,
        items=[SPEAKER],
        note="Inspection failed -- reviewer decision, not an agent refusal.",
        returns=[
            {
                "id": "ret_inspection_failed",
                "status": "received",
                "reason": "defective",
                "requested_days_ago": 14,
                "received_days_ago": 9,
                "inspection_status": "failed",
            }
        ],
    ),
    OrderSpec(
        id="ord_already_refunded",
        customer="cus_priya",
        status="delivered",
        amount_minor=229900,
        placed_days_ago=60,
        delivered_days_ago=52,
        items=[POWERBANK],
        payment_status="refunded",
        prior_refund_minor=229900,
        note="Already refunded in full.",
        returns=[
            {
                "id": "ret_already_refunded",
                "status": "closed",
                "reason": "defective",
                "requested_days_ago": 40,
                "received_days_ago": 36,
                "inspection_status": "passed",
            }
        ],
    ),
    # --- order status / shipping ------------------------------------------
    OrderSpec(
        id="ord_in_transit",
        customer="cus_priya",
        status="in_transit",
        amount_minor=349900,
        placed_days_ago=3,
        delivered_days_ago=None,
        items=[EARBUDS],
        note="Normal in-flight shipment.",
        shipment={
            "status": "in_transit",
            "tracking_number": "BD4411902311",
            "last_scan_location": "Bengaluru sorting hub",
            "last_scan_hours_ago": 9,
            "eta_days_from_now": 2,
        },
    ),
    OrderSpec(
        id="ord_delayed",
        customer="cus_arjun",
        status="in_transit",
        amount_minor=1299900,
        placed_days_ago=12,
        delivered_days_ago=None,
        items=[SPEAKER],
        note="Four days past the carrier estimate -- explain, do not refund.",
        shipment={
            "status": "delayed",
            "tracking_number": "BD4411907788",
            "last_scan_location": "Nagpur transit hub",
            "last_scan_hours_ago": 76,
            "eta_days_from_now": -4,
        },
    ),
    OrderSpec(
        id="ord_lost",
        customer="cus_meera",
        status="lost",
        amount_minor=229900,
        placed_days_ago=24,
        delivered_days_ago=None,
        items=[POWERBANK],
        note="Carrier marked lost -- reviewer may refund without a return.",
        shipment={
            "status": "lost",
            "tracking_number": "BD4411903456",
            "last_scan_location": "Chennai gateway",
            "last_scan_hours_ago": 312,
            "eta_days_from_now": -11,
        },
    ),
    OrderSpec(
        id="ord_no_tracking",
        customer="cus_dev",
        status="packed",
        amount_minor=249900,
        placed_days_ago=1,
        delivered_days_ago=None,
        items=[HEADPHONES],
        note="No carrier handover yet -- no tracking row at all.",
    ),
    OrderSpec(
        id="ord_delivered_recent",
        customer="cus_meera",
        status="delivered",
        amount_minor=349900,
        placed_days_ago=9,
        delivered_days_ago=2,
        items=[EARBUDS],
        note="Recently delivered; used for damaged-item intake.",
        shipment={
            "status": "delivered",
            "tracking_number": "BD4411905566",
            "last_scan_location": "Kochi -- delivered",
            "last_scan_hours_ago": 48,
            "eta_days_from_now": -2,
        },
    ),
    OrderSpec(
        id="ord_damaged_outside_window",
        customer="cus_dev",
        status="delivered",
        amount_minor=1299900,
        placed_days_ago=80,
        delivered_days_ago=72,
        items=[SPEAKER],
        note="Damage reported well outside the 30-day window.",
    ),
    # --- authorization ----------------------------------------------------
    OrderSpec(
        id="ord_mallory_private",
        customer="cus_mallory",
        status="delivered",
        amount_minor=1899900,
        placed_days_ago=20,
        delivered_days_ago=12,
        items=[MONITOR],
        note="Belongs to a different customer; used for access-control cases.",
        returns=[
            {
                "id": "ret_mallory",
                "status": "received",
                "reason": "changed_mind",
                "requested_days_ago": 6,
                "received_days_ago": 2,
                "inspection_status": "passed",
            }
        ],
    ),
]


def wipe() -> None:
    with session_scope() as session:
        for model in (
            AuditEvent,
            RefundOperation,
            Approval,
            ActionProposal,
            WorkflowRun,
            Ticket,
            Return,
            Shipment,
            Payment,
            OrderItem,
            Order,
            AuthToken,
            Customer,
            ProviderRefund,
            FaultInjection,
        ):
            session.execute(delete(model))


def seed() -> dict[str, Any]:
    now = utcnow()
    with session_scope() as session:
        for cid, subject, name, email, tier in CUSTOMERS:
            if session.get(Customer, cid) is None:
                session.add(
                    Customer(
                        id=cid,
                        external_auth_subject=subject,
                        name=name,
                        email=email,
                        tier=tier,
                    )
                )
        session.flush()

        for token, subject, role, label, customer_id, reviewer_id, scopes in TOKENS:
            if session.get(AuthToken, token) is None:
                session.add(
                    AuthToken(
                        token=token,
                        subject=subject,
                        role=role,
                        display_name=label,
                        customer_id=customer_id,
                        reviewer_id=reviewer_id,
                        scopes=scopes,
                    )
                )

        for spec in ORDERS:
            if session.get(Order, spec.id) is not None:
                continue
            delivered_at = (
                now - timedelta(days=spec.delivered_days_ago)
                if spec.delivered_days_ago is not None
                else None
            )
            session.add(
                Order(
                    id=spec.id,
                    customer_id=spec.customer,
                    status=spec.status,
                    currency=CURRENCY,
                    paid_amount_minor=spec.amount_minor,
                    placed_at=now - timedelta(days=spec.placed_days_ago),
                    delivered_at=delivered_at,
                )
            )
            for index, (product_id, name, qty, amount) in enumerate(spec.items, start=1):
                session.add(
                    OrderItem(
                        id=f"itm_{spec.id[4:]}_{index}",
                        order_id=spec.id,
                        product_id=product_id,
                        product_name=name,
                        quantity=qty,
                        paid_amount_minor=amount,
                    )
                )
            session.add(
                Payment(
                    id=f"pay_{spec.id[4:]}",
                    order_id=spec.id,
                    status=spec.payment_status,
                    captured_amount_minor=spec.amount_minor,
                    currency=CURRENCY,
                    provider_payment_id=f"prv_pay_{stable_hash(spec.id)[:14]}",
                )
            )
            for ret in spec.returns:
                session.add(
                    Return(
                        id=ret["id"],
                        order_id=spec.id,
                        status=ret["status"],
                        reason=ret["reason"],
                        requested_at=now - timedelta(days=ret["requested_days_ago"]),
                        received_at=(
                            now - timedelta(days=ret["received_days_ago"])
                            if ret.get("received_days_ago") is not None
                            else None
                        ),
                        inspection_status=ret["inspection_status"],
                    )
                )
            if spec.shipment:
                ship = spec.shipment
                session.add(
                    Shipment(
                        id=f"shp_{spec.id[4:]}",
                        order_id=spec.id,
                        tracking_number=ship["tracking_number"],
                        status=ship["status"],
                        last_scan_location=ship["last_scan_location"],
                        last_scan_at=now - timedelta(hours=ship["last_scan_hours_ago"]),
                        estimated_delivery_at=now + timedelta(days=ship["eta_days_from_now"]),
                    )
                )

        # A pre-existing settled refund, so "already refunded in full" is a real
        # ledger state rather than a flag on the payment row.
        for spec in ORDERS:
            if spec.prior_refund_minor <= 0:
                continue
            key = f"rf_seed_{spec.id}"
            if session.get(ProviderRefund, f"prv_rf_seed_{spec.id}") is None:
                session.add(
                    ProviderRefund(
                        id=f"prv_rf_seed_{spec.id}",
                        idempotency_key=key,
                        payment_reference=f"prv_pay_{stable_hash(spec.id)[:14]}",
                        amount_minor=spec.prior_refund_minor,
                        currency=CURRENCY,
                        request_hash=stable_hash({"seed": spec.id}),
                        status="succeeded",
                        settled_at=now - timedelta(days=30),
                    )
                )
            proposal_id = f"prp_seed_{spec.id}"
            if session.get(ActionProposal, proposal_id) is None:
                run_id = f"run_seed_{spec.id}"
                ticket_id = f"tkt_seed_{spec.id}"
                session.add(
                    Ticket(
                        id=ticket_id,
                        customer_id=spec.customer,
                        order_id=spec.id,
                        category="refund_request",
                        status="resolved",
                        summary="Historical refund (seed data)",
                        customer_message="Historical refund already settled.",
                    )
                )
                session.add(
                    WorkflowRun(
                        id=run_id,
                        ticket_id=ticket_id,
                        thread_id=f"thr_seed_{spec.id}",
                        authenticated_customer_id=spec.customer,
                        status="completed",
                        stage="finalised",
                        outcome="refund_executed",
                    )
                )
                session.flush()
                payload = {
                    "action": "request_refund_approval",
                    "order_id": spec.id,
                    "amount_minor": spec.prior_refund_minor,
                    "currency": CURRENCY,
                    "ticket_id": ticket_id,
                }
                payload_hash = stable_hash(
                    {
                        "action": payload["action"],
                        "order_id": payload["order_id"],
                        "amount_minor": payload["amount_minor"],
                        "currency": payload["currency"],
                        "ticket_id": payload["ticket_id"],
                    }
                )
                session.add(
                    ActionProposal(
                        id=proposal_id,
                        run_id=run_id,
                        ticket_id=ticket_id,
                        action_type="refund",
                        payload=payload,
                        payload_hash=payload_hash,
                        status="executed",
                        rationale="Seeded historical refund.",
                    )
                )
                approval_id = f"apv_seed_{spec.id}"
                session.add(
                    Approval(
                        id=approval_id,
                        proposal_id=proposal_id,
                        run_id=run_id,
                        bound_payload_hash=payload_hash,
                        decision="approved",
                        reviewer_id="rev_kiran",
                        expires_at=now - timedelta(days=29),
                        decided_at=now - timedelta(days=30),
                        consumed_at=now - timedelta(days=30),
                    )
                )
                session.flush()
                session.add(
                    RefundOperation(
                        id=f"rfo_seed_{spec.id}",
                        order_id=spec.id,
                        proposal_id=proposal_id,
                        approval_id=approval_id,
                        run_id=run_id,
                        amount_minor=spec.prior_refund_minor,
                        currency=CURRENCY,
                        idempotency_key=key,
                        request_payload_hash=stable_hash({"seed": spec.id}),
                        provider_reference=f"prv_rf_seed_{spec.id}",
                        provider_status="succeeded",
                        status="succeeded",
                        attempt_count=1,
                        settled_at=now - timedelta(days=30),
                    )
                )

    with session_scope() as session:
        counts = {
            "customers": session.query(Customer).count(),
            "orders": session.query(Order).count(),
            "returns": session.query(Return).count(),
            "payments": session.query(Payment).count(),
            "shipments": session.query(Shipment).count(),
            "tokens": session.query(AuthToken).count(),
            "refund_operations": session.query(RefundOperation).count(),
        }
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic support data.")
    parser.add_argument("--keep", action="store_true", help="do not wipe existing rows first")
    args = parser.parse_args()

    Base.metadata.create_all(engine)
    if not args.keep:
        wipe()
    counts = seed()
    print("Seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("\nCustomer tokens:  tok_cust_priya | tok_cust_arjun | tok_cust_meera | tok_cust_dev")
    print("Reviewer token:   tok_rev_kiran (read+decide), tok_rev_readonly (read only)")


if __name__ == "__main__":
    main()
