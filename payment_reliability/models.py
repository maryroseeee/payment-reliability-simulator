"""SQLAlchemy definitions for persisted payment records."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import Index, Numeric, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Collect the application's SQLAlchemy table definitions."""


class PaymentRecord(Base):
    """Map a payment and its creation request's key to a database row."""

    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "uq_payments_processor_transaction_id",
            "processor_transaction_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    processor_transaction_id: Mapped[UUID | None] = mapped_column()
