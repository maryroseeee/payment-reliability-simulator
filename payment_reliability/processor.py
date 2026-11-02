"""An in-process provider with deterministic successful or declined outcomes."""

from decimal import Decimal
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator


class ProcessorError(RuntimeError):
    """The simulator cannot confirm the requested payment."""


class ProcessorTimeoutError(ProcessorError):
    """The processor recorded an outcome, but its response was lost."""


class ProcessorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    def __init__(self, *, decline: bool = False, lose_response: bool = False):
        self.decline = decline
        self.lose_response = lose_response
        self._lock = Lock()
        self._results: dict[
            str, tuple[tuple[UUID, Decimal, str], ProcessorResult]
        ] = {}

    @property
    def charge_count(self) -> int:
        """Number of successful simulated charges in this instance."""
        with self._lock:
            return sum(
                result.status == "succeeded" for _, result in self._results.values()
            )

    def process(
        self,
        *,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> ProcessorResult:
        signature = (payment_id, amount, currency)

        with self._lock:
            existing = self._results.get(idempotency_key)
            if existing is not None:
                original_signature, original_result = existing
                if signature != original_signature:
                    raise ProcessorError(
                        "The processor key was already used with different details."
                    )
                return original_result

            result = (
                ProcessorResult(status="declined")
                if self.decline
                else ProcessorResult(transaction_id=uuid4())
            )
            self._results[idempotency_key] = (signature, result)
            if self.lose_response:
                raise ProcessorTimeoutError(
                    "The processor recorded the outcome, but its response was lost."
                )
            return result

    def get_result(self, idempotency_key: str) -> ProcessorResult | None:
        """Look up a recorded outcome without processing another payment."""
        with self._lock:
            existing = self._results.get(idempotency_key)
            if existing is None:
                return None
            return existing[1]
