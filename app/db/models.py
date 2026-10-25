"""Application tables.

Two rules drive this schema:

1. Money is always an integer in minor units (paise). There are no floats.
2. The refund ledger lives here, in tables with explicit constraints -- never
   in a LangGraph checkpoint. A checkpoint can be replayed; a ledger cannot.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class AuthToken(Base, TimestampMixin):
    """Opaque bearer tokens for the demo.

    The whole point of this table: the *server* maps a token to a subject. A
    model or a request body can never choose which customer it represents.
    """

    __tablename__ = "auth_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # customer|reviewer
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (CheckConstraint("role IN ('customer','reviewer')", name="ck_auth_role"),)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cus"))
    external_auth_subject: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(190), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(String(32), default="standard")

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


# --------------------------------------------------------------------------- #
# Commerce
# --------------------------------------------------------------------------- #
class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ord"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    paid_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_method: Mapped[str] = mapped_column(String(32), default="card")

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order")

    __table_args__ = (
        CheckConstraint("paid_amount_minor >= 0", name="ck_order_amount_nonneg"),
        CheckConstraint(
            "status IN ('placed','packed','shipped','in_transit','delivered','cancelled','lost')",
            name="ck_order_status",
        ),
    )


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("itm"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str] = mapped_column(String(190), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="electronics")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    paid_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (CheckConstraint("quantity > 0", name="ck_item_qty_positive"),)


class Shipment(Base, TimestampMixin):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("shp"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    carrier: Mapped[str] = mapped_column(String(64), default="BlueDart")
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_scan_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Return(Base, TimestampMixin):
    __tablename__ = "returns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ret"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    order_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(190), default="not_specified")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inspection_status: Mapped[str] = mapped_column(String(32), default="pending")

    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','label_issued','in_transit','received','closed','cancelled')",
            name="ck_return_status",
        ),
        CheckConstraint(
            "inspection_status IN ('pending','passed','failed','not_applicable')",
            name="ck_inspection_status",
        ),
    )


class Payment(Base, TimestampMixin):
    """The captured payment for an order -- the thing a refund draws down."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pay"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="captured")
    captured_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    provider_payment_id: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('captured','partially_refunded','refunded','failed','voided')",
            name="ck_payment_status",
        ),
    )


# --------------------------------------------------------------------------- #
# Support
# --------------------------------------------------------------------------- #
class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tkt"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="unclassified")
    status: Mapped[str] = mapped_column(String(32), default="open")
    summary: Mapped[str] = mapped_column(Text, default="")
    customer_message: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','investigating','awaiting_customer','awaiting_approval',"
            "'resolved','escalated','rejected')",
            name="ck_ticket_status",
        ),
    )


class WorkflowRun(Base, TimestampMixin):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("run"))
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    authenticated_customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(48), default="created")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    waiting_ms: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd_micros: Mapped[int] = mapped_column(Integer, default=0)
    final_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','awaiting_approval','awaiting_customer',"
            "'completed','failed','escalated')",
            name="ck_run_status",
        ),
    )


# --------------------------------------------------------------------------- #
# Policy / RAG
# --------------------------------------------------------------------------- #
class PolicyDocument(Base, TimestampMixin):
    __tablename__ = "policy_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(190), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    chunks: Mapped[list[PolicyChunk]] = relationship(back_populates="document")


class PolicyChunk(Base, TimestampMixin):
    __tablename__ = "policy_chunks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("policy_documents.id"), nullable=False, index=True
    )
    section_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    heading: Mapped[str] = mapped_column(String(190), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Stored as JSON so the project runs on plain Postgres or SQLite; see
    # docs/architecture.md for the pgvector upgrade path.
    embedding: Mapped[list] = mapped_column(JSON, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    document: Mapped[PolicyDocument] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_policy_chunk_doc_section", "document_id", "section_ref"),)


# --------------------------------------------------------------------------- #
# Proposal / approval / execution
# --------------------------------------------------------------------------- #
class ActionProposal(Base, TimestampMixin):
    """Immutable once written. `payload_hash` is what an approval binds to."""

    __tablename__ = "action_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("prp"))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    policy_refs: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "payload_hash", name="uq_proposal_run_payload"),
        CheckConstraint(
            "status IN ('pending_approval','approved','rejected','expired',"
            "'executed','invalidated','failed')",
            name="ck_proposal_status",
        ),
    )


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("apv"))
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("action_proposals.id"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_approval_one_per_proposal"),
        CheckConstraint(
            "decision IN ('pending','approved','rejected','expired')", name="ck_approval_decision"
        ),
    )


class RefundOperation(Base, TimestampMixin):
    """One row per attempted money movement.

    The unique index on `idempotency_key` is the durable guarantee that a
    retry, a restart or two concurrent workers cannot create a second refund.
    """

    __tablename__ = "refund_operations"

    # Statuses that still hold a claim on the refundable balance.
    IN_FLIGHT = ("reserved", "submitted", "unknown", "pending_reconciliation")
    COMMITTED = ("succeeded",)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rfo"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("action_proposals.id"), nullable=False)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(96), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    reconcile_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_refund_idempotency_key"),
        UniqueConstraint("proposal_id", name="uq_refund_one_per_proposal"),
        CheckConstraint("amount_minor > 0", name="ck_refund_amount_positive"),
        CheckConstraint(
            "status IN ('created','reserved','submitted','succeeded','failed',"
            "'unknown','pending_reconciliation','cancelled')",
            name="ck_refund_status",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    redacted_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# --------------------------------------------------------------------------- #
# Mock provider ledgers.
# Logically a different system; kept in the same database only so the demo
# runs from one `docker compose up`. Nothing in app/ reads these directly --
# access goes through mock_services/.
# --------------------------------------------------------------------------- #
class ProviderRefund(Base):
    __tablename__ = "mock_provider_refunds"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    payment_reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="processing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_provider_refund_key"),
        CheckConstraint(
            "status IN ('processing','succeeded','failed')", name="ck_provider_refund_status"
        ),
    )


class FaultInjection(Base):
    """Demo-only switchboard used by the dashboard to simulate provider faults."""

    __tablename__ = "mock_fault_injection"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="none")
    remaining_uses: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
