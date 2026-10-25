"""The tool layer.

Design rules, each of which shows up in the signatures below:

* Identity comes from ``ToolContext.principal``, never from an argument. There
  is no ``customer_id`` parameter anywhere in this file.
* Ownership is re-checked inside the service on every call.
* Privileged fields -- reviewer identity, approval state, payment credentials --
  are not parameters at all.
* Failures come back as structured dicts, not exceptions.
* Each toolset grants only what that caller needs. The investigation toolset
  physically cannot execute a refund: the name is not in its allow-list.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.auth.principal import Principal
from app.db.base import session_scope
from app.db.models import ActionProposal, WorkflowRun
from app.services import approvals, audit, eligibility, orders, policy_store, proposals, refunds
from app.services import tickets as ticket_service
from app.services.common import utcnow
from app.services.errors import ErrorCode, ToolError, ok


@dataclass(slots=True)
class ToolContext:
    principal: Principal
    run_id: str | None = None
    ticket_id: str | None = None
    tool_calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
def get_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return ok(**orders.get_order(session, ctx.principal, order_id))


def list_orders(ctx: ToolContext) -> dict[str, Any]:
    with session_scope() as session:
        return ok(orders=orders.list_customer_orders(session, ctx.principal))


def get_return_status(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return ok(**orders.get_return_status(session, ctx.principal, order_id))


def get_payment_status(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return ok(**orders.get_payment_status(session, ctx.principal, order_id))


def get_shipment_status(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return ok(**orders.get_shipment_status(session, ctx.principal, order_id))


def get_refund_status(ctx: ToolContext, operation_id: str) -> dict[str, Any]:
    with session_scope() as session:
        operation = refunds.get_operation(session, operation_id)
        # Ownership: the operation must sit on an order this principal owns.
        orders.load_owned_order(session, ctx.principal, operation["order_id"])
        return ok(**operation)


def get_refund_eligibility(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    """Deterministic rules, exposed read-only so an agent can see *why*."""
    with session_scope() as session:
        orders.load_owned_order(session, ctx.principal, order_id)
        return ok(**eligibility.evaluate_refund(session, order_id).as_dict())


def search_policy(
    ctx: ToolContext,
    query: str,
    *,
    topics: list[str] | None = None,
    document_ids: list[str] | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    with session_scope() as session:
        hits = policy_store.search(
            session, query, topics=topics, document_ids=document_ids, top_k=top_k, as_of=utcnow()
        )
        return ok(
            results=[hit.as_dict() for hit in hits],
            conflicts=policy_store.detect_conflicts(hits),
            policy_established=bool(hits),
        )


def get_ticket(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    with session_scope() as session:
        ticket = ticket_service.load_owned_ticket(session, ctx.principal, ticket_id)
        return ok(**ticket_service.view(session, ticket))


# --------------------------------------------------------------------------- #
# Write tools
# --------------------------------------------------------------------------- #
def create_refund_proposal(
    ctx: ToolContext,
    *,
    order_id: str,
    amount_minor: int,
    reason: str,
    evidence_refs: list[str] | None = None,
    policy_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Prepares a refund request. Moves no money.

    Validates ownership, eligibility and the amount *before* writing, so an
    impossible proposal never reaches a reviewer's queue.
    """
    if not ctx.run_id:
        raise ToolError(ErrorCode.INTERNAL_ERROR, "create_refund_proposal requires a run context.")
    with session_scope() as session:
        order = orders.load_owned_order(session, ctx.principal, order_id)
        run = session.get(WorkflowRun, ctx.run_id)
        if run is None:
            raise ToolError(ErrorCode.RUN_NOT_FOUND, "Workflow run not found.")

        decision = eligibility.evaluate_refund(session, order_id)
        if not decision.eligible:
            raise ToolError(
                ErrorCode.APPROVAL_REQUIRED
                if decision.escalate
                else decision.blockers[0].code,
                decision.blockers[0].message,
                blockers=decision.blocking_codes,
            )
        valid, problems = eligibility.validate_proposed_amount(session, order_id, amount_minor)
        if not valid:
            raise ToolError(ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE, problems[0].message)

        proposal, created = proposals.create_or_get(
            session,
            run=run,
            action_type="refund",
            payload={
                "action": "request_refund_approval",
                "order_id": order_id,
                "amount_minor": int(amount_minor),
                "currency": order.currency,
            },
            evidence_refs=evidence_refs,
            policy_refs=policy_refs,
            rationale=reason,
        )
        if created:
            proposals.invalidate_superseded(session, run.id, proposal.id)
        approval = approvals.create_pending(session, proposal)
        return ok(
            proposal_id=proposal.id,
            approval_id=approval.id,
            created=created,
            payload=proposal.payload,
            payload_hash=proposal.payload_hash,
            status=proposal.status,
            requires_approval=True,
        )


def execute_approved_refund(ctx: ToolContext, *, proposal_id: str) -> dict[str, Any]:
    """Executes a refund that a reviewer has already approved.

    Everything privileged is re-derived here: the approval row, the reviewer's
    decision, the payload hash and the refundable balance. The caller supplies
    only a proposal id.
    """
    with session_scope() as session:
        proposal = session.get(ActionProposal, proposal_id)
        if proposal is None:
            raise ToolError(ErrorCode.PROPOSAL_NOT_FOUND, "Proposal not found.")
        approval = approvals.validate_for_execution(session, proposal_id)
        operation = refunds.reserve_operation(session, proposal=proposal, approval=approval)
        approvals.consume(session, approval)
        proposal.status = "executed"
        operation_id = operation.id
        audit.record(
            session,
            actor="system:executor",
            event_type="refund.reserved",
            run_id=proposal.run_id,
            ticket_id=proposal.ticket_id,
            message="Approval revalidated and refundable amount reserved.",
            payload={
                "operation_id": operation_id,
                "proposal_id": proposal_id,
                "approval_id": approval.id,
            },
        )
    # The reservation is committed. Only now does the provider get called.
    result = refunds.execute_operation(operation_id)
    return ok(**result)


def update_ticket(
    ctx: ToolContext,
    *,
    ticket_id: str,
    status: str | None = None,
    note: str | None = None,
    category: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    result = ticket_service.update_through_api(
        ticket_id=ticket_id,
        status=status,
        note=note,
        category=category,
        summary=summary,
        actor="agent",
    )
    return ok(**result)


def reconcile_refund(ctx: ToolContext, *, operation_id: str) -> dict[str, Any]:
    return ok(**refunds.reconcile_operation(operation_id))


# --------------------------------------------------------------------------- #
# Toolsets -- least privilege, per specialist
# --------------------------------------------------------------------------- #
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_order": get_order,
    "list_orders": list_orders,
    "get_return_status": get_return_status,
    "get_payment_status": get_payment_status,
    "get_shipment_status": get_shipment_status,
    "get_refund_status": get_refund_status,
    "get_refund_eligibility": get_refund_eligibility,
    "search_policy": search_policy,
    "get_ticket": get_ticket,
    "create_refund_proposal": create_refund_proposal,
    "execute_approved_refund": execute_approved_refund,
    "update_ticket": update_ticket,
    "reconcile_refund": reconcile_refund,
}

TOOLSETS: dict[str, frozenset[str]] = {
    # Gathers facts. Cannot propose, cannot execute, cannot touch tickets.
    "investigation": frozenset(
        {
            "get_order",
            "list_orders",
            "get_return_status",
            "get_payment_status",
            "get_shipment_status",
            "get_refund_status",
            "get_refund_eligibility",
        }
    ),
    # Reads policy only.
    "policy": frozenset({"search_policy"}),
    # Combines facts and policy into a proposal. Still cannot execute.
    "resolution": frozenset({"get_refund_eligibility", "create_refund_proposal", "search_policy"}),
    # Only the workflow's execution node holds this.
    "executor": frozenset({"execute_approved_refund", "update_ticket", "reconcile_refund"}),
    # The single-agent baseline: every read tool in one bundle. Still no
    # execution tool -- that stays with the workflow in both designs.
    "generalist": frozenset(
        {
            "get_order",
            "list_orders",
            "get_return_status",
            "get_payment_status",
            "get_shipment_status",
            "get_refund_status",
            "get_refund_eligibility",
            "search_policy",
            "create_refund_proposal",
        }
    ),
}


class Toolset:
    """A name-restricted facade over TOOLS."""

    def __init__(self, ctx: ToolContext, name: str) -> None:
        if name not in TOOLSETS:
            raise KeyError(f"unknown toolset: {name}")
        self.ctx = ctx
        self.name = name
        self.allowed = TOOLSETS[name]

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self.allowed

    def call(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        if tool_name not in self.allowed:
            failure = ToolError(
                ErrorCode.FORBIDDEN,
                f"Tool '{tool_name}' is not available to the {self.name} agent.",
            ).as_dict()
            self._log(tool_name, kwargs, failure)
            return failure
        self.ctx.tool_calls += 1
        try:
            result = TOOLS[tool_name](self.ctx, **kwargs)
        except ToolError as exc:
            result = exc.as_dict()
        except Exception as exc:  # defensive: never leak a traceback to an agent
            result = ToolError(
                ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}", retryable=True
            ).as_dict()
        self._log(tool_name, kwargs, result)
        return result

    def _log(self, tool_name: str, kwargs: dict[str, Any], result: dict[str, Any]) -> None:
        self.ctx.log.append(
            {
                "toolset": self.name,
                "tool": tool_name,
                "args": {k: v for k, v in kwargs.items() if k != "note"},
                "success": bool(result.get("success")),
                "error_code": result.get("error_code"),
            }
        )


def tool_catalogue() -> list[dict[str, Any]]:
    """Used by the dashboard and the README to show the tool contracts."""
    import inspect

    out = []
    for name, fn in TOOLS.items():
        signature = inspect.signature(fn)
        params = [p for p in signature.parameters if p != "ctx"]
        out.append(
            {
                "tool": name,
                "parameters": params,
                "doc": (fn.__doc__ or "").strip().split("\n")[0],
                "toolsets": sorted(key for key, names in TOOLSETS.items() if name in names),
                "write": name
                in {
                    "create_refund_proposal",
                    "execute_approved_refund",
                    "update_ticket",
                    "reconcile_refund",
                },
            }
        )
    return out
