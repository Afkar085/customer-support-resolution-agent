"""The scenarios that decide whether this system is trustworthy.

Each test ends with an assertion about the refund ledger, not about wording.
"""
from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import Approval, RefundOperation, utcnow
from app.services import refunds
from app.workflows import runner
from mock_services.payments import provider as payment_provider
from tests.conftest import CUSTOMER_TOKEN, REVIEWER_TOKEN


def _start_refund(client, auth, order_id="ord_eligible_basic", token=CUSTOMER_TOKEN):
    ticket = client.post(
        "/tickets",
        json={
            "customer_message": "I returned the item, it was received and inspected. Refund please.",
            "order_id": order_id,
        },
        headers=auth(token),
    ).json()
    run = client.post(
        f"/tickets/{ticket['ticket_id']}/runs", json={}, headers=auth(token)
    ).json()
    assert run["status"] == "awaiting_approval", run
    return run


def _approve(client, auth, run):
    return client.post(
        f"/approvals/{run['awaiting']['approval_id']}/decision",
        json={"decision": "approved", "note": "verified"},
        headers=auth(REVIEWER_TOKEN),
    ).json()


def _ops(order_id: str) -> list[dict]:
    with session_scope() as db:
        return refunds.operations_for_order(db, order_id)


def _provider_records(order_id: str) -> list[dict]:
    with session_scope() as db:
        from app.db.models import Payment

        payment = db.scalar(select(Payment).where(Payment.order_id == order_id))
    return payment_provider.list_refunds(payment.provider_payment_id)


# --------------------------------------------------------------------------- #
# Uncertain provider outcomes
# --------------------------------------------------------------------------- #
def test_accepted_then_timed_out_refund_is_reconciled_not_retried(client, auth):
    """The hard case: the provider took the money out, we never heard back."""
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "accept_then_timeout", 1)
    result = _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert len(operations) == 1, "a lost response must not create a second operation"
    assert operations[0]["status"] == "succeeded"
    assert operations[0]["attempt_count"] == 1
    assert operations[0]["reconcile_attempts"] == 1
    assert len(_provider_records("ord_eligible_basic")) == 1
    assert result["run"]["outcome"] == "refund_succeeded"


def test_crash_before_reconciliation_recovers_without_a_second_refund(client, auth):
    """Simulates the process dying while the operation is still `unknown`."""
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "accept_then_timeout", 1)
    _approve(client, auth, run)

    # Rewind to the state a crash would have left behind.
    with session_scope() as db:
        operation = db.scalar(
            select(RefundOperation).where(RefundOperation.order_id == "ord_eligible_basic")
        )
        operation.status = "unknown"
        operation.settled_at = None
        operation.reconcile_attempts = 0
        operation_id = operation.id

    summary = runner.recover_unfinished()
    assert any(op["operation_id"] == operation_id for op in summary["reconciled_operations"])

    operations = _ops("ord_eligible_basic")
    assert len(operations) == 1
    assert operations[0]["status"] == "succeeded"
    assert len(_provider_records("ord_eligible_basic")) == 1


def test_timeout_without_acceptance_leaves_no_money_moved(client, auth):
    """The request never landed: reconciliation finds nothing and says so."""
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "timeout", 1)
    _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert len(operations) == 1
    assert operations[0]["status"] in ("reserved", "failed")
    assert operations[0]["provider_reference"] is None
    assert _provider_records("ord_eligible_basic") == []


def test_provider_unavailable_is_retryable_with_the_same_key(client, auth):
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "unavailable", 1)
    _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert len(operations) == 1
    first_key = operations[0]["idempotency_key"]
    assert operations[0]["status"] == "reserved"

    # Retrying uses the same durable operation, not a new one.
    refunds.execute_operation(operations[0]["operation_id"])
    after = _ops("ord_eligible_basic")
    assert len(after) == 1
    assert after[0]["idempotency_key"] == first_key
    assert after[0]["status"] == "succeeded"
    assert len(_provider_records("ord_eligible_basic")) == 1


def test_provider_rejection_is_reported_as_not_completed(client, auth):
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "reject", 1)
    result = _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert operations[0]["status"] == "failed"
    assert "did not complete" in result["run"]["final_response"]


def test_pending_settlement_is_described_as_processing_not_complete(client, auth):
    run = _start_refund(client, auth)
    payment_provider.set_fault("payments", "pending", 1)
    result = _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert operations[0]["status"] == "pending_reconciliation"
    assert "processing" in result["run"]["final_response"]
    assert "completed" not in result["run"]["final_response"]

    # The provider settles later; reconciliation picks it up.
    payment_provider.settle_pending(operations[0]["idempotency_key"], succeed=True)
    refunds.reconcile_operation(operations[0]["operation_id"])
    assert _ops("ord_eligible_basic")[0]["status"] == "succeeded"


def test_double_execution_of_the_same_proposal_is_refused(client, auth):
    """An approval is single-use: replaying execution cannot pay twice."""
    run = _start_refund(client, auth)
    _approve(client, auth, run)
    proposal_id = _ops("ord_eligible_basic")[0]["proposal_id"]

    from app.auth.principal import principal_from_token
    from app.tools.registry import ToolContext, Toolset

    with session_scope() as db:
        principal = principal_from_token(db, CUSTOMER_TOKEN)
    tools = Toolset(ToolContext(principal=principal, run_id=run["run_id"]), "executor")
    replay = tools.call("execute_approved_refund", proposal_id=proposal_id)

    assert replay["success"] is False
    assert replay["error_code"] == "APPROVAL_ALREADY_CONSUMED"
    assert len(_ops("ord_eligible_basic")) == 1
    assert len(_provider_records("ord_eligible_basic")) == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_execution_produces_exactly_one_refund(client, auth):
    run = _start_refund(client, auth)
    _approve(client, auth, run)
    operation_id = _ops("ord_eligible_basic")[0]["operation_id"]

    # Force it back to a pre-send state, then hammer it from several threads.
    with session_scope() as db:
        operation = db.get(RefundOperation, operation_id)
        operation.status = "reserved"
        operation.provider_reference = None
        operation.provider_status = None
        operation.settled_at = None

    errors: list[Exception] = []

    def worker() -> None:
        try:
            refunds.execute_operation(operation_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_ops("ord_eligible_basic")) == 1
    assert len(_provider_records("ord_eligible_basic")) == 1, "idempotency key must dedupe"
    assert _ops("ord_eligible_basic")[0]["status"] == "succeeded"


def test_two_runs_on_one_order_cannot_both_reserve_the_balance(client, auth):
    first = _start_refund(client, auth)
    second = _start_refund(client, auth)
    assert first["run_id"] != second["run_id"]

    _approve(client, auth, first)
    blocked = _approve(client, auth, second)

    operations = _ops("ord_eligible_basic")
    assert len([op for op in operations if op["status"] == "succeeded"]) == 1
    total = sum(op["amount_minor"] for op in operations if op["status"] == "succeeded")
    assert total == 249900
    assert blocked["run"]["outcome"] in ("escalated", "answered")


# --------------------------------------------------------------------------- #
# Approval integrity
# --------------------------------------------------------------------------- #
def test_expired_approval_cannot_be_used(client, auth):
    run = _start_refund(client, auth)
    with session_scope() as db:
        approval = db.get(Approval, run["awaiting"]["approval_id"])
        approval.expires_at = utcnow() - timedelta(minutes=1)

    response = client.post(
        f"/approvals/{run['awaiting']['approval_id']}/decision",
        json={"decision": "approved"},
        headers=auth(REVIEWER_TOKEN),
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "APPROVAL_EXPIRED"
    assert _ops("ord_eligible_basic") == []


def test_changed_proposal_invalidates_the_approval(client, auth):
    run = _start_refund(client, auth)
    approval_id = run["awaiting"]["approval_id"]
    client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers=auth(REVIEWER_TOKEN),
    )
    # Tamper with the approved payload after the fact.
    with session_scope() as db:
        approval = db.get(Approval, approval_id)
        approval.consumed_at = None
        approval.bound_payload_hash = "0" * 64

    from app.services.approvals import validate_for_execution
    from app.services.errors import ToolError

    with session_scope() as db:
        proposal_id = _ops("ord_eligible_basic")[0]["proposal_id"]
        try:
            validate_for_execution(db, proposal_id)
            raise AssertionError("tampered approval must not validate")
        except ToolError as exc:
            assert exc.error_code == "PROPOSAL_CHANGED"


def test_approval_cannot_be_decided_twice(client, auth):
    run = _start_refund(client, auth)
    approval_id = run["awaiting"]["approval_id"]
    first = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers=auth(REVIEWER_TOKEN),
    )
    assert first.status_code == 200
    second = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "rejected"},
        headers=auth(REVIEWER_TOKEN),
    )
    assert second.status_code == 409
    assert len(_ops("ord_eligible_basic")) == 1


# --------------------------------------------------------------------------- #
# Dependent services
# --------------------------------------------------------------------------- #
def test_failed_ticket_update_does_not_undo_a_successful_refund(client, auth):
    run = _start_refund(client, auth)
    payment_provider.set_fault("ticketing", "unavailable", 10)
    result = _approve(client, auth, run)

    operations = _ops("ord_eligible_basic")
    assert operations[0]["status"] == "succeeded"
    assert result["run"]["outcome"] == "refund_succeeded"

    events = client.get(
        f"/runs/{run['run_id']}/events", headers=auth(CUSTOMER_TOKEN)
    ).json()["events"]
    assert any(event["event_type"] == "ticket.update_failed" for event in events)
    payment_provider.clear_faults()


def test_transient_carrier_failure_is_retried(client, auth):
    payment_provider.set_fault("shipping", "unavailable", 1)
    ticket = client.post(
        "/tickets",
        json={"customer_message": "Where is my order?", "order_id": "ord_in_transit"},
        headers=auth(CUSTOMER_TOKEN),
    ).json()
    run = client.post(
        f"/tickets/{ticket['ticket_id']}/runs", json={}, headers=auth(CUSTOMER_TOKEN)
    ).json()
    assert run["outcome"] == "answered"
    assert "BD4411902311" in run["final_response"]
