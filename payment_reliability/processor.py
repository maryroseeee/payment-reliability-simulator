"""A simulated provider with durable successful or declined outcomes."""

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from payment_reliability.processor_store import ProcessorStore


class ProcessorError(RuntimeError):
    """The simulator cannot confirm the requested payment."""


class ProcessorTimeoutError(ProcessorError):
    """The processor recorded an outcome, but its response was lost."""


class ProcessorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    payment_id: UUID
    amount: Decimal = Field(gt=0, decimal_places=2)
    # Describe what the provider recorded so the service can detect a mismatch.
    currency: str
    status: Literal["succeeded", "declined"] = "succeeded"
    transaction_id: UUID | None = None

    @model_validator(mode="after")
    def validate_transaction_id(self) -> "ProcessorResult":
        if self.status == "succeeded" and self.transaction_id is None:
            raise ValueError("Successful results require a transaction ID.")
        if self.status == "declined" and self.transaction_id is not None:
            raise ValueError("Declined results cannot include a transaction ID.")
        return self


class SimulatedPaymentProcessor:
    def __init__(
        self, store: ProcessorStore, *, decline: bool = False, lose_response: bool = False
    ):
        self.store = store
        self.decline = decline
        self.lose_response = lose_response

    @property
    def charge_count(self) -> int:
        """Number of successful charges recorded in this provider store."""
        return self.store.count_charges()

    def process(
        self,
        *,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ProcessorResult:
        proposed = ProcessorResult(
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            status="declined" if self.decline else "succeeded",
            transaction_id=None if self.decline else uuid4(),
        )
        record, created = self.store.record_operation(
            idempotency_key=idempotency_key,
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            status=proposed.status,
            transaction_id=proposed.transaction_id,
        )
        original_signature = (
            record["payment_id"], record["amount"], record["currency"]
        )
        if original_signature != (payment_id, amount, currency):
            raise ProcessorError(
                "The processor key was already used with different details."
            )

        result = ProcessorResult.model_validate(record)
        # The provider transaction has committed before this response is lost.
        if created and self.lose_response:
            raise ProcessorTimeoutError(
                "The processor recorded the outcome, but its response was lost."
            )
        return result

    def get_result(self, idempotency_key: str) -> ProcessorResult | None:
        """Look up a recorded outcome without processing another payment."""
        record = self.store.get_by_idempotency_key(idempotency_key)
        if record is None:
            return None
        return ProcessorResult.model_validate(record)
