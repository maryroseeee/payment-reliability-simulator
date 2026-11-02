"""Database operations for payment records."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from payment_reliability.models import PaymentRecord
from payment_reliability.schemas import Payment, PaymentRequest


class PaymentStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def claim(self, idempotency_key: str, request: PaymentRequest) -> Payment | None:
        """Commit a new processing record, or return None if the key is taken."""
        payment = Payment(amount=request.amount, currency=request.currency)

        statement = (
            insert(PaymentRecord)
            .values(
                id=payment.id,
                idempotency_key=idempotency_key,
                amount=payment.amount,
                currency=payment.currency,
                status=payment.status,
            )
            .on_conflict_do_nothing(index_elements=[PaymentRecord.idempotency_key])
            .returning(PaymentRecord.id)
        )

        with self.engine.begin() as connection:
            inserted_id = connection.execute(statement).scalar_one_or_none()

        # The transaction commits before a successful claim is returned.
        if inserted_id is None:
            return None
        return payment

    def get_by_idempotency_key(self, idempotency_key: str) -> Payment | None:
        """Return the stored payment, or None when the key does not exist."""
        statement = select(PaymentRecord).where(
            PaymentRecord.idempotency_key == idempotency_key
        )

        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()

        if row is None:
            return None
        return Payment.model_validate(row)

    def mark_succeeded(
        self, payment_id: UUID, processor_transaction_id: UUID
    ) -> Payment | None:
        """Commit processing -> succeeded, or return None if it cannot apply."""
        if processor_transaction_id is None:
            raise ValueError("Successful payments require a processor transaction ID.")

        statement = (
            update(PaymentRecord)
            .where(
                PaymentRecord.id == payment_id,
                PaymentRecord.status == "processing",
            )
            .values(
                status="succeeded", processor_transaction_id=processor_transaction_id
            )
            .returning(PaymentRecord)
        )

        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            payment = Payment.model_validate(row)

        return payment

    def mark_declined(self, payment_id: UUID) -> Payment | None:
        """Commit processing -> declined, or return None if it cannot apply."""
        statement = (
            update(PaymentRecord)
            .where(
                PaymentRecord.id == payment_id,
                PaymentRecord.status == "processing",
            )
            .values(status="declined", processor_transaction_id=None)
            .returning(PaymentRecord)
        )

        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            payment = Payment.model_validate(row)

        return payment
