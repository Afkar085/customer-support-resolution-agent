"""FastAPI authentication dependencies."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.principal import Principal, principal_from_token
from app.db.base import get_db
from app.services.errors import ToolError

DbSession = Annotated[Session, Depends(get_db)]


def _token_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def current_principal(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    try:
        return principal_from_token(session, _token_from_header(authorization))
    except ToolError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.as_dict(),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def current_customer(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_customer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "FORBIDDEN", "message": "A customer identity is required."},
        )
    return principal


def current_reviewer(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_reviewer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "REVIEWER_ROLE_REQUIRED",
                "message": "A support reviewer identity is required.",
            },
        )
    return principal


def raw_token(authorization: Annotated[str | None, Header()] = None) -> str:
    """The workflow needs the token itself so tool calls re-derive identity
    from it on every step, rather than trusting a customer id in the state."""
    token = _token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail={"error_code": "NOT_AUTHENTICATED"})
    return token


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
CurrentCustomer = Annotated[Principal, Depends(current_customer)]
CurrentReviewer = Annotated[Principal, Depends(current_reviewer)]
BearerToken = Annotated[str, Depends(raw_token)]
