"""Create agent schema annotated tables

Revision ID: 20260309_0001
Revises:
Create Date: 2026-03-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260309_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")

    op.create_table(
        "message_annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("rationale", sa.Text()),
        sa.Column("model", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("error_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("message_id", name="uq_message_annotations_message_id"),
        schema="agent",
    )
    op.create_index("ix_message_annotations_doc_type", "message_annotations", ["doc_type"], schema="agent")

    op.create_table(
        "invoice_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_fil_id", sa.String(64)),
        sa.Column("vendor", sa.String(512)),
        sa.Column("invoice_number", sa.String(256)),
        sa.Column("amount", sa.Numeric(18, 4)),
        sa.Column("currency", sa.String(16)),
        sa.Column("invoice_date", sa.Date()),
        sa.Column("due_date", sa.Date()),
        sa.Column("po_number", sa.String(256)),
        sa.Column(
            "field_confidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("overall_confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("error_detail", sa.Text()),
        sa.Column("raw_excerpt", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema="agent",
    )
    op.create_index("ix_invoice_fields_vendor", "invoice_fields", ["vendor"], schema="agent")
    op.create_index("ix_invoice_fields_invoice_number", "invoice_fields", ["invoice_number"], schema="agent")

    op.create_table(
        "attachment_status",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fil_id", sa.String(64), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(1024)),
        sa.Column("content_type", sa.String(256)),
        sa.Column("is_inline", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("path", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("fil_id", "message_id", name="uq_attachment_status_fil_message"),
        schema="agent",
    )
    op.create_index("ix_attachment_status_status", "attachment_status", ["status"], schema="agent")

    op.create_table(
        "vendor_chase_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor", sa.String(512)),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column(
            "referenced_invoice_numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("invoice_present_in_inbox", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("rationale", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("error_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema="agent",
    )
    op.create_index("ix_vendor_chase_events_vendor", "vendor_chase_events", ["vendor"], schema="agent")
    op.create_index("ix_vendor_chase_events_stage", "vendor_chase_events", ["stage"], schema="agent")

    op.create_table(
        "pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "failures",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("detail", sa.Text()),
        schema="agent",
    )


def downgrade() -> None:
    op.drop_table("pipeline_runs", schema="agent")
    op.drop_index("ix_vendor_chase_events_stage", table_name="vendor_chase_events", schema="agent")
    op.drop_index("ix_vendor_chase_events_vendor", table_name="vendor_chase_events", schema="agent")
    op.drop_table("vendor_chase_events", schema="agent")
    op.drop_index("ix_attachment_status_status", table_name="attachment_status", schema="agent")
    op.drop_table("attachment_status", schema="agent")
    op.drop_index("ix_invoice_fields_invoice_number", table_name="invoice_fields", schema="agent")
    op.drop_index("ix_invoice_fields_vendor", table_name="invoice_fields", schema="agent")
    op.drop_table("invoice_fields", schema="agent")
    op.drop_index("ix_message_annotations_doc_type", table_name="message_annotations", schema="agent")
    op.drop_table("message_annotations", schema="agent")
    op.execute("DROP SCHEMA IF EXISTS agent CASCADE")
