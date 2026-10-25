"""Human approval.

The rule this module exists to enforce: an approval message in a chat is not
authorization. Execution reads a stored approval row, bound to one proposal
payload hash, decided by an authenticated reviewer, not yet expired and not yet
consumed. Anything else refuses.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.config import get_settings
from app.db.models import ActionProposal, Approval, Customer, Order, Ticket, utcnow
from app.services import audit, proposals
from app.services.common import format_money, iso
from app.services.eligibility import evaluate_refund, validate_proposed_amount
from app.services.errors import ErrorCode, ToolError


def create_pending(session: Session, proposal: ActionProposal) -> Approval:
    """Idempotent: one approval per proposal, enforced by a unique constraint."""
    existing = session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    if existing is not None:
        return existing

    settings = get_settings()
    approval = Approval(
        proposal_id=proposal.id,
        run_id=proposal.run_id,
        bound_payload_hash=proposal.payload_hash,
        decision="pending",
        expires_at=utcnow() + timedelta(minutes=settings.approval_ttl_minutes),
    )
    session.add(approval)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        found = session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
        if found is None:
            raise
        return found

    audit.record(
        session,
        actor="system:approvals",
        event_type="approval.requested",
        run_id=proposal.run_id,
        ticket_id=proposal.ticket_id,
        message="Approval requested from a support reviewer.",
        payload={
            "approval_id": approval.id,
            "proposal_id": proposal.id,
            "expires_at": iso(approval.expires_at),
        },
    )
    return approval


def _expire_if_due(session: Session, approval: Approval) -> Approval:
    if approval.decision == "pending" and (approval.expires_at is not None):
        from app.services.common import as_aware

        expires = as_aware(approval.expires_at)
        if expires and expires <= utcnow():
            approval.decision = "expired"
            proposal = session.get(ActionProposal, approval.proposal_id)
            if proposal is not None and proposal.status == "pending_approval":
                proposal.status = "expired"
            audit.record(
                session,
                actor="system:approvals",
                event_type="approval.expired",
                run_id=approval.run_id,
                severity="warning",
                message="Approval expired before a decision was recorded.",
                payload={"approval_id": approval.id},
            )
    return approval


def decide(
    session: Session,
    principal: Principal,
    approval_id: str,
    *,
    decision: str,
    note: str | None = None,
) -> dict[str, Any]:
    reviewer_id = principal.require_reviewer()
    if decision not in ("approved", "rejected"):
        raise ToolError(ErrorCode.FORBIDDEN, "Decision must be 'approved' or 'rejected'.")

    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ToolError(ErrorCode.APPROVAL_NOT_FOUND, "Approval not found.")
    _expire_if_due(session, approval)

    if approval.decision == "expired":
        raise ToolError(
            ErrorCode.APPROVAL_EXPIRED,
            "This approval expired. A fresh proposal and approval are required.",
        )
    if approval.decision != "pending":
        raise ToolError(
            ErrorCode.APPROVAL_ALREADY_DECIDED,
            f"This approval was already {approval.decision}.",
        )

    proposal = session.get(ActionProposal, approval.proposal_id)
    if proposal is None:
        raise ToolError(ErrorCode.PROPOSAL_NOT_FOUND, "Proposal not found.")
    if proposal.payload_hash != approval.bound_payload_hash:
        raise ToolError(
            ErrorCode.PROPOSAL_CHANGED,
            "The proposal changed after this approval was created.",
        )

    approval.decision = decision
    approval.reviewer_id = reviewer_id
    approval.reviewer_note = note
    approval.decided_at = utcnow()
    proposal.status = "approved" if decision == "approved" else "rejected"

    audit.record(
        session,
        actor=f"reviewer:{reviewer_id}",
        event_type=f"approval.{decision}",
        run_id=approval.run_id,
        ticket_id=proposal.ticket_id,
        message=f"Reviewer {decision} the proposed {proposal.action_type}.",
        payload={
            "approval_id": approval.id,
            "proposal_id": proposal.id,
            "payload_hash": proposal.payload_hash,
            "note": note,
        },
    )
    return {
        "approval_id": approval.id,
        "proposal_id": proposal.id,
        "run_id": approval.run_id,
        "decision": approval.decision,
        "reviewer_id": reviewer_id,
        "decided_at": iso(approval.decided_at),
    }


def validate_for_execution(session: Session, proposal_id: str) -> Approval:
    """Re-check everything at execution time, not at approval time.

    Called after the workflow resumes. Between the reviewer clicking approve
    and this call, the refundable balance may have changed.
    """
    proposal = session.get(ActionProposal, proposal_id)
    if proposal is None:
        raise ToolError(ErrorCode.PROPOSAL_NOT_FOUND, "Proposal not found.")

    approval = session.scalar(select(Approval).where(Approval.proposal_id == proposal_id))
    if approval is None:
        raise ToolError(ErrorCode.APPROVAL_REQUIRED, "No approval exists for this proposal.")
    _expire_if_due(session, approval)

    if approval.decision == "rejected":
        raise ToolError(ErrorCode.APPROVAL_NOT_GRANTED, "The reviewer rejected this action.")
    if approval.decision == "expired":
        raise ToolError(
            ErrorCode.APPROVAL_EXPIRED, "The approval expired before the action was executed."
        )
    if approval.decision != "approved":
        raise ToolError(ErrorCode.APPROVAL_REQUIRED, "This action is still awaiting approval.")
    if approval.consumed_at is not None:
        raise ToolError(
            ErrorCode.APPROVAL_ALREADY_CONSUMED,
            "This approval has already been used to execute an action.",
        )
    if approval.bound_payload_hash != proposal.payload_hash:
        raise ToolError(
            ErrorCode.PROPOSAL_CHANGED,
            "The approved payload no longer matches the proposal.",
        )
    if proposal.status not in ("approved",):
        raise ToolError(
            ErrorCode.APPROVAL_NOT_GRANTED,
            f"Proposal is in state '{proposal.status}', not 'approved'.",
        )

    # Independent revalidation of the business rules.
    order_id = str(proposal.payload["order_id"])
    amount_minor = int(proposal.payload["amount_minor"])
    decision = evaluate_refund(session, order_id)
    if not decision.eligible:
        raise ToolError(
            ErrorCode.APPROVAL_NOT_GRANTED,
            "Eligibility no longer holds at execution time: "
            + "; ".join(b.message for b in decision.blockers),
            blockers=decision.blocking_codes,
        )
    valid_amount, problems = validate_proposed_amount(session, order_id, amount_minor)
    if not valid_amount:
        raise ToolError(
            ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE,
            "; ".join(p.message for p in problems),
        )
    return approval


def consume(session: Session, approval: Approval) -> None:
    approval.consumed_at = utcnow()
    session.flush()


# --------------------------------------------------------------------------- #
# Reviewer queue -- everything the approval screen needs in one payload
# --------------------------------------------------------------------------- #
def list_for_reviewer(
    session: Session, principal: Principal, *, include_decided: bool = False
) -> list[dict[str, Any]]:
    principal.require_reviewer("approvals:read")
    stmt = select(Approval).order_by(Approval.created_at.desc())
    if not include_decided:
        stmt = stmt.where(Approval.decision == "pending")
    rows = session.scalars(stmt).all()
    out: list[dict[str, Any]] = []
    for approval in rows:
        _expire_if_due(session, approval)
        proposal = session.get(ActionProposal, approval.proposal_id)
        if proposal is None:
            continue
        out.append(_reviewer_card(session, approval, proposal))
    return out


def _reviewer_card(
    session: Session, approval: Approval, proposal: ActionProposal
) -> dict[str, Any]:
    ticket = session.get(Ticket, proposal.ticket_id)
    order = session.get(Order, proposal.payload.get("order_id")) if proposal.payload else None
    customer = session.get(Customer, ticket.customer_id) if ticket else None
    amount = int(proposal.payload.get("amount_minor") or 0)
    currency = str(proposal.payload.get("currency") or "INR")
    return {
        "approval_id": approval.id,
        "decision": approval.decision,
        "expires_at": iso(approval.expires_at),
        "created_at": iso(approval.created_at),
        "reviewer_id": approval.reviewer_id,
        "reviewer_note": approval.reviewer_note,
        "run_id": approval.run_id,
        "proposal": proposals.view(session, proposal),
        "amount_display": format_money(amount, currency),
        "customer": None
        if customer is None
        else {"customer_id": customer.id, "name": customer.name, "tier": customer.tier},
        "order": None
        if order is None
        else {
            "order_id": order.id,
            "status": order.status,
            "paid_amount_minor": order.paid_amount_minor,
            "currency": order.currency,
            "delivered_at": iso(order.delivered_at),
        },
        "ticket": None
        if ticket is None
        else {
            "ticket_id": ticket.id,
            "category": ticket.category,
            "status": ticket.status,
            "summary": ticket.summary,
            "customer_message": ticket.customer_message,
        },
    }


def get_card(session: Session, principal: Principal, approval_id: str) -> dict[str, Any]:
    principal.require_reviewer("approvals:read")
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ToolError(ErrorCode.APPROVAL_NOT_FOUND, "Approval not found.")
    _expire_if_due(session, approval)
    proposal = session.get(ActionProposal, approval.proposal_id)
    if proposal is None:
        raise ToolError(ErrorCode.PROPOSAL_NOT_FOUND, "Proposal not found.")
    return _reviewer_card(session, approval, proposal)
