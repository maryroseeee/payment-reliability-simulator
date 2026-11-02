"""Payment decisions independent of HTTP handling."""

from payment_reliability.processor import ProcessorError, SimulatedPaymentProcessor
from payment_reliability.schemas import Payment, PaymentRequest
from payment_reliability.store import PaymentStore


class IdempotencyConflictError(Exception):
    """A key was reused with different payment details."""


class PaymentInProgressError(Exception):
    """The matching payment has no completed outcome yet."""


class PaymentRecordMissingError(RuntimeError):
    """A retry's previously claimed payment record could not be found."""


class PaymentStateError(RuntimeError):
    """A confirmed payment outcome could not be represented consistently."""


class PaymentService:
    def __init__(self, store: PaymentStore, processor: SimulatedPaymentProcessor):
        self.store = store
        self.processor = processor

    def process_payment(self, idempotency_key: str, request: PaymentRequest) -> Payment:
        """Reserve a payment, obtain confirmation, and save its successful outcome."""
        claimed = self.store.claim(idempotency_key, request)
        if claimed is None:
            return self.resolve_retry(idempotency_key, request)

        confirmation = self.processor.process(
            payment_id=claimed.id,
            amount=claimed.amount,
            currency=claimed.currency,
            idempotency_key=idempotency_key,
        )
        if confirmation.status != "succeeded" or confirmation.transaction_id is None:
            raise ProcessorError("The processor did not confirm a successful payment.")

        completed = self.store.mark_succeeded(claimed.id, confirmation.transaction_id)
        if completed is None:
            raise PaymentStateError("The confirmed payment outcome could not be saved.")
        return completed

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

        if original.status == "succeeded":
            if original.processor_transaction_id is None:
                raise PaymentStateError(
                    "The stored success has no processor transaction ID."
                )
            return original

        raise PaymentInProgressError(
            "A payment with this idempotency key is still processing."
        )
