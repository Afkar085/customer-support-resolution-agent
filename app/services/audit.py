"""Append-only audit trail. Everything the dashboard timeline shows comes from
here, and everything written here is redacted first."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent
from app.services.common import iso, redact


def record(
    session: Session,
    *,
    actor: str,
    event_type: str,
    message: str = "",
    run_id: str | None = None,
    ticket_id: str | None = None,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        run_id=run_id,
        ticket_id=ticket_id,
        actor=actor,
        event_type=event_type,
        severity=severity,
        message=message,
        redacted_payload=redact(payload or {}),
    )
    session.add(event)
    session.flush()
    return event


def timeline(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
    ).all()
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "event_type": row.event_type,
            "severity": row.severity,
            "message": row.message,
            "payload": row.redacted_payload,
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]


def ticket_timeline(session: Session, ticket_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditEvent).where(AuditEvent.ticket_id == ticket_id).order_by(AuditEvent.id)
    ).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "actor": row.actor,
            "event_type": row.event_type,
            "severity": row.severity,
            "message": row.message,
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]
