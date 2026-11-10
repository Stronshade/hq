"""SQLAlchemy models for schema `agent` (annotated) and light frontapp reads."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ap_inbox.db import Base


class MessageAnnotation(Base):
    __tablename__ = "message_annotations"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_annotations_message_id"),
        Index("ix_message_annotations_doc_type", "doc_type"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=Decimal("0"))
    rationale: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InvoiceField(Base):
    __tablename__ = "invoice_fields"
    __table_args__ = (
        Index("ix_invoice_fields_vendor", "vendor"),
        Index("ix_invoice_fields_invoice_number", "invoice_number"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attachment_fil_id: Mapped[str | None] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(512))
    invoice_number: Mapped[str | None] = mapped_column(String(256))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(16))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    po_number: Mapped[str | None] = mapped_column(String(256))
    field_confidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    overall_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    error_detail: Mapped[str | None] = mapped_column(Text)
    raw_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AttachmentStatus(Base):
    __tablename__ = "attachment_status"
    __table_args__ = (
        UniqueConstraint("fil_id", "message_id", name="uq_attachment_status_fil_message"),
        Index("ix_attachment_status_status", "status"),
        Index("ix_attachment_status_doc_type", "doc_type"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fil_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(1024))
    content_type: Mapped[str | None] = mapped_column(String(256))
    is_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(Text)
    doc_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    doc_rationale: Mapped[str | None] = mapped_column(Text)
    doc_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VendorChaseEvent(Base):
    __tablename__ = "vendor_chase_events"
    __table_args__ = (
        Index("ix_vendor_chase_events_vendor", "vendor"),
        Index("ix_vendor_chase_events_stage", "stage"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(512))
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    referenced_invoice_numbers: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'[]'::jsonb")
    )
    invoice_present_in_inbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=Decimal("0"))
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = {"schema": "agent"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    failures: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))
    detail: Mapped[str | None] = mapped_column(Text)


class DocumentChunk(Base):
    """Chunk + embedding store. Embeddings are JSON float arrays; cosine is in-app (no pgvector)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "message_id",
            "fil_id",
            "chunk_index",
            name="uq_document_chunks_source_msg_fil_idx",
        ),
        Index("ix_document_chunks_message_id", "message_id"),
        Index("ix_document_chunks_source_type", "source_type"),
        Index("ix_document_chunks_fil_id", "fil_id"),
        {"schema": "agent"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # message_body | attachment_text
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fil_id: Mapped[str | None] = mapped_column(String(64))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
