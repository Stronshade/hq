"""Add per-attachment doc_type fields on agent.attachment_status.

Revision ID: 20261109_0003
Revises: 20261109_0002
Create Date: 2026-11-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0003"
down_revision: Union[str, None] = "20261109_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("attachment_status", sa.Column("doc_type", sa.Text(), nullable=True), schema="agent")
    op.add_column(
        "attachment_status",
        sa.Column("doc_confidence", sa.Numeric(5, 4), nullable=True),
        schema="agent",
    )
    op.add_column("attachment_status", sa.Column("doc_rationale", sa.Text(), nullable=True), schema="agent")
    op.add_column("attachment_status", sa.Column("doc_model", sa.Text(), nullable=True), schema="agent")
    op.create_index(
        "ix_attachment_status_doc_type",
        "attachment_status",
        ["doc_type"],
        schema="agent",
    )


def downgrade() -> None:
    op.drop_index("ix_attachment_status_doc_type", table_name="attachment_status", schema="agent")
    op.drop_column("attachment_status", "doc_model", schema="agent")
    op.drop_column("attachment_status", "doc_rationale", schema="agent")
    op.drop_column("attachment_status", "doc_confidence", schema="agent")
    op.drop_column("attachment_status", "doc_type", schema="agent")
