"""Business-rule validation of a model proposal.

This is the boundary the whole design turns on: above it, a language model
suggests; below it, ordinary code decides. The model's chosen action is an
input here, not a decision.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.schemas.agent import ResolutionProposal, ValidationOutcome
from app.services import eligibility as eligibility_service
from app.services.errors import ESCALATION_CODES, ToolError
from app.services.orders import load_owned_order


def validate(
    session: Session,
    principal: Principal,
    proposal: ResolutionProposal,
    *,
    category: str,
) -> ValidationOutcome:
    notes: list[str] = []

    # 1. An order referenced in a proposal must belong to the authenticated
    #    customer. The model cannot widen access by naming an id.
    if proposal.order_id:
        try:
            load_owned_order(session, principal, proposal.order_id)
        except ToolError as exc:
            return ValidationOutcome(
                route="escalate",
                blockers=[{"code": exc.error_code, "message": exc.message}],
                escalation_reason="order_access_denied",
                notes=["Proposal referenced an order outside the authenticated account."],
            )

    if proposal.action == "request_information":
        if not proposal.missing_information:
            notes.append(
                "Proposal asked for information without naming any; treated as an answer."
            )
            return ValidationOutcome(route="answer_only", notes=notes)
        return ValidationOutcome(route="request_information", notes=notes)

    if proposal.action == "escalate_to_human":
        return ValidationOutcome(
            route="escalate",
            escalation_reason=proposal.escalation_reason or proposal.reason,
            notes=notes,
        )

    if proposal.action == "answer_only":
        return ValidationOutcome(route="answer_only", notes=notes)

    # ---- request_refund_approval: everything below is re-derived ----------
    if not proposal.order_id:
        return ValidationOutcome(
            route="escalate",
            blockers=[{"code": "MISSING_ORDER", "message": "Refund proposed without an order."}],
            escalation_reason="refund_proposal_without_order",
        )

    decision = eligibility_service.evaluate_refund(session, proposal.order_id)
    if not decision.eligible:
        blockers = [b.as_dict() for b in decision.blockers]
        codes = decision.blocking_codes
        notes.append("Model proposed a refund that the deterministic check rejected.")
        if decision.escalate or any(code in ESCALATION_CODES for code in codes):
            return ValidationOutcome(
                route="escalate",
                blockers=blockers,
                escalation_reason="eligibility_requires_review",
                notes=notes,
            )
        return ValidationOutcome(route="answer_only", blockers=blockers, notes=notes)

    # The amount is ours, not the model's. If the model guessed differently we
    # keep ours and record the discrepancy.
    authoritative = decision.max_refundable_minor
    if proposal.amount_minor is not None and proposal.amount_minor != authoritative:
        notes.append(
            f"Proposed amount {proposal.amount_minor} replaced with computed refundable "
            f"amount {authoritative}."
        )
    valid, problems = eligibility_service.validate_proposed_amount(
        session, proposal.order_id, authoritative
    )
    if not valid:
        return ValidationOutcome(
            route="answer_only",
            blockers=[p.as_dict() for p in problems],
            notes=notes,
        )

    if not proposal.policy_refs:
        # A refund with no policy citation is not a refund we are willing to
        # put in front of a reviewer as "policy-backed".
        notes.append("Refund proposal had no policy citation.")
        return ValidationOutcome(
            route="escalate",
            escalation_reason="refund_without_policy_citation",
            notes=notes,
        )

    return ValidationOutcome(
        route="request_approval",
        adjusted_amount_minor=authoritative,
        notes=notes,
    )


def summarise(outcome: ValidationOutcome) -> dict[str, Any]:
    return {
        "route": outcome.route,
        "blockers": outcome.blockers,
        "adjusted_amount_minor": outcome.adjusted_amount_minor,
        "escalation_reason": outcome.escalation_reason,
        "notes": outcome.notes,
    }
