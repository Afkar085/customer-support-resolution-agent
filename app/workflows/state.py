"""Workflow state.

Flat, JSON-serialisable and small enough to read in a terminal. Everything
here can be reconstructed from the database; the state is a working set, not a
record of truth. Money never lives only here.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Reducer for the fan-out: investigation and policy nodes write disjoint
    keys concurrently and both land in the same dict."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class SupportState(TypedDict, total=False):
    # identity and routing -- set by the server, never by a model
    run_id: str
    ticket_id: str
    thread_id: str
    authenticated_customer_id: str
    auth_token: str

    customer_message: str
    extra_customer_messages: Annotated[list[str], operator.add]
    order_id: str | None
    complaint_category: str
    classification: dict[str, Any]

    # specialist outputs
    verified_facts: Annotated[dict[str, Any], _merge_dicts]
    evidence_refs: list[str]
    investigation: dict[str, Any]
    policy_references: list[str]
    policy: dict[str, Any]
    damage_intake: dict[str, Any]

    missing_information: list[str]
    proposal: dict[str, Any]
    validation: dict[str, Any]
    route: str

    proposed_action_id: str | None
    approval_id: str | None
    approval_decision: str | None
    reviewer_id: str | None

    execution_status: str | None
    external_result_reference: str | None
    refund_operation_id: str | None

    retry_counts: dict[str, int]
    error_details: dict[str, Any] | None
    escalation_reason: str | None
    final_response: str | None
    outcome: str | None
    steps: Annotated[list[str], operator.add]


def initial_state(
    *,
    run_id: str,
    ticket_id: str,
    thread_id: str,
    customer_id: str,
    auth_token: str,
    customer_message: str,
    order_id: str | None,
) -> SupportState:
    return SupportState(
        run_id=run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        authenticated_customer_id=customer_id,
        auth_token=auth_token,
        customer_message=customer_message,
        extra_customer_messages=[],
        order_id=order_id,
        verified_facts={},
        evidence_refs=[],
        policy_references=[],
        missing_information=[],
        retry_counts={},
        steps=[],
    )
