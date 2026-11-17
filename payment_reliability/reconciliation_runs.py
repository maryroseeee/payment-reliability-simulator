"""Append and read saved reconciliation observations without changing financial records."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from payment_reliability.models import PaymentRecord, ReconciliationRunRecord
from payment_reliability.reconciliation import ReconciliationReport, reconcile_successful_payment
from payment_reliability.reconciliation_store import ReconciliationStore


class ReconciliationRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    payment_id: UUID
    checked_at: AwareDatetime
    report: ReconciliationReport

    @model_validator(mode="after")
    def validate_report_payment(self) -> "ReconciliationRun":
        if self.report.payment_id != self.payment_id:
            raise ValueError("The saved report must belong to the run's payment.")
        return self


class ReconciliationRunStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def append(self, report: ReconciliationReport, checked_at: datetime) -> ReconciliationRun:
        """Commit a new run; never replace an earlier run for this payment."""
        run = ReconciliationRun(
            payment_id=report.payment_id, checked_at=checked_at, report=report
        )
        with self.engine.begin() as connection:
            connection.execute(insert(ReconciliationRunRecord).values(
                id=run.id,
                payment_id=run.payment_id,
                checked_at=run.checked_at,
                report=run.report.model_dump(mode="json"),
            ))
        return run

    def list_for_key(self, idempotency_key: str) -> list[ReconciliationRun] | None:
        """Read stored reports in observation order, or None for an unknown payment."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            payment_id = connection.execute(
                select(PaymentRecord.id).where(PaymentRecord.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if payment_id is None:
                return None
            rows = connection.execute(
                select(ReconciliationRunRecord)
                .where(ReconciliationRunRecord.payment_id == payment_id)
                .order_by(ReconciliationRunRecord.checked_at, ReconciliationRunRecord.id)
            ).mappings().all()
            runs = [ReconciliationRun.model_validate(row) for row in rows]
        return runs


def create_reconciliation_run(
    reader: ReconciliationStore,
    history: ReconciliationRunStore,
    idempotency_key: str,
) -> ReconciliationRun | None:
    """Observe, compare, then save that observation in a separate write transaction."""
    snapshot = reader.load(idempotency_key)
    if snapshot is None:
        return None
    report = reconcile_successful_payment(
        payment=snapshot.payment, provider=snapshot.provider, ledger=snapshot.ledger
    )
    return history.append(report, snapshot.checked_at)
