"""SQLAlchemy definitions for persisted payment records."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
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


class LedgerPostingRecord(Base):
    """One accounting event per successful payment in this first ledger version."""

    __tablename__ = "ledger_postings"
    __table_args__ = (
        CheckConstraint(
            "amount > 0 AND amount < 'Infinity'::numeric "
            "AND amount = trunc(amount, 2)",
            name="ck_ledger_postings_amount",
        ),
        CheckConstraint("currency = 'USD'", name="ck_ledger_postings_currency"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(Text)


class LedgerEntryRecord(Base):
    """At most one debit and one credit per posting, on the expected accounts."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "(account = 'processor_clearing' AND direction = 'debit') OR "
            "(account = 'merchant_payable' AND direction = 'credit')",
            name="ck_ledger_entries_account_direction",
        ),
        CheckConstraint(
            "amount > 0 AND amount < 'Infinity'::numeric "
            "AND amount = trunc(amount, 2)",
            name="ck_ledger_entries_amount",
        ),
    )

    posting_id: Mapped[UUID] = mapped_column(
        ForeignKey("ledger_postings.id"), primary_key=True
    )
    direction: Mapped[str] = mapped_column(Text, primary_key=True)
    account: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric)


class ReconciliationRunRecord(Base):
    """A saved observation; a payment can have many independently identified runs."""

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(report) = 'object' AND "
            "(report ->> 'payment_id') IS NOT NULL AND "
            "(report ->> 'payment_id') = payment_id::text",
            name="ck_reconciliation_runs_report_payment",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict] = mapped_column(JSONB)
