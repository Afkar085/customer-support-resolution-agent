"""Deterministic rules, money arithmetic and proposal identity."""
from __future__ import annotations

import pytest

from app.services.common import format_money, idempotency_key, stable_hash, to_minor
from app.services.eligibility import evaluate_refund, validate_proposed_amount
from app.services.errors import ErrorCode
from app.services.proposals import proposal_payload_hash
from app.services.refunds import refundable_balance


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
def test_money_is_integer_minor_units():
    assert to_minor(2499.00) == 249900
    assert to_minor(0.01) == 1
    assert to_minor(19.99) == 1999  # no float drift


@pytest.mark.parametrize(
    ("minor", "expected"),
    [(249900, "₹2,499.00"), (5, "₹0.05"), (100, "₹1.00"), (0, "₹0.00")],
)
def test_money_formatting(minor, expected):
    assert format_money(minor, "INR") == expected


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("order_id", "eligible", "code"),
    [
        ("ord_eligible_basic", True, None),
        ("ord_eligible_highvalue", True, None),
        ("ord_eligible_cancelled", True, None),
        ("ord_return_inspection_pending", False, ErrorCode.INSPECTION_NOT_PASSED),
        ("ord_return_in_transit", False, ErrorCode.RETURN_NOT_RECEIVED),
        ("ord_no_return", False, ErrorCode.NO_RETURN_ON_RECORD),
        ("ord_window_expired", False, ErrorCode.RETURN_WINDOW_EXPIRED),
        ("ord_inspection_failed", False, ErrorCode.INSPECTION_FAILED),
        ("ord_already_refunded", False, ErrorCode.ALREADY_FULLY_REFUNDED),
    ],
)
def test_refund_eligibility(session, order_id, eligible, code):
    decision = evaluate_refund(session, order_id)
    assert decision.eligible is eligible
    if code:
        assert code in decision.blocking_codes


def test_failed_inspection_escalates_rather_than_refusing(session):
    decision = evaluate_refund(session, "ord_inspection_failed")
    assert decision.escalate is True


def test_refund_amount_cannot_exceed_balance(session):
    balance = refundable_balance(session, "ord_eligible_basic")
    ok, _ = validate_proposed_amount(session, "ord_eligible_basic", balance.available_minor)
    assert ok
    ok, problems = validate_proposed_amount(
        session, "ord_eligible_basic", balance.available_minor + 1
    )
    assert not ok
    assert problems[0].code == ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE


@pytest.mark.parametrize("amount", [0, -1, -249900])
def test_refund_amount_must_be_positive(session, amount):
    ok, _ = validate_proposed_amount(session, "ord_eligible_basic", amount)
    assert not ok


def test_already_refunded_order_has_no_balance(session):
    balance = refundable_balance(session, "ord_already_refunded")
    assert balance.committed_minor == balance.captured_minor
    assert balance.available_minor == 0


# --------------------------------------------------------------------------- #
# Proposal identity
# --------------------------------------------------------------------------- #
def _payload(**overrides):
    base = {
        "action": "request_refund_approval",
        "order_id": "ord_eligible_basic",
        "amount_minor": 249900,
        "currency": "INR",
        "ticket_id": "tkt_1",
    }
    base.update(overrides)
    return base


def test_payload_hash_is_stable_across_key_order():
    a = proposal_payload_hash(_payload())
    reordered = dict(reversed(list(_payload().items())))
    assert proposal_payload_hash(reordered) == a


def test_payload_hash_changes_with_material_fields():
    base = proposal_payload_hash(_payload())
    assert proposal_payload_hash(_payload(amount_minor=249901)) != base
    assert proposal_payload_hash(_payload(order_id="ord_other")) != base
    assert proposal_payload_hash(_payload(currency="USD")) != base


def test_payload_hash_ignores_cosmetic_fields():
    """Rewording a rationale must not invalidate a reviewer's approval."""
    base = proposal_payload_hash(_payload())
    assert proposal_payload_hash(_payload(reason="reworded", note="x")) == base


def test_idempotency_key_is_derived_not_random():
    key_a = idempotency_key("prp_1", stable_hash({"a": 1}))
    key_b = idempotency_key("prp_1", stable_hash({"a": 1}))
    assert key_a == key_b
    assert key_a != idempotency_key("prp_2", stable_hash({"a": 1}))
