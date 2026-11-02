"""An in-process payment provider simulator for the first successful flow."""

from decimal import Decimal
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict


class ProcessorError(RuntimeError):
    """The simulator cannot confirm the requested payment."""


class ProcessorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["succeeded"] = "succeeded"
    transaction_id: UUID


class SimulatedPaymentProcessor:
    def __init__(self):
        self._lock = Lock()
        self._results: dict[
            str, tuple[tuple[UUID, Decimal, str], ProcessorResult]
        ] = {}

    @property
    def charge_count(self) -> int:
        """Number of successful simulated charges in this instance."""
        with self._lock:
            return len(self._results)

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

            result = ProcessorResult(transaction_id=uuid4())
            self._results[idempotency_key] = (signature, result)
            return result
