"""Validated schemas for specialist agent output.

Model output is parsed into these before anything else looks at it. A field the
model is not allowed to decide (reviewer identity, approval status, who the
customer is) does not exist here, so it cannot be smuggled in.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ComplaintCategory = Literal[
    "order_status", "refund_request", "damaged_item", "out_of_scope", "unclear"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
class Classification(StrictModel):
    category: ComplaintCategory
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    order_reference_mentioned: str | None = None
    reasoning: Annotated[str, Field(max_length=600)] = ""

    @field_validator("order_reference_mentioned")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


# --------------------------------------------------------------------------- #
# Investigation
# --------------------------------------------------------------------------- #
class EvidenceItem(StrictModel):
    ref: Annotated[str, Field(max_length=96)]
    kind: Literal["order", "return", "payment", "shipment", "ticket", "refund_operation"]
    summary: Annotated[str, Field(max_length=400)]


class InvestigationResult(StrictModel):
    order_id: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list)
    read_errors: list[dict[str, Any]] = Field(default_factory=list)
    summary: Annotated[str, Field(max_length=1200)] = ""

    @property
    def evidence_refs(self) -> list[str]:
        return [item.ref for item in self.evidence]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class PolicyCitation(StrictModel):
    citation: Annotated[str, Field(max_length=96)]
    heading: Annotated[str, Field(max_length=190)]
    relevance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    excerpt: Annotated[str, Field(max_length=600)] = ""


class PolicyResult(StrictModel):
    citations: list[PolicyCitation] = Field(default_factory=list)
    interpretation: Annotated[str, Field(max_length=1200)] = ""
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    policy_established: bool = False
    untrusted_content_detected: bool = False

    @property
    def refs(self) -> list[str]:
        return [item.citation for item in self.citations]


# --------------------------------------------------------------------------- #
# Resolution proposal -- the structured output the brief specifies
# --------------------------------------------------------------------------- #
ResolutionAction = Literal[
    "answer_only",
    "request_refund_approval",
    "request_information",
    "escalate_to_human",
]


class ResolutionProposal(StrictModel):
    action: ResolutionAction
    order_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    reason: Annotated[str, Field(max_length=400)]
    customer_message: Annotated[str, Field(max_length=1800)]
    evidence_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    missing_information: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list)
    escalation_reason: Annotated[str, Field(max_length=400)] | None = None

    @field_validator("amount_minor")
    @classmethod
    def _amount_sane(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("amount_minor must be a positive integer in minor units")
        if value > 100_000_000:
            raise ValueError("amount_minor is implausibly large")
        return value

    def requires_amount(self) -> bool:
        return self.action == "request_refund_approval"


class ValidationOutcome(StrictModel):
    """Result of running the proposal past deterministic business rules."""

    route: Literal["answer_only", "request_approval", "request_information", "escalate"]
    blockers: list[dict[str, str]] = Field(default_factory=list)
    adjusted_amount_minor: int | None = None
    escalation_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
