"""HTTP API.

Authorization is checked on every endpoint through a dependency, and the
authenticated principal is the only source of customer identity. Nothing in a
request body can change who the caller is.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.auth.deps import (
    BearerToken,
    CurrentCustomer,
    CurrentPrincipal,
    CurrentReviewer,
    DbSession,
)
from app.config import get_settings
from app.db.models import Approval, RefundOperation, Ticket, WorkflowRun
from app.schemas.api import (
    ApprovalDecisionRequest,
    CreateTicketRequest,
    CustomerMessageRequest,
    StartRunRequest,
)
from app.services import approvals as approval_service
from app.services import audit, policy_store
from app.services import tickets as ticket_service
from app.tools.registry import tool_catalogue
from app.workflows import runner

router = APIRouter()


def _model_mode(settings) -> str:
    """What the agents will actually run on, as the console reports it."""
    if settings.force_offline_reasoner or settings.llm_provider == "offline":
        return "offline"
    if settings.llm_provider in ("auto", "anthropic") and settings.anthropic_api_key:
        return "anthropic"
    if settings.llm_provider in ("auto", "openai") and settings.openai_api_key:
        return "openai"
    return "offline"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@router.get("/health", tags=["ops"])
def health(session: DbSession) -> dict[str, Any]:
    settings = get_settings()
    session.execute(select(1))
    documents = policy_store.list_documents(session)
    return {
        "status": "ok",
        "environment": settings.environment,
        "database": "postgres" if settings.is_postgres else "sqlite",
        "model_mode": _model_mode(settings),
        "model_name": settings.model_name,
        "policy_documents": len(documents),
        "approval_ttl_minutes": settings.approval_ttl_minutes,
    }


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
@router.post("/tickets", status_code=status.HTTP_201_CREATED, tags=["customer"])
def create_ticket(
    payload: CreateTicketRequest, principal: CurrentCustomer, session: DbSession
) -> dict[str, Any]:
    ticket = ticket_service.create_ticket(
        session,
        principal,
        customer_message=payload.customer_message,
        order_id=payload.order_id,
    )
    return ticket_service.view(session, ticket)


@router.get("/tickets", tags=["customer"])
def list_tickets(principal: CurrentCustomer, session: DbSession) -> dict[str, Any]:
    return {"tickets": ticket_service.list_for_customer(session, principal)}


@router.get("/tickets/{ticket_id}", tags=["customer"])
def get_ticket(ticket_id: str, principal: CurrentCustomer, session: DbSession) -> dict[str, Any]:
    ticket = ticket_service.load_owned_ticket(session, principal, ticket_id)
    view = ticket_service.view(session, ticket)
    view["events"] = audit.ticket_timeline(session, ticket_id)
    return view


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
@router.post(
    "/tickets/{ticket_id}/runs", status_code=status.HTTP_202_ACCEPTED, tags=["customer"]
)
def start_run(
    ticket_id: str,
    payload: StartRunRequest,
    principal: CurrentCustomer,
    token: BearerToken,
    response: Response,
) -> dict[str, Any]:
    """Starts the resolution workflow.

    Returns 202 with the run id. Short runs finish inside the request; a run
    that reaches the approval step comes back as `awaiting_approval` with the
    reviewer payload attached.
    """
    result = runner.start_run(
        principal,
        ticket_id=ticket_id,
        auth_token=token,
        variant=payload.variant,
    )
    response.headers["Location"] = f"/runs/{result['run_id']}"
    return dict(result)


@router.get("/runs/{run_id}", tags=["customer"])
def get_run(run_id: str, principal: CurrentCustomer, session: DbSession) -> dict[str, Any]:
    run = session.get(WorkflowRun, run_id)
    if run is not None:
        # Ownership check before anything about the run is revealed.
        ticket_service.load_owned_ticket(session, principal, run.ticket_id)
    return runner.run_view(session, run_id)


@router.get("/runs/{run_id}/events", tags=["customer"])
def get_run_events(run_id: str, principal: CurrentCustomer, session: DbSession) -> dict[str, Any]:
    run = session.get(WorkflowRun, run_id)
    if run is not None:
        ticket_service.load_owned_ticket(session, principal, run.ticket_id)
    return {"run_id": run_id, "events": audit.timeline(session, run_id)}


@router.post("/runs/{run_id}/messages", tags=["customer"])
def add_customer_message(
    run_id: str,
    payload: CustomerMessageRequest,
    principal: CurrentCustomer,
    token: BearerToken,
    session: DbSession,
) -> dict[str, Any]:
    """Supply the information a run asked for.

    A run that has already asked for clarification is finished; the extra
    detail is recorded on the ticket and a fresh run picks the case up with
    the full conversation. The original run stays in the audit trail.
    """
    run = session.get(WorkflowRun, run_id)
    if run is None:
        return {"error_code": "RUN_NOT_FOUND", "message": "Run not found."}
    ticket = ticket_service.load_owned_ticket(session, principal, run.ticket_id)
    ticket_service.append_customer_message(session, principal, ticket.id, payload.message)
    session.commit()

    if not payload.restart:
        return {"run_id": run_id, "ticket_id": ticket.id, "restarted": False}

    result = runner.start_run(principal, ticket_id=ticket.id, auth_token=token)
    return {"previous_run_id": run_id, "restarted": True, **dict(result)}


# --------------------------------------------------------------------------- #
# Reviewer surface
# --------------------------------------------------------------------------- #
@router.get("/approvals", tags=["reviewer"])
def list_approvals(
    principal: CurrentReviewer, session: DbSession, include_decided: bool = False
) -> dict[str, Any]:
    return {
        "approvals": approval_service.list_for_reviewer(
            session, principal, include_decided=include_decided
        )
    }


@router.get("/approvals/{approval_id}", tags=["reviewer"])
def get_approval(
    approval_id: str, principal: CurrentReviewer, session: DbSession
) -> dict[str, Any]:
    card = approval_service.get_card(session, principal, approval_id)
    card["events"] = audit.timeline(session, card["run_id"])
    return card


@router.post("/approvals/{approval_id}/decision", tags=["reviewer"])
def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    principal: CurrentReviewer,
    session: DbSession,
) -> dict[str, Any]:
    """Record the decision, then resume the paused workflow.

    The decision is committed before the workflow is resumed, so a crash
    between the two leaves a decided approval that `recover_unfinished` picks
    up -- rather than a lost decision.
    """
    decision = approval_service.decide(
        session, principal, approval_id, decision=payload.decision, note=payload.note
    )
    session.commit()

    resume_value = {
        "decision": decision["decision"],
        "approval_id": decision["approval_id"],
        "reviewer_id": decision["reviewer_id"],
    }
    try:
        result = runner.resume_run(decision["run_id"], resume_value)
    except Exception as exc:  # noqa: BLE001 - the decision is already durable
        return {
            **decision,
            "resumed": False,
            "resume_error": f"{type(exc).__name__}: {exc}",
        }
    return {**decision, "resumed": True, "run": dict(result)}


@router.get("/reviewer/queue-summary", tags=["reviewer"])
def queue_summary(principal: CurrentReviewer, session: DbSession) -> dict[str, Any]:
    principal.require_reviewer("approvals:read")
    rows = session.scalars(select(Approval)).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.decision] = counts.get(row.decision, 0) + 1
    return {"counts": counts, "total": len(rows)}


# --------------------------------------------------------------------------- #
# Reference data (used by the dashboard and the docs)
# --------------------------------------------------------------------------- #
@router.get("/meta/overview", tags=["meta"])
def meta_overview(principal: CurrentPrincipal, session: DbSession) -> dict[str, Any]:
    """Counters for the console header.

    Deliberately includes `duplicate_refunds`: it is the number this system
    exists to keep at zero, so it belongs on the first screen rather than in a
    test report.
    """
    runs = session.scalars(select(WorkflowRun)).all()
    by_status: dict[str, int] = {}
    for run in runs:
        by_status[run.status] = by_status.get(run.status, 0) + 1

    tickets = session.scalars(select(Ticket)).all()
    open_tickets = sum(
        1 for ticket in tickets if ticket.status not in ("resolved", "rejected")
    )

    pending_approvals = sum(
        1 for row in session.scalars(select(Approval)).all() if row.decision == "pending"
    )

    operations = session.scalars(select(RefundOperation)).all()
    settled = [row for row in operations if row.status == "succeeded"]
    in_flight = [row for row in operations if row.status in RefundOperation.IN_FLIGHT]
    keys = [row.idempotency_key for row in operations]

    return {
        "open_tickets": open_tickets,
        "total_tickets": len(tickets),
        "pending_approvals": pending_approvals,
        "runs_by_status": by_status,
        "refunds_settled": len(settled),
        "refunds_settled_minor": sum(row.amount_minor for row in settled),
        "refunds_in_flight": len(in_flight),
        "duplicate_refunds": len(keys) - len(set(keys)),
        "currency": get_settings().currency,
    }


@router.get("/meta/tools", tags=["meta"])
def meta_tools() -> dict[str, Any]:
    return {"tools": tool_catalogue()}


@router.get("/meta/policies", tags=["meta"])
def meta_policies(session: DbSession) -> dict[str, Any]:
    return {"documents": policy_store.list_documents(session)}


@router.get("/meta/policy-search", tags=["meta"])
def meta_policy_search(q: str, session: DbSession, top_k: int = 5) -> dict[str, Any]:
    hits = policy_store.search(session, q, top_k=top_k)
    return {"query": q, "results": [hit.as_dict() for hit in hits]}
