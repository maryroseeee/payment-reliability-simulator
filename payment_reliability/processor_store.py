"""Durable provider records saved in transactions separate from payment updates."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine, RowMapping

from payment_reliability.models import ProcessorOperationRecord


class ProcessorStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def record_operation(
        self,
        *,
        idempotency_key: str,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        status: str,
        transaction_id: UUID | None,
    ) -> tuple[RowMapping, bool]:
        """Commit one outcome per key; return its record and whether it was new."""
        statement = (
            insert(ProcessorOperationRecord)
            .values(
                idempotency_key=idempotency_key,
                payment_id=payment_id,
                amount=amount,
                currency=currency,
                status=status,
                transaction_id=transaction_id,
            )
            .on_conflict_do_nothing(
                index_elements=[ProcessorOperationRecord.idempotency_key]
            )
            .returning(ProcessorOperationRecord)
        )

        with self.engine.begin() as connection:
            record = connection.execute(statement).mappings().one_or_none()
            created = record is not None
            if record is None:
                # A competing request may have committed this key first.
                record = connection.execute(
                    select(ProcessorOperationRecord).where(
                        ProcessorOperationRecord.idempotency_key == idempotency_key
                    )
                ).mappings().one()

        # Leave the transaction block before returning or simulating a timeout.
        return record, created

    def get_by_idempotency_key(self, idempotency_key: str) -> RowMapping | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(ProcessorOperationRecord).where(
                    ProcessorOperationRecord.idempotency_key == idempotency_key
                )
            ).mappings().one_or_none()

    def count_charges(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                select(func.count())
                .select_from(ProcessorOperationRecord)
                .where(ProcessorOperationRecord.status == "succeeded")
            ).scalar_one()
