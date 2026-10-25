"""Request and response models for the HTTP API.

Note what is absent from every request model: `customer_id`, `reviewer_id`,
`approval_status`. Those are derived from the bearer token on the server.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateTicketRequest(ApiModel):
    customer_message: Annotated[str, Field(min_length=3, max_length=4000)]
    order_id: Annotated[str, Field(max_length=64)] | None = None


class TicketResponse(ApiModel):
    model_config = ConfigDict(extra="allow")

    ticket_id: str
    status: str
    category: str


class StartRunRequest(ApiModel):
    # Graph shape, for the A/B measurement in evals/. See docs/evaluation.md.
    variant: Literal["specialists", "parallel", "baseline"] = "specialists"


class RunAcceptedResponse(ApiModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    status: str
    stage: str


class ApprovalDecisionRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    note: Annotated[str, Field(max_length=1000)] | None = None


class CustomerMessageRequest(ApiModel):
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    restart: bool = True


class FaultRequest(ApiModel):
    service: Literal["payments", "shipping", "ticketing"]
    mode: Literal[
        "none", "timeout", "accept_then_timeout", "unavailable", "reject", "slow", "pending"
    ]
    remaining_uses: Annotated[int, Field(ge=0, le=10)] = 1


class ErrorResponse(ApiModel):
    success: bool = False
    error_code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
