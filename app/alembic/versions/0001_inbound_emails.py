"""Create inbound_emails and inbound_email_attachments tables.

Revision ID: 0001_inbound_emails
Revises:
Create Date: 2026-11-20 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_inbound_emails"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=512), nullable=False),
        sa.Column("from_address", sa.String(length=512), nullable=False),
        sa.Column("to_address", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.Text(), server_default="", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("html_excerpt", sa.Text(), nullable=True),
        sa.Column("raw_excerpt", sa.Text(), nullable=True),
        sa.Column("attachments_meta", sa.JSON(), nullable=False),
        sa.Column("jev_verdict", sa.JSON(), nullable=True),
        sa.Column("route_target", sa.String(length=64), server_default="unclassified", nullable=False),
        sa.Column("dispatch_status", sa.String(length=32), server_default="received", nullable=False),
        sa.Column("consumer_ref", sa.String(length=256), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inbound_emails_message_id", "inbound_emails", ["message_id"], unique=True)
    op.create_index("ix_inbound_emails_from_address", "inbound_emails", ["from_address"], unique=False)
    op.create_index("ix_inbound_emails_received_at", "inbound_emails", ["received_at"], unique=False)
    op.create_index("ix_inbound_emails_route_target", "inbound_emails", ["route_target"], unique=False)
    op.create_index("ix_inbound_emails_dispatch_status", "inbound_emails", ["dispatch_status"], unique=False)

    op.create_table(
        "inbound_email_attachments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_base64", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["inbound_emails.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inbound_email_attachments_email_id",
        "inbound_email_attachments",
        ["email_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_email_attachments_email_id", table_name="inbound_email_attachments")
    op.drop_table("inbound_email_attachments")

    op.drop_index("ix_inbound_emails_dispatch_status", table_name="inbound_emails")
    op.drop_index("ix_inbound_emails_route_target", table_name="inbound_emails")
    op.drop_index("ix_inbound_emails_received_at", table_name="inbound_emails")
    op.drop_index("ix_inbound_emails_from_address", table_name="inbound_emails")
    op.drop_index("ix_inbound_emails_message_id", table_name="inbound_emails")
    op.drop_table("inbound_emails")
