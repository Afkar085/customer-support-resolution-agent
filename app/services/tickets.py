"""Ticket lifecycle. Writes go through the (mock) ticketing API so that a
failing ticket update is a realistic, independently retryable failure."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.db.models import Ticket, WorkflowRun
from app.services import audit
from app.services.common import iso, retry_read
from app.services.errors import ErrorCode, ToolError
from app.services.orders import load_owned_order
from mock_services.payments.provider import ProviderTimeout, ProviderUnavailable
from mock_services.ticketing import provider as ticketing_provider


def create_ticket(
    session: Session,
    principal: Principal,
    *,
    customer_message: str,
    order_id: str | None = None,
) -> Ticket:
    customer_id = principal.require_customer()
    if order_id:
        # Validates ownership before the id is ever stored on the ticket.
        load_owned_order(session, principal, order_id)
    ticket = Ticket(
        customer_id=customer_id,
        order_id=order_id,
        customer_message=customer_message.strip(),
        summary=customer_message.strip()[:180],
        status="open",
    )
    session.add(ticket)
    session.flush()
    audit.record(
        session,
        actor=f"customer:{customer_id}",
        event_type="ticket.created",
        ticket_id=ticket.id,
        message="Customer submitted a support request.",
        payload={"ticket_id": ticket.id, "order_id": order_id},
    )
    return ticket


def load_owned_ticket(session: Session, principal: Principal, ticket_id: str) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise ToolError(ErrorCode.TICKET_NOT_FOUND, "Ticket not found.", ticket_id=ticket_id)
    if principal.is_customer and ticket.customer_id != principal.customer_id:
        raise ToolError(ErrorCode.FORBIDDEN, "Ticket not found.", ticket_id=ticket_id)
    return ticket


def view(session: Session, ticket: Ticket) -> dict[str, Any]:
    runs = session.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.ticket_id == ticket.id)
        .order_by(WorkflowRun.created_at)
    ).all()
    return {
        "ticket_id": ticket.id,
        "customer_id": ticket.customer_id,
        "order_id": ticket.order_id,
        "category": ticket.category,
        "status": ticket.status,
        "summary": ticket.summary,
        "customer_message": ticket.customer_message,
        "notes": ticket.notes or [],
        "created_at": iso(ticket.created_at),
        "runs": [{"run_id": run.id, "status": run.status, "stage": run.stage} for run in runs],
    }


def list_for_customer(session: Session, principal: Principal) -> list[dict[str, Any]]:
    customer_id = principal.require_customer()
    tickets = session.scalars(
        select(Ticket).where(Ticket.customer_id == customer_id).order_by(Ticket.created_at.desc())
    ).all()
    return [view(session, ticket) for ticket in tickets]


def update_through_api(
    *,
    ticket_id: str,
    status: str | None = None,
    note: str | None = None,
    category: str | None = None,
    summary: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    """Retried independently of any refund that may already have succeeded."""

    def _call() -> dict[str, Any]:
        try:
            return ticketing_provider.update_ticket(
                ticket_id, status=status, note=note, category=category, summary=summary, actor=actor
            )
        except (ProviderTimeout, ProviderUnavailable) as exc:
            raise ToolError(
                ErrorCode.PROVIDER_UNAVAILABLE, str(exc), retryable=True, service="ticketing"
            ) from exc
        except KeyError as exc:
            raise ToolError(ErrorCode.TICKET_NOT_FOUND, "Ticket not found.") from exc

    return retry_read(_call)


def append_customer_message(
    session: Session, principal: Principal, ticket_id: str, message: str
) -> Ticket:
    ticket = load_owned_ticket(session, principal, ticket_id)
    notes = list(ticket.notes or [])
    notes.append({"actor": "customer", "note": message.strip(), "at": iso(_now())})
    ticket.notes = notes
    session.flush()
    audit.record(
        session,
        actor=f"customer:{ticket.customer_id}",
        event_type="ticket.customer_message",
        ticket_id=ticket.id,
        message="Customer supplied additional information.",
        payload={"length": len(message)},
    )
    return ticket


def _now():
    from app.services.common import utcnow

    return utcnow()
