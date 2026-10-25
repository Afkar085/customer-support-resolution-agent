"""Action proposals.

A proposal is an immutable record of exactly what was put in front of a
reviewer. Creating one must never move money, and creating one twice (because
an interrupted LangGraph node re-runs from its start) must not produce two.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ActionProposal, Approval, WorkflowRun
from app.services import audit
from app.services.common import iso, stable_hash


def proposal_payload_hash(payload: dict[str, Any]) -> str:
    """Hash only the fields that make the action what it is.

    A reviewer approves "refund 2499.00 on ord_1042". Rewording the rationale
    must not invalidate that approval; changing the amount must.
    """
    material = {
        "action": payload.get("action"),
        "order_id": payload.get("order_id"),
        "amount_minor": payload.get("amount_minor"),
        "currency": payload.get("currency"),
        "ticket_id": payload.get("ticket_id"),
    }
    return stable_hash(material)


def create_or_get(
    session: Session,
    *,
    run: WorkflowRun,
    action_type: str,
    payload: dict[str, Any],
    evidence_refs: list[str] | None = None,
    policy_refs: list[str] | None = None,
    rationale: str = "",
) -> tuple[ActionProposal, bool]:
    """Returns (proposal, created)."""
    payload = dict(payload)
    payload.setdefault("ticket_id", run.ticket_id)
    payload_hash = proposal_payload_hash(payload)

    existing = session.scalar(
        select(ActionProposal).where(
            ActionProposal.run_id == run.id, ActionProposal.payload_hash == payload_hash
        )
    )
    if existing is not None:
        return existing, False

    proposal = ActionProposal(
        run_id=run.id,
        ticket_id=run.ticket_id,
        action_type=action_type,
        payload=payload,
        payload_hash=payload_hash,
        evidence_refs=list(evidence_refs or []),
        policy_refs=list(policy_refs or []),
        rationale=rationale,
        status="pending_approval",
    )
    session.add(proposal)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        found = session.scalar(
            select(ActionProposal).where(
                ActionProposal.run_id == run.id, ActionProposal.payload_hash == payload_hash
            )
        )
        if found is None:
            raise
        return found, False

    audit.record(
        session,
        actor="agent:resolution",
        event_type="proposal.created",
        run_id=run.id,
        ticket_id=run.ticket_id,
        message=f"Proposed {action_type}.",
        payload={
            "proposal_id": proposal.id,
            "payload": payload,
            "payload_hash": payload_hash,
            "policy_refs": proposal.policy_refs,
            "evidence_refs": proposal.evidence_refs,
        },
    )
    return proposal, True


def invalidate_superseded(session: Session, run_id: str, keep_proposal_id: str) -> int:
    """Material details changed -> the old proposal and its approval are dead."""
    stale = session.scalars(
        select(ActionProposal).where(
            ActionProposal.run_id == run_id,
            ActionProposal.id != keep_proposal_id,
            ActionProposal.status.in_(("pending_approval", "approved")),
        )
    ).all()
    for proposal in stale:
        proposal.status = "invalidated"
        proposal.superseded_by = keep_proposal_id
        approval = session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
        if approval is not None and approval.decision in ("pending", "approved"):
            approval.decision = "expired"
            approval.reviewer_note = "Superseded: the proposed action changed."
        audit.record(
            session,
            actor="system:validator",
            event_type="proposal.invalidated",
            run_id=run_id,
            severity="warning",
            message="Proposal superseded because material details changed.",
            payload={"proposal_id": proposal.id, "superseded_by": keep_proposal_id},
        )
    return len(stale)


def view(session: Session, proposal: ActionProposal) -> dict[str, Any]:
    approval = session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    return {
        "proposal_id": proposal.id,
        "run_id": proposal.run_id,
        "ticket_id": proposal.ticket_id,
        "action_type": proposal.action_type,
        "payload": proposal.payload,
        "payload_hash": proposal.payload_hash,
        "evidence_refs": proposal.evidence_refs,
        "policy_refs": proposal.policy_refs,
        "rationale": proposal.rationale,
        "status": proposal.status,
        "created_at": iso(proposal.created_at),
        "approval": None
        if approval is None
        else {
            "approval_id": approval.id,
            "decision": approval.decision,
            "reviewer_id": approval.reviewer_id,
            "expires_at": iso(approval.expires_at),
            "decided_at": iso(approval.decided_at),
        },
    }
