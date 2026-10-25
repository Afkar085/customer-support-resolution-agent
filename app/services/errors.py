"""Structured failures.

Every tool returns either a typed result or one of these. Nothing raises a
bare exception into the agent loop, because an agent cannot reason about a
stack trace -- it can reason about `retryable: false`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    # authorization
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    ORDER_NOT_OWNED = "ORDER_NOT_OWNED"
    REVIEWER_ROLE_REQUIRED = "REVIEWER_ROLE_REQUIRED"

    # lookup
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    NO_APPLICABLE_POLICY = "NO_APPLICABLE_POLICY"

    # eligibility
    RETURN_NOT_RECEIVED = "RETURN_NOT_RECEIVED"
    INSPECTION_NOT_PASSED = "INSPECTION_NOT_PASSED"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    NO_RETURN_ON_RECORD = "NO_RETURN_ON_RECORD"
    RETURN_WINDOW_EXPIRED = "RETURN_WINDOW_EXPIRED"
    ORDER_NOT_DELIVERED = "ORDER_NOT_DELIVERED"
    ALREADY_FULLY_REFUNDED = "ALREADY_FULLY_REFUNDED"
    AMOUNT_EXCEEDS_REFUNDABLE = "AMOUNT_EXCEEDS_REFUNDABLE"
    REFUND_ALREADY_IN_FLIGHT = "REFUND_ALREADY_IN_FLIGHT"
    PAYMENT_NOT_CAPTURED = "PAYMENT_NOT_CAPTURED"
    MISSING_INFORMATION = "MISSING_INFORMATION"

    # approval
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_NOT_GRANTED = "APPROVAL_NOT_GRANTED"
    APPROVAL_ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    PROPOSAL_CHANGED = "PROPOSAL_CHANGED"

    # provider / transport
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    IDEMPOTENCY_PAYLOAD_MISMATCH = "IDEMPOTENCY_PAYLOAD_MISMATCH"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

    # model / workflow
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
    STEP_LIMIT_EXCEEDED = "STEP_LIMIT_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


RETRYABLE_CODES = frozenset(
    {
        ErrorCode.PROVIDER_TIMEOUT,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
    }
)

# Codes that mean "a human must look at this", as opposed to "tell the
# customer why not".
ESCALATION_CODES = frozenset(
    {
        ErrorCode.NO_APPLICABLE_POLICY,
        ErrorCode.INSPECTION_FAILED,
        ErrorCode.OUTCOME_UNKNOWN,
        ErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH,
        ErrorCode.INVALID_MODEL_OUTPUT,
        ErrorCode.STEP_LIMIT_EXCEEDED,
    }
)


@dataclass(slots=True)
class ToolFailure:
    error_code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    success: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "message": self.message,
            "details": self.details,
        }


class ToolError(Exception):
    """Raised inside services; converted to a ToolFailure at the tool boundary."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool | None = None,
        **details: Any,
    ) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code
        self.message = message
        self.retryable = (
            error_code in RETRYABLE_CODES if retryable is None else retryable
        )
        self.details = details

    def as_failure(self) -> ToolFailure:
        return ToolFailure(
            error_code=self.error_code,
            message=self.message,
            retryable=self.retryable,
            details=self.details,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.as_failure().as_dict()


def ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}
