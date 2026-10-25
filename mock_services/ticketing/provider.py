"""Mock ticketing system.

Separate from the refund path on purpose: when a refund succeeds but the
ticket update fails, only the ticket update is retried.
"""
from __future__ import annotations

from typing import Any

from app.db.base import session_scope
from app.db.models import Ticket, utcnow
from app.services.common import iso
from mock_services.payments.provider import (
    FaultMode,
    ProviderTimeout,
    ProviderUnavailable,
    _consume_fault,
)

SERVICE = "ticketing"

VALID_STATUS = {
    "open",
    "investigating",
    "awaiting_customer",
    "awaiting_approval",
    "resolved",
    "escalated",
    "rejected",
}


def update_ticket(
    ticket_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    category: str | None = None,
    summary: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    fault = _consume_fault(SERVICE)
    if fault == FaultMode.UNAVAILABLE:
        raise ProviderUnavailable("ticketing API unavailable")
    if fault == FaultMode.TIMEOUT:
        raise ProviderTimeout("ticketing API timed out")

    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"invalid ticket status: {status}")

    with session_scope() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        if status is not None:
            ticket.status = status
        if category is not None:
            ticket.category = category
        if summary is not None:
            ticket.summary = summary
        if note:
            notes = list(ticket.notes or [])
            notes.append({"actor": actor, "note": note, "at": iso(utcnow())})
            ticket.notes = notes
        session.flush()
        return {
            "ticket_id": ticket.id,
            "status": ticket.status,
            "category": ticket.category,
            "summary": ticket.summary,
            "note_count": len(ticket.notes or []),
        }
