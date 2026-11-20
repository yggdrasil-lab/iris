from datetime import datetime
from typing import Any, Optional
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class InboundEmail(Base):
    """Persistent audit ledger record for an email ingested via Iris."""

    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True, nullable=False)
    from_address: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    to_address: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    text_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    html_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachments_meta: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    jev_verdict: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    route_target: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="unclassified")
    dispatch_status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="received")
    consumer_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    attachments: Mapped[list["InboundEmailAttachment"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="InboundEmailAttachment.id",
    )


class InboundEmailAttachment(Base):
    """Stored attachment payload linked to an InboundEmail for downstream replay."""

    __tablename__ = "inbound_email_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("inbound_emails.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_base64: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    email: Mapped["InboundEmail"] = relationship(back_populates="attachments")
