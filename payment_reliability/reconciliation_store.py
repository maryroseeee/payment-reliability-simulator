"""Load payment, provider, and ledger records from one read-only database view."""

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from payment_reliability.models import (
    LedgerEntryRecord,
    LedgerPostingRecord,
    PaymentRecord,
    ProcessorOperationRecord,
)
from payment_reliability.processor import ProcessorResult
from payment_reliability.reconciliation import (
    LedgerEntrySnapshot,
    LedgerSnapshot,
    ReconciliationReport,
    reconcile_successful_payment,
)
from payment_reliability.schemas import Payment


class ReconciliationSnapshot(BaseModel):
    """The three sources as observed in the same database transaction snapshot."""

    model_config = ConfigDict(frozen=True)

    checked_at: AwareDatetime
    payment: Payment
    provider: ProcessorResult | None
    ledger: LedgerSnapshot | None


class ReconciliationStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def load(self, idempotency_key: str) -> ReconciliationSnapshot | None:
        """Return committed records, or None when the internal payment is missing."""
        with self.engine.begin() as connection:
            # Set these modes before the first SELECT; they apply only to this transaction.
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            payment_row = connection.execute(
                select(PaymentRecord, func.statement_timestamp().label("checked_at"))
                .where(PaymentRecord.idempotency_key == idempotency_key)
            ).mappings().one_or_none()
            if payment_row is None:
                return None
            payment = Payment.model_validate(payment_row)

            provider_row = connection.execute(
                select(ProcessorOperationRecord).where(
                    ProcessorOperationRecord.idempotency_key == idempotency_key
                )
            ).mappings().one_or_none()
            provider = None if provider_row is None else ProcessorResult.model_validate(provider_row)

            posting_row = connection.execute(
                select(LedgerPostingRecord).where(LedgerPostingRecord.payment_id == payment.id)
            ).mappings().one_or_none()
            ledger = None
            if posting_row is not None:
                entry_rows = connection.execute(
                    select(LedgerEntryRecord)
                    .where(LedgerEntryRecord.posting_id == posting_row["id"])
                    .order_by(LedgerEntryRecord.direction.desc())
                ).mappings().all()
                ledger = LedgerSnapshot(
                    **posting_row,
                    entries=tuple(LedgerEntrySnapshot.model_validate(row) for row in entry_rows),
                )

            snapshot = ReconciliationSnapshot(
                checked_at=payment_row["checked_at"], payment=payment, provider=provider, ledger=ledger
            )

        return snapshot


def reconcile_stored_payment(
    store: ReconciliationStore, idempotency_key: str
) -> ReconciliationReport | None:
    """Load and check a succeeded payment; return None for an unknown internal key."""
    snapshot = store.load(idempotency_key)
    if snapshot is None:
        return None
    return reconcile_successful_payment(
        payment=snapshot.payment, provider=snapshot.provider, ledger=snapshot.ledger
    )
