"""Payment decisions independent of HTTP handling."""

from payment_reliability.processor import (
    ProcessorError,
    ProcessorResult,
    SimulatedPaymentProcessor,
)
from payment_reliability.schemas import Payment, PaymentRequest
from payment_reliability.store import PaymentStore


class IdempotencyConflictError(Exception):
    """A key was reused with different payment details."""


class PaymentInProgressError(Exception):
    """The matching payment has no completed outcome yet."""


class PaymentRecordMissingError(RuntimeError):
    """The requested payment record could not be found."""


class PaymentStateError(RuntimeError):
    """A confirmed payment outcome could not be represented consistently."""


class PaymentService:
    def __init__(self, store: PaymentStore, processor: SimulatedPaymentProcessor):
        self.store = store
        self.processor = processor

    def process_payment(self, idempotency_key: str, request: PaymentRequest) -> Payment:
        """Reserve a payment, obtain a confirmed outcome, and save it."""
        claimed = self.store.claim(idempotency_key, request)
        if claimed is None:
            return self.resolve_retry(idempotency_key, request)

        confirmation = self.processor.process(
            payment_id=claimed.id,
            amount=claimed.amount,
            currency=claimed.currency,
            idempotency_key=idempotency_key,
        )
        return self._save_outcome(idempotency_key, claimed, confirmation)

    def recover_payment(self, idempotency_key: str) -> Payment:
        """Resolve an existing payment using a read-only processor lookup."""
        original = self.store.get_by_idempotency_key(idempotency_key)
        if original is None:
            raise PaymentRecordMissingError("The payment record could not be found.")

        completed = self._completed_payment(original)
        if completed is not None:
            return completed

        confirmation = self.processor.get_result(idempotency_key)
        if confirmation is None:
            return original

        return self._save_outcome(idempotency_key, original, confirmation)

    def _save_outcome(
        self, idempotency_key: str, payment: Payment, confirmation: ProcessorResult
    ) -> Payment:
        """Use the same guarded state transitions for processing and recovery."""
        if confirmation.status == "succeeded" and confirmation.transaction_id is not None:
            completed = self.store.mark_succeeded(payment.id, confirmation.transaction_id)
        elif confirmation.status == "declined" and confirmation.transaction_id is None:
            completed = self.store.mark_declined(payment.id)
        else:
            raise ProcessorError("The processor did not confirm a valid payment outcome.")
        if completed is not None:
            return completed

        # Another caller may have saved this same confirmation first.
        saved = self.store.get_by_idempotency_key(idempotency_key)
        if (
            saved is not None
            and saved.id == payment.id
            and saved.amount == payment.amount
            and saved.currency == payment.currency
            and saved.status == confirmation.status
            and saved.processor_transaction_id == confirmation.transaction_id
        ):
            completed = self._completed_payment(saved)
            if completed is not None:
                return completed

        raise PaymentStateError("The confirmed payment outcome could not be saved.")

    def resolve_retry(self, idempotency_key: str, request: PaymentRequest) -> Payment:
        """Resolve a retry after its attempt to claim the key was unsuccessful."""
        original = self.store.get_by_idempotency_key(idempotency_key)

        if original is None:
            raise PaymentRecordMissingError(
                "The previously claimed payment record could not be found."
            )

        if original.amount != request.amount or original.currency != request.currency:
            raise IdempotencyConflictError(
                "This idempotency key was already used with different payment details."
            )

        completed = self._completed_payment(original)
        if completed is not None:
            return completed

        raise PaymentInProgressError(
            "A payment with this idempotency key is still processing."
        )

    def _completed_payment(self, payment: Payment) -> Payment | None:
        """Return a valid final outcome, or None while the payment is unresolved."""
        if payment.status == "succeeded":
            if payment.processor_transaction_id is None:
                raise PaymentStateError(
                    "The stored success has no processor transaction ID."
                )
            return payment

        if payment.status == "declined":
            if payment.processor_transaction_id is not None:
                raise PaymentStateError(
                    "The stored decline unexpectedly has a processor transaction ID."
                )
            return payment

        return None
