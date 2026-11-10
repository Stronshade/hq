"""Add agent.document_chunks for in-app embedding retrieval (JSONB, no pgvector).

Revision ID: 20261109_0002
Revises: 20260309_0001
Create Date: 2026-11-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261109_0002"
down_revision: Union[str, None] = "20260309_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fil_id", sa.String(64)),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "embedding",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("model", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "source_type",
            "message_id",
            "fil_id",
            "chunk_index",
            name="uq_document_chunks_source_msg_fil_idx",
        ),
        schema="agent",
    )
    op.create_index(
        "ix_document_chunks_message_id",
        "document_chunks",
        ["message_id"],
        schema="agent",
    )
    op.create_index(
        "ix_document_chunks_source_type",
        "document_chunks",
        ["source_type"],
        schema="agent",
    )
    op.create_index(
        "ix_document_chunks_fil_id",
        "document_chunks",
        ["fil_id"],
        schema="agent",
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_fil_id", table_name="document_chunks", schema="agent")
    op.drop_index("ix_document_chunks_source_type", table_name="document_chunks", schema="agent")
    op.drop_index("ix_document_chunks_message_id", table_name="document_chunks", schema="agent")
    op.drop_table("document_chunks", schema="agent")
