"""Authenticated identity.

A Principal is created from a bearer token by the server and is the ONLY
source of customer identity in the system. No request body, tool argument or
model output can set `customer_id`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuthToken
from app.services.errors import ErrorCode, ToolError


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: str
    display_name: str
    customer_id: str | None = None
    reviewer_id: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_customer(self) -> bool:
        return self.role == "customer"

    @property
    def is_reviewer(self) -> bool:
        return self.role == "reviewer"

    def require_customer(self) -> str:
        if not self.is_customer or not self.customer_id:
            raise ToolError(ErrorCode.FORBIDDEN, "A customer identity is required.")
        return self.customer_id

    def require_reviewer(self, scope: str = "approvals:decide") -> str:
        if not self.is_reviewer or not self.reviewer_id:
            raise ToolError(
                ErrorCode.REVIEWER_ROLE_REQUIRED, "A support reviewer identity is required."
            )
        if scope and scope not in self.scopes:
            raise ToolError(ErrorCode.FORBIDDEN, f"Missing required scope: {scope}")
        return self.reviewer_id


SYSTEM = Principal(
    subject="system:workflow",
    role="reviewer",
    display_name="Workflow engine",
    reviewer_id=None,
    scopes=(),
)


def principal_from_token(session: Session, token: str | None) -> Principal:
    if not token:
        raise ToolError(ErrorCode.NOT_AUTHENTICATED, "Missing bearer token.")
    row = session.scalar(select(AuthToken).where(AuthToken.token == token))
    if row is None or not row.active:
        raise ToolError(ErrorCode.NOT_AUTHENTICATED, "Unknown or inactive token.")
    return Principal(
        subject=row.subject,
        role=row.role,
        display_name=row.display_name,
        customer_id=row.customer_id,
        reviewer_id=row.reviewer_id,
        scopes=tuple(row.scopes or ()),
    )
