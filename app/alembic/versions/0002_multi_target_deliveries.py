"""Create inbound_email_deliveries table for multi-target consumer fanout.

Revision ID: 0002_multi_target_deliveries
Revises: 0001_inbound_emails
Create Date: 2026-11-26 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_multi_target_deliveries"
down_revision: Union[str, None] = "0001_inbound_emails"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_email_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=False),
        sa.Column("consumer", sa.String(length=32), nullable=False),
        sa.Column("dispatch_status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("consumer_ref", sa.String(length=256), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["inbound_emails.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inbound_email_deliveries_email_id",
        "inbound_email_deliveries",
        ["email_id"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_email_deliveries_consumer",
        "inbound_email_deliveries",
        ["consumer"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_email_deliveries_dispatch_status",
        "inbound_email_deliveries",
        ["dispatch_status"],
        unique=False,
    )
    op.create_index(
        "uq_inbound_email_deliveries_email_consumer",
        "inbound_email_deliveries",
        ["email_id", "consumer"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_inbound_email_deliveries_email_consumer", table_name="inbound_email_deliveries")
    op.drop_index("ix_inbound_email_deliveries_dispatch_status", table_name="inbound_email_deliveries")
    op.drop_index("ix_inbound_email_deliveries_consumer", table_name="inbound_email_deliveries")
    op.drop_index("ix_inbound_email_deliveries_email_id", table_name="inbound_email_deliveries")
    op.drop_table("inbound_email_deliveries")
