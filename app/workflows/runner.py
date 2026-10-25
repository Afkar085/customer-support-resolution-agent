"""Starting, pausing, resuming and recovering workflow runs.

The run row in Postgres and the LangGraph checkpoint play different parts:

* the checkpoint is how the graph continues from where it paused;
* the run row is what the API, the dashboard and the eval harness read.

A checkpoint alone is not enough to restart unfinished work, so
``recover_unfinished`` exists: it reconciles in-flight refunds and resumes
threads whose approval has since been decided.
"""
from __future__ import annotations

import time
from typing import Any

from langgraph.types import Command
from sqlalchemy import select

from app.auth.principal import Principal
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import (
    ActionProposal,
    Approval,
    RefundOperation,
    Ticket,
    WorkflowRun,
    new_id,
    utcnow,
)
from app.services import audit, refunds
from app.services.common import iso
from app.services.errors import ErrorCode, ToolError
from app.workflows.graph import RunContext, compile_graph
from app.workflows.state import initial_state


class RunResult(dict):
    """A plain dict with named accessors, so callers can stay dict-oriented."""

    @property
    def run_id(self) -> str:
        return self["run_id"]

    @property
    def paused(self) -> bool:
        return self["status"] == "awaiting_approval"


def _config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_settings().max_workflow_steps,
    }


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    raw = result.get("__interrupt__")
    if not raw:
        return None
    first = raw[0]
    value = getattr(first, "value", None)
    return value if isinstance(value, dict) else {"value": value}


def start_run(
    principal: Principal,
    *,
    ticket_id: str,
    auth_token: str,
    variant: str = "specialists",
) -> RunResult:
    customer_id = principal.require_customer()
    with session_scope() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None or ticket.customer_id != customer_id:
            raise ToolError(ErrorCode.TICKET_NOT_FOUND, "Ticket not found.")
        thread_id = f"thr_{new_id('')[1:]}"
        run = WorkflowRun(
            ticket_id=ticket.id,
            thread_id=thread_id,
            authenticated_customer_id=customer_id,
            status="queued",
            stage="created",
            started_at=utcnow(),
        )
        session.add(run)
        session.flush()
        run_id = run.id
        message = ticket.customer_message
        extra = [
            note["note"]
            for note in (ticket.notes or [])
            if note.get("actor") == "customer" and note.get("note")
        ]
        order_id = ticket.order_id
        audit.record(
            session,
            actor=f"customer:{customer_id}",
            event_type="run.started",
            run_id=run_id,
            ticket_id=ticket.id,
            message="Resolution workflow started.",
            payload={"thread_id": thread_id},
        )

    state = initial_state(
        run_id=run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        customer_id=customer_id,
        auth_token=auth_token,
        customer_message=message,
        order_id=order_id,
    )
    state["extra_customer_messages"] = extra
    return _drive(run_id, thread_id, state, variant=variant)


def resume_run(
    run_id: str, resume_value: dict[str, Any], *, variant: str = "specialists"
) -> RunResult:
    with session_scope() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            raise ToolError(ErrorCode.RUN_NOT_FOUND, "Run not found.")
        thread_id = run.thread_id
        run.status = "running"
    return _drive(run_id, thread_id, Command(resume=resume_value), variant=variant)


def _drive(
    run_id: str,
    thread_id: str,
    payload: Any,
    *,
    variant: str = "specialists",
) -> RunResult:
    context = RunContext()
    graph = compile_graph(context, variant=variant)

    started = time.perf_counter()
    try:
        result = graph.invoke(payload, config=_config(thread_id))
    except ToolError as exc:
        _fail(run_id, exc.as_dict())
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        _fail(
            run_id,
            {"error_code": ErrorCode.INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"},
        )
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    interrupt_payload = _interrupt_payload(result)
    with session_scope() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        # A run can be driven more than once: start, then resume after the
        # approval pause. Every counter is a total across those legs.
        run.active_ms = (run.active_ms or 0) + elapsed_ms
        run.model_calls = (run.model_calls or 0) + context.usage.calls
        run.tool_calls = (run.tool_calls or 0) + context.tool_calls
        run.estimated_cost_usd_micros = (
            run.estimated_cost_usd_micros or 0
        ) + context.usage.estimated_cost_usd_micros
        if interrupt_payload is not None:
            run.status = "awaiting_approval"
            run.stage = "awaiting_approval"
        status = run.status
        stage = run.stage
        outcome = run.outcome
        final_response = run.final_response

    return RunResult(
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        stage=stage,
        outcome=outcome,
        final_response=final_response,
        awaiting=interrupt_payload,
        active_ms=elapsed_ms,
        model_mode=context.usage.mode,
        variant=variant,
        usage=context.usage.as_dict(),
        tool_calls=context.tool_calls,
        state={
            key: value
            for key, value in result.items()
            if key not in ("auth_token", "__interrupt__")
        },
    )


def _fail(run_id: str, details: dict[str, Any]) -> None:
    with session_scope() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.stage = "failed"
        run.error_details = details
        run.completed_at = utcnow()
        audit.record(
            session,
            actor="system:workflow",
            event_type="run.failed",
            run_id=run_id,
            ticket_id=run.ticket_id,
            severity="error",
            message=str(details.get("message"))[:400],
            payload=details,
        )


# --------------------------------------------------------------------------- #
# Status / events
# --------------------------------------------------------------------------- #
def run_view(session, run_id: str) -> dict[str, Any]:
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise ToolError(ErrorCode.RUN_NOT_FOUND, "Run not found.")
    proposals = session.scalars(
        select(ActionProposal).where(ActionProposal.run_id == run_id)
    ).all()
    pending = [
        {
            "proposal_id": proposal.id,
            "action_type": proposal.action_type,
            "status": proposal.status,
            "payload": proposal.payload,
            "approval": _approval_brief(session, proposal.id),
        }
        for proposal in proposals
    ]
    operations = [
        refunds.get_operation(session, row.id)
        for row in session.scalars(
            select(RefundOperation).where(RefundOperation.run_id == run_id)
        ).all()
    ]
    return {
        "run_id": run.id,
        "ticket_id": run.ticket_id,
        "thread_id": run.thread_id,
        "status": run.status,
        "stage": run.stage,
        "outcome": run.outcome,
        "final_response": run.final_response,
        "started_at": iso(run.started_at),
        "completed_at": iso(run.completed_at),
        "active_ms": run.active_ms,
        "waiting_ms": run.waiting_ms,
        "model_calls": run.model_calls,
        "tool_calls": run.tool_calls,
        "retry_count": run.retry_count,
        "estimated_cost_usd_micros": run.estimated_cost_usd_micros,
        "error_details": run.error_details,
        "snapshot": run.snapshot,
        "proposals": pending,
        "refund_operations": operations,
    }


def _approval_brief(session, proposal_id: str) -> dict[str, Any] | None:
    approval = session.scalar(select(Approval).where(Approval.proposal_id == proposal_id))
    if approval is None:
        return None
    return {
        "approval_id": approval.id,
        "decision": approval.decision,
        "reviewer_id": approval.reviewer_id,
        "expires_at": iso(approval.expires_at),
        "decided_at": iso(approval.decided_at),
        "consumed_at": iso(approval.consumed_at),
    }


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #
def recover_unfinished(limit: int = 25) -> dict[str, Any]:
    """Run at startup and from the dashboard's recovery button.

    Order matters: reconcile money first, then resume workflows. A workflow
    resumed before reconciliation would be deciding from a stale picture.
    """
    reconciled = refunds.reconcile_unsettled()

    resumed: list[dict[str, Any]] = []
    with session_scope() as session:
        candidates = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status.in_(("awaiting_approval", "running", "queued")))
            .limit(limit)
        ).all()
        to_resume: list[tuple[str, dict[str, Any]]] = []
        for run in candidates:
            proposal = session.scalar(
                select(ActionProposal)
                .where(ActionProposal.run_id == run.id)
                .order_by(ActionProposal.created_at.desc())
            )
            if proposal is None:
                continue
            approval = session.scalar(
                select(Approval).where(Approval.proposal_id == proposal.id)
            )
            if approval is None or approval.decision == "pending":
                continue
            if approval.consumed_at is not None and proposal.status == "executed":
                continue
            to_resume.append(
                (
                    run.id,
                    {
                        "decision": approval.decision,
                        "approval_id": approval.id,
                        "reviewer_id": approval.reviewer_id,
                    },
                )
            )

    for run_id, resume_value in to_resume:
        try:
            result = resume_run(run_id, resume_value)
            resumed.append({"run_id": run_id, "status": result["status"]})
        except Exception as exc:  # noqa: BLE001 - reported, never fatal at startup
            resumed.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "reconciled_operations": reconciled,
        "resumed_runs": resumed,
    }
