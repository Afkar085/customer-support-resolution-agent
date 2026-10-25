r"""The LangGraph workflow.

    authenticate -> classify -> investigate -+-> propose -> validate -> route
                             \-> policy -----/                            |
                                                                          |
    answer_only ------------------------------------------------------+   |
    request_information ----------------------------------------------+   |
    escalate ---------------------------------------------------------+   |
    request_approval -> [interrupt] -> revalidate -> execute -> verify-+  |
                                                                       |  |
                                                 update_ticket -> finalise

Two things about the approval pause are deliberate:

* ``request_approval`` writes the proposal and the pending approval *before*
  it interrupts, and both writes are idempotent. When the run resumes, LangGraph
  re-executes the node from its start, so a non-idempotent write here would
  create a second proposal every time a reviewer clicked approve.
* The money moves in ``execute``, a separate node after the interrupt, so a
  re-executed node can never re-issue a refund.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents import classifier
from app.agents import investigation as investigation_agent
from app.agents import policy as policy_agent
from app.agents import resolution as resolution_agent
from app.agents.llm import LLMClient, ModelUsage
from app.auth.principal import Principal, principal_from_token
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Approval, Ticket, WorkflowRun
from app.schemas.agent import (
    Classification,
    InvestigationResult,
    PolicyResult,
    ResolutionProposal,
)
from app.services import approvals as approval_service
from app.services import audit, eligibility, refunds
from app.services import tickets as ticket_service
from app.services.common import format_money
from app.services.errors import ErrorCode, ToolError
from app.tools.registry import ToolContext, Toolset
from app.workflows import validation
from app.workflows.state import SupportState

CATEGORY_TO_TICKET = {
    "order_status": "order_status",
    "refund_request": "refund_request",
    "damaged_item": "damaged_item",
    "out_of_scope": "out_of_scope",
    "unclear": "unclassified",
}


# --------------------------------------------------------------------------- #
# Per-run context carried outside the graph state (never checkpointed)
# --------------------------------------------------------------------------- #
class RunContext:
    """Holds the things that must not be serialised into a checkpoint:
    the live LLM client, usage counters and the tool context."""

    def __init__(self, usage: ModelUsage | None = None) -> None:
        self.usage = usage or ModelUsage()
        self.llm = LLMClient(self.usage)
        self.tool_calls = 0
        self.variant = "specialists"

    def toolset(self, principal: Principal, name: str, state: SupportState) -> Toolset:
        ctx = ToolContext(
            principal=principal, run_id=state.get("run_id"), ticket_id=state.get("ticket_id")
        )
        toolset = Toolset(ctx, name)
        self._pending = ctx
        return toolset

    def absorb(self, toolset: Toolset) -> None:
        self.tool_calls += toolset.ctx.tool_calls


def _principal(state: SupportState) -> Principal:
    with session_scope() as session:
        return principal_from_token(session, state.get("auth_token"))


def _full_message(state: SupportState) -> str:
    parts = [state.get("customer_message", "")]
    parts.extend(state.get("extra_customer_messages") or [])
    return "\n\n".join(part for part in parts if part)


def _set_stage(run_id: str, stage: str, *, status: str | None = None) -> None:
    with session_scope() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return
        run.stage = stage
        if status:
            run.status = status


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def make_nodes(context: RunContext) -> dict[str, Any]:
    settings = get_settings()

    def authenticate(state: SupportState) -> dict[str, Any]:
        """Identity and access, in code, before anything else happens."""
        principal = _principal(state)
        customer_id = principal.require_customer()
        if customer_id != state.get("authenticated_customer_id"):
            raise ToolError(ErrorCode.FORBIDDEN, "Token does not match the run's customer.")
        with session_scope() as session:
            ticket = ticket_service.load_owned_ticket(session, principal, state["ticket_id"])
            order_id = state.get("order_id") or ticket.order_id
            run = session.get(WorkflowRun, state["run_id"])
            if run is not None:
                run.status = "running"
                run.stage = "authenticated"
            audit.record(
                session,
                actor=f"customer:{customer_id}",
                event_type="run.authenticated",
                run_id=state["run_id"],
                ticket_id=ticket.id,
                message="Request authenticated and ticket ownership verified.",
                payload={"order_id": order_id},
            )
        return {"order_id": order_id, "steps": ["authenticate"]}

    def classify(state: SupportState) -> dict[str, Any]:
        result = classifier.classify(context.llm, _full_message(state), state.get("order_id"))
        with session_scope() as session:
            audit.record(
                session,
                actor="agent:classifier",
                event_type="run.classified",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message=f"Classified as {result.category} (confidence {result.confidence}).",
                payload=result.model_dump(),
            )
            ticket = session.get(Ticket, state["ticket_id"])
            if ticket is not None:
                ticket.category = CATEGORY_TO_TICKET.get(result.category, "unclassified")
                ticket.status = "investigating"
        _set_stage(state["run_id"], "classified")
        return {
            "complaint_category": result.category,
            "classification": result.model_dump(),
            "steps": ["classify"],
        }

    def investigate(state: SupportState) -> dict[str, Any]:
        principal = _principal(state)
        classification = Classification.model_validate(state["classification"])
        tools = context.toolset(principal, "investigation", state)
        result = investigation_agent.investigate(
            context.llm,
            tools,
            classification=classification,
            customer_message=_full_message(state),
            ticket_order_id=state.get("order_id"),
        )
        context.absorb(tools)

        intake: dict[str, Any] = {}
        if classification.category == "damaged_item" and result.order_id:
            with session_scope() as session:
                intake = eligibility.evaluate_damage_intake(
                    session, result.order_id, _collected_damage_fields(state)
                )

        with session_scope() as session:
            audit.record(
                session,
                actor="agent:investigation",
                event_type="run.investigated",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message=result.summary[:500] or "Investigation complete.",
                payload={
                    "order_id": result.order_id,
                    "evidence_refs": result.evidence_refs,
                    "missing_information": result.missing_information,
                    "tool_calls": tools.ctx.log,
                },
            )
        _set_stage(state["run_id"], "investigated")
        return {
            "order_id": result.order_id,
            "investigation": result.model_dump(),
            "evidence_refs": result.evidence_refs,
            "verified_facts": result.facts,
            "missing_information": result.missing_information,
            "damage_intake": intake,
            "steps": ["investigate"],
        }

    def retrieve_policy(state: SupportState) -> dict[str, Any]:
        """Runs after investigation: the query uses established order facts."""
        principal = _principal(state)
        tools = context.toolset(principal, "policy", state)
        result = policy_agent.retrieve(
            context.llm,
            tools,
            category=state.get("complaint_category", "unclear"),
            customer_message=_full_message(state),
            facts=state.get("verified_facts") or {},
        )
        context.absorb(tools)
        with session_scope() as session:
            audit.record(
                session,
                actor="agent:policy",
                event_type="run.policy_retrieved",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                severity="warning" if result.untrusted_content_detected else "info",
                message=result.interpretation[:500] or "Policy retrieval complete.",
                payload={
                    "citations": result.refs,
                    "conflicts": result.conflicts,
                    "untrusted_content_detected": result.untrusted_content_detected,
                },
            )
        _set_stage(state["run_id"], "policy_retrieved")
        return {
            "policy": result.model_dump(),
            "policy_references": result.refs,
            "steps": ["retrieve_policy"],
        }

    def retrieve_policy_concurrent(state: SupportState) -> dict[str, Any]:
        """Same agent, but with no order facts available yet.

        Used by the parallel graph variant so the two specialists can run in
        one superstep. Retrieval quality of this variant is measured in the
        eval report (see docs/evaluation.md).
        """
        return retrieve_policy({**state, "verified_facts": {}})

    def generalist(state: SupportState) -> dict[str, Any]:
        """Single-agent baseline: one node classifies, gathers, retrieves, proposes."""
        from app.agents import generalist as generalist_agent

        principal = _principal(state)
        tools = context.toolset(principal, "generalist", state)

        def _intake(order_id: str) -> dict[str, Any]:
            with session_scope() as session:
                return eligibility.evaluate_damage_intake(
                    session, order_id, _collected_damage_fields(state)
                )

        classification, investigation_result, policy_result, proposal, intake = (
            generalist_agent.run(
                context.llm,
                tools,
                customer_message=_full_message(state),
                ticket_order_id=state.get("order_id"),
                intake_fn=_intake,
                currency=settings.currency,
            )
        )
        context.absorb(tools)

        with session_scope() as session:
            ticket = session.get(Ticket, state["ticket_id"])
            if ticket is not None:
                ticket.category = CATEGORY_TO_TICKET.get(classification.category, "unclassified")
                ticket.status = "investigating"
            audit.record(
                session,
                actor="agent:generalist",
                event_type="run.generalist_pass",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message=f"Single-agent pass classified this as {classification.category}.",
                payload={
                    "classification": classification.model_dump(),
                    "evidence_refs": investigation_result.evidence_refs,
                    "citations": policy_result.refs,
                    "tool_calls": tools.ctx.log,
                },
            )
        _set_stage(state["run_id"], "proposed")
        return {
            "complaint_category": classification.category,
            "classification": classification.model_dump(),
            "order_id": investigation_result.order_id,
            "investigation": investigation_result.model_dump(),
            "evidence_refs": investigation_result.evidence_refs,
            "verified_facts": investigation_result.facts,
            "missing_information": investigation_result.missing_information,
            "damage_intake": intake,
            "policy": policy_result.model_dump(),
            "policy_references": policy_result.refs,
            "proposal": proposal.model_dump(),
            "steps": ["generalist"],
        }

    def propose(state: SupportState) -> dict[str, Any]:
        classification = Classification.model_validate(state["classification"])
        investigation_result = InvestigationResult.model_validate(state["investigation"])
        policy_result = PolicyResult.model_validate(state.get("policy") or {})
        currency = (
            (state.get("verified_facts", {}).get("order") or {}).get("currency")
            or settings.currency
        )
        proposal = resolution_agent.propose(
            context.llm,
            classification=classification,
            investigation=investigation_result,
            policy=policy_result,
            intake=state.get("damage_intake"),
            currency=currency,
        )
        _set_stage(state["run_id"], "proposed")
        return {"proposal": proposal.model_dump(), "steps": ["propose"]}

    def validate_rules(state: SupportState) -> dict[str, Any]:
        principal = _principal(state)
        proposal = ResolutionProposal.model_validate(state["proposal"])
        with session_scope() as session:
            outcome = validation.validate(
                session,
                principal,
                proposal,
                category=state.get("complaint_category", "unclear"),
            )
            audit.record(
                session,
                actor="system:validator",
                event_type="run.validated",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                severity="warning" if outcome.notes else "info",
                message=f"Proposal routed to '{outcome.route}'.",
                payload=validation.summarise(outcome),
            )
        _set_stage(state["run_id"], f"validated:{outcome.route}")
        return {
            "validation": validation.summarise(outcome),
            "route": outcome.route,
            "escalation_reason": outcome.escalation_reason,
            "steps": ["validate_rules"],
        }

    # ---- terminal branches ------------------------------------------------
    def answer_only(state: SupportState) -> dict[str, Any]:
        proposal = ResolutionProposal.model_validate(state["proposal"])
        message = _message_for_blocked(state, proposal)
        _ticket_update(
            state,
            status="resolved",
            note=f"Answered without an action: {proposal.reason}",
            summary=proposal.reason[:180],
        )
        return {
            "final_response": message,
            "outcome": "answered",
            "execution_status": "none",
            "steps": ["answer_only"],
        }

    def request_information(state: SupportState) -> dict[str, Any]:
        proposal = ResolutionProposal.model_validate(state["proposal"])
        _ticket_update(
            state,
            status="awaiting_customer",
            note="Asked the customer for: " + "; ".join(proposal.missing_information),
            summary=proposal.reason[:180],
        )
        return {
            "final_response": proposal.customer_message,
            "missing_information": proposal.missing_information,
            "outcome": "clarification_requested",
            "execution_status": "none",
            "steps": ["request_information"],
        }

    def escalate(state: SupportState) -> dict[str, Any]:
        proposal = ResolutionProposal.model_validate(state["proposal"])
        reason = state.get("escalation_reason") or proposal.escalation_reason or proposal.reason
        _ticket_update(
            state,
            status="escalated",
            note=f"Escalated to a support reviewer: {reason}",
            summary=proposal.reason[:180],
        )
        with session_scope() as session:
            audit.record(
                session,
                actor="system:router",
                event_type="run.escalated",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                severity="warning",
                message=f"Escalated: {reason}",
                payload={"validation": state.get("validation")},
            )
        return {
            "final_response": proposal.customer_message,
            "outcome": "escalated",
            "escalation_reason": reason,
            "execution_status": "none",
            "steps": ["escalate"],
        }

    # ---- approval pause ---------------------------------------------------
    def request_approval(state: SupportState) -> dict[str, Any]:
        """Writes the proposal and the pending approval, then pauses.

        Everything before the interrupt is idempotent, because LangGraph
        re-runs this node from the top when the thread resumes.
        """
        principal = _principal(state)
        proposal = ResolutionProposal.model_validate(state["proposal"])
        amount = int(
            state.get("validation", {}).get("adjusted_amount_minor") or proposal.amount_minor or 0
        )
        tools = context.toolset(principal, "resolution", state)
        created = tools.call(
            "create_refund_proposal",
            order_id=proposal.order_id,
            amount_minor=amount,
            reason=proposal.reason,
            evidence_refs=proposal.evidence_refs,
            policy_refs=proposal.policy_refs,
        )
        context.absorb(tools)
        if not created.get("success"):
            # Eligibility moved under us between validation and here.
            return {
                "route": "escalate",
                "escalation_reason": created.get("error_code"),
                "error_details": created,
                "final_response": (
                    "I could not complete the refund request automatically, so I have passed "
                    "the case to a support reviewer. They will contact you."
                ),
                "outcome": "escalated",
                "steps": ["request_approval:blocked"],
            }

        proposal_id = created["proposal_id"]
        approval_id = created["approval_id"]
        _set_stage(state["run_id"], "awaiting_approval", status="awaiting_approval")
        with session_scope() as session:
            ticket = session.get(Ticket, state["ticket_id"])
            if ticket is not None:
                ticket.status = "awaiting_approval"

        # ---- the pause ----------------------------------------------------
        decision = interrupt(
            {
                "type": "refund_approval",
                "proposal_id": proposal_id,
                "approval_id": approval_id,
                "order_id": proposal.order_id,
                "amount_minor": amount,
                "amount_display": format_money(amount, proposal.currency or settings.currency),
                "currency": proposal.currency or settings.currency,
                "reason": proposal.reason,
                "evidence_refs": proposal.evidence_refs,
                "policy_refs": proposal.policy_refs,
                "customer_message_preview": proposal.customer_message,
            }
        )
        # Resumed. The payload only unblocks us -- the decision of record is
        # the row in the approvals table, which the next node re-reads.
        return {
            "proposed_action_id": proposal_id,
            "approval_id": approval_id,
            "approval_decision": (decision or {}).get("decision") if isinstance(decision, dict) else None,
            "reviewer_id": (decision or {}).get("reviewer_id") if isinstance(decision, dict) else None,
            "steps": ["request_approval:resumed"],
        }

    def revalidate(state: SupportState) -> dict[str, Any]:
        """Re-check the stored approval and the business rules after the pause."""
        proposal_id = state.get("proposed_action_id")
        if not proposal_id:
            return {"route": "escalate", "escalation_reason": "missing_proposal", "steps": ["revalidate"]}
        with session_scope() as session:
            try:
                approval_service.validate_for_execution(session, proposal_id)
            except ToolError as exc:
                audit.record(
                    session,
                    actor="system:validator",
                    event_type="approval.revalidation_failed",
                    run_id=state["run_id"],
                    ticket_id=state["ticket_id"],
                    severity="warning",
                    message=exc.message,
                    payload={"error_code": exc.error_code, "proposal_id": proposal_id},
                )
                return {
                    "route": "rejected"
                    if exc.error_code
                    in (ErrorCode.APPROVAL_NOT_GRANTED, ErrorCode.APPROVAL_EXPIRED)
                    else "escalate",
                    "error_details": exc.as_dict(),
                    "escalation_reason": exc.error_code,
                    "steps": ["revalidate:blocked"],
                }
            audit.record(
                session,
                actor="system:validator",
                event_type="approval.revalidated",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message="Approval and eligibility revalidated at execution time.",
                payload={"proposal_id": proposal_id},
            )
        _set_stage(state["run_id"], "revalidated", status="running")
        return {"route": "execute", "steps": ["revalidate"]}

    def execute(state: SupportState) -> dict[str, Any]:
        """The only node that moves money."""
        principal = _principal(state)
        tools = context.toolset(principal, "executor", state)
        result = tools.call("execute_approved_refund", proposal_id=state["proposed_action_id"])
        context.absorb(tools)
        if not result.get("success"):
            return {
                "execution_status": "failed",
                "error_details": result,
                "route": "escalate",
                "escalation_reason": result.get("error_code"),
                "steps": ["execute:failed"],
            }
        _set_stage(state["run_id"], "executed")
        return {
            "refund_operation_id": result["operation_id"],
            "execution_status": result["status"],
            "external_result_reference": result.get("provider_reference"),
            "steps": ["execute"],
        }

    def verify(state: SupportState) -> dict[str, Any]:
        """Confirm the external result before claiming anything to the customer."""
        operation_id = state.get("refund_operation_id")
        if not operation_id:
            return {"steps": ["verify:skipped"]}
        with session_scope() as session:
            current = refunds.get_operation(session, operation_id)
        if current["status"] in ("unknown", "pending_reconciliation", "submitted"):
            current = refunds.reconcile_operation(operation_id)
        with session_scope() as session:
            audit.record(
                session,
                actor="system:verifier",
                event_type="refund.verified",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message=f"Verified external refund status: {current['status']}.",
                payload={
                    "operation_id": operation_id,
                    "status": current["status"],
                    "provider_reference": current.get("provider_reference"),
                },
            )
        _set_stage(state["run_id"], "verified")
        return {
            "execution_status": current["status"],
            "external_result_reference": current.get("provider_reference"),
            "steps": ["verify"],
        }

    def update_ticket_node(state: SupportState) -> dict[str, Any]:
        status_text = refunds.customer_facing_status(state.get("execution_status") or "created")
        retry_counts = dict(state.get("retry_counts") or {})
        try:
            _ticket_update(
                state,
                status="resolved" if state.get("execution_status") == "succeeded" else "investigating",
                note=f"Refund {status_text}. Operation {state.get('refund_operation_id')}.",
                summary="Refund issued after reviewer approval",
            )
        except ToolError as exc:
            # The refund already happened. Only the ticket update is retried.
            retry_counts["update_ticket"] = retry_counts.get("update_ticket", 0) + 1
            with session_scope() as session:
                audit.record(
                    session,
                    actor="system:ticketing",
                    event_type="ticket.update_failed",
                    run_id=state["run_id"],
                    ticket_id=state["ticket_id"],
                    severity="warning",
                    message=f"Ticket update failed after a successful refund: {exc.message}",
                    payload={"error_code": exc.error_code, "attempts": retry_counts["update_ticket"]},
                )
            return {"retry_counts": retry_counts, "steps": ["update_ticket:failed"]}
        return {"retry_counts": retry_counts, "steps": ["update_ticket"]}

    def rejected(state: SupportState) -> dict[str, Any]:
        details = state.get("error_details") or {}
        expired = details.get("error_code") == ErrorCode.APPROVAL_EXPIRED
        with session_scope() as session:
            approval_id = state.get("approval_id")
            approval = session.get(Approval, approval_id) if approval_id else None
            note = approval.reviewer_note if approval else None
        if expired:
            message = (
                "Your refund request was prepared but the approval window closed before a "
                "reviewer could action it. I have left the case open and a reviewer will "
                "pick it up again -- nothing has been charged or refunded in the meantime."
            )
            outcome = "approval_expired"
        else:
            message = (
                "A support reviewer has looked at your refund request and was not able to "
                "approve it."
                + (f" Their note: {note}" if note else "")
                + " If you think that is wrong, reply here and it will go back to the team."
            )
            outcome = "rejected"
        _ticket_update(
            state,
            status="rejected" if not expired else "awaiting_approval",
            note=f"Refund not issued ({outcome}).",
        )
        return {
            "final_response": message,
            "outcome": outcome,
            "execution_status": "none",
            "steps": ["rejected"],
        }

    def finalise(state: SupportState) -> dict[str, Any]:
        """Compose a customer update that matches the actual recorded state."""
        proposal_data = state.get("proposal") or {}
        message = state.get("final_response")
        execution_status = state.get("execution_status")
        operation_id = state.get("refund_operation_id")

        if operation_id and execution_status:
            amount = int(state.get("validation", {}).get("adjusted_amount_minor") or 0)
            currency = proposal_data.get("currency") or settings.currency
            if execution_status == "succeeded":
                message = (
                    f"Approved. A refund of {format_money(amount, currency)} has been completed "
                    "by your payment provider and will appear on the original payment method "
                    "within 5 to 7 working days."
                )
                outcome = "refund_succeeded"
            elif execution_status in ("pending_reconciliation", "submitted"):
                message = (
                    f"Approved. Your refund of {format_money(amount, currency)} has been "
                    "accepted by the payment provider and is still processing. I will confirm "
                    "once it settles."
                )
                outcome = "refund_processing"
            elif execution_status == "unknown":
                message = (
                    "Your refund was submitted but I could not confirm the outcome yet. The "
                    "case is being checked and no second refund will be created in the "
                    "meantime."
                )
                outcome = "refund_unknown"
            else:
                message = (
                    "Your refund was approved but the payment provider did not complete it. "
                    "A support reviewer is looking at it now and no money has left or entered "
                    "your account."
                )
                outcome = "refund_failed"
        else:
            outcome = state.get("outcome") or "answered"

        if not message:
            message = "Your request has been recorded and a support reviewer will follow up."

        with session_scope() as session:
            run = session.get(WorkflowRun, state["run_id"])
            if run is not None:
                run.final_response = message
                run.outcome = outcome
                run.stage = "finalised"
                run.status = {
                    "clarification_requested": "awaiting_customer",
                    "escalated": "escalated",
                }.get(outcome, "completed")
                run.completed_at = _now()
                # Counters are accumulated by the runner across every leg of
                # the run; a resumed leg must not overwrite the first leg's.
                run.snapshot = {
                    "category": state.get("complaint_category"),
                    "route": state.get("route"),
                    "policy_references": state.get("policy_references"),
                    "evidence_refs": state.get("evidence_refs"),
                    "execution_status": execution_status,
                    "missing_information": state.get("missing_information") or [],
                    "refund_operation_id": operation_id,
                    "model_mode": context.usage.mode,
                    "variant": context.variant,
                    "usage": context.usage.as_dict(),
                }
            audit.record(
                session,
                actor="system:workflow",
                event_type="run.completed",
                run_id=state["run_id"],
                ticket_id=state["ticket_id"],
                message=f"Run finished with outcome '{outcome}'.",
                payload={"outcome": outcome, "execution_status": execution_status},
            )
        return {"final_response": message, "outcome": outcome, "steps": ["finalise"]}

    return {
        "authenticate": authenticate,
        "classify": classify,
        "investigate": investigate,
        "generalist": generalist,
        "retrieve_policy": retrieve_policy,
        "retrieve_policy_concurrent": retrieve_policy_concurrent,
        "propose": propose,
        "validate_rules": validate_rules,
        "answer_only": answer_only,
        "request_information": request_information,
        "escalate": escalate,
        "request_approval": request_approval,
        "revalidate": revalidate,
        "execute": execute,
        "verify": verify,
        "update_ticket": update_ticket_node,
        "rejected": rejected,
        "finalise": finalise,
    }


# --------------------------------------------------------------------------- #
# helpers used by the nodes
# --------------------------------------------------------------------------- #
def _now():
    from app.services.common import utcnow

    return utcnow()


def _collected_damage_fields(state: SupportState) -> dict[str, Any]:
    """Damaged-item intake fields gathered from the customer's own messages."""
    text = " ".join(
        [state.get("customer_message", ""), *(state.get("extra_customer_messages") or [])]
    ).lower()
    has_photo = any(
        token in text
        for token in ("photo", "photos", "picture", "pictures", "image", "attached", "attachment")
    )
    descriptive = any(
        token in text
        for token in (
            "cracked",
            "broken",
            "shattered",
            "dented",
            "scratched",
            "bent",
            "broke",
            "stopped working",
            "not working",
            "does not work",
            "doesn't work",
            "missing",
            "torn",
            "leaking",
            "swollen",
        )
    )
    return {
        "damage_description": descriptive,
        "photo_provided": has_photo,
    }


def _message_for_blocked(state: SupportState, proposal: ResolutionProposal) -> str:
    """If validation overrode the model's action, the customer must hear the
    validated reason, not the model's original optimistic wording."""
    validation_data = state.get("validation") or {}
    blockers = validation_data.get("blockers") or []
    if validation_data.get("notes") and blockers:
        reasons = " ".join(b.get("message", "") for b in blockers)
        return f"I have checked this against our refund policy. {reasons}".strip()
    return proposal.customer_message


def _ticket_update(
    state: SupportState,
    *,
    status: str | None = None,
    note: str | None = None,
    summary: str | None = None,
) -> None:
    ticket_service.update_through_api(
        ticket_id=state["ticket_id"],
        status=status,
        note=note,
        summary=summary,
        actor="agent",
    )


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
def _route_after_validation(state: SupportState) -> str:
    return {
        "answer_only": "answer_only",
        "request_information": "request_information",
        "escalate": "escalate",
        "request_approval": "request_approval",
    }.get(state.get("route", "escalate"), "escalate")


def _route_after_approval(state: SupportState) -> str:
    if state.get("route") == "escalate":
        return "escalate"
    return "revalidate"


def _route_after_revalidation(state: SupportState) -> str:
    route = state.get("route")
    if route == "execute":
        return "execute"
    if route == "rejected":
        return "rejected"
    return "escalate"


def _route_after_execute(state: SupportState) -> str:
    if state.get("route") == "escalate":
        return "escalate"
    return "verify"


VARIANTS = ("specialists", "parallel", "baseline")


def build_graph(context: RunContext, *, variant: str = "specialists"):
    """Three shapes over the same nodes, so the design can be measured.

    specialists -- investigation, then policy retrieval that uses its facts
    parallel    -- both specialists in one superstep, policy without facts
    baseline    -- one generalist node holding every read tool
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown graph variant: {variant}")
    nodes = make_nodes(context)
    graph = StateGraph(SupportState)

    for name in (
        "authenticate",
        "validate_rules",
        "answer_only",
        "request_information",
        "escalate",
        "request_approval",
        "revalidate",
        "execute",
        "verify",
        "update_ticket",
        "rejected",
        "finalise",
    ):
        graph.add_node(name, nodes[name])

    graph.add_edge(START, "authenticate")

    if variant == "baseline":
        graph.add_node("generalist", nodes["generalist"])
        graph.add_edge("authenticate", "generalist")
        graph.add_edge("generalist", "validate_rules")
    else:
        graph.add_node("classify", nodes["classify"])
        graph.add_node("investigate", nodes["investigate"])
        graph.add_node("propose", nodes["propose"])
        graph.add_node(
            "retrieve_policy",
            nodes["retrieve_policy_concurrent"]
            if variant == "parallel"
            else nodes["retrieve_policy"],
        )
        graph.add_edge("authenticate", "classify")
        if variant == "parallel":
            # One superstep, two specialists. Policy retrieval cannot use the
            # order facts here, because they do not exist yet.
            graph.add_edge("classify", "investigate")
            graph.add_edge("classify", "retrieve_policy")
            graph.add_edge("investigate", "propose")
            graph.add_edge("retrieve_policy", "propose")
        else:
            graph.add_edge("classify", "investigate")
            graph.add_edge("investigate", "retrieve_policy")
            graph.add_edge("retrieve_policy", "propose")
        graph.add_edge("propose", "validate_rules")
    graph.add_conditional_edges(
        "validate_rules",
        _route_after_validation,
        {
            "answer_only": "answer_only",
            "request_information": "request_information",
            "escalate": "escalate",
            "request_approval": "request_approval",
        },
    )
    graph.add_conditional_edges(
        "request_approval",
        _route_after_approval,
        {"revalidate": "revalidate", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "revalidate",
        _route_after_revalidation,
        {"execute": "execute", "rejected": "rejected", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "execute", _route_after_execute, {"verify": "verify", "escalate": "escalate"}
    )
    graph.add_edge("verify", "update_ticket")
    graph.add_edge("update_ticket", "finalise")

    for terminal in ("answer_only", "request_information", "escalate", "rejected"):
        graph.add_edge(terminal, "finalise")
    graph.add_edge("finalise", END)
    return graph


def compile_graph(context: RunContext, *, variant: str = "specialists"):
    from app.db.checkpointer import get_checkpointer

    context.variant = variant
    return build_graph(context, variant=variant).compile(checkpointer=get_checkpointer())
