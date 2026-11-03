"""SQLAlchemy definitions for persisted payment records."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Numeric, Text
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


class ProcessorOperationRecord(Base):
    """Provider-owned evidence, independent of the internal payments table."""

    __tablename__ = "processor_operations"
    __table_args__ = (
        CheckConstraint(
            "(status = 'succeeded' AND transaction_id IS NOT NULL) OR "
            "(status = 'declined' AND transaction_id IS NULL)",
            name="ck_processor_operations_outcome",
        ),
        CheckConstraint(
            "amount > 0 AND amount < 'Infinity'::numeric "
            "AND amount = trunc(amount, 2)",
            name="ck_processor_operations_amount",
        ),
        CheckConstraint("currency = 'USD'", name="ck_processor_operations_currency"),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_id: Mapped[UUID] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    transaction_id: Mapped[UUID | None] = mapped_column(unique=True)
