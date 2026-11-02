"""Service decisions tested with a stand-in store; no database is needed."""

from decimal import Decimal
from unittest.mock import Mock, call, create_autospec
from uuid import uuid4

import pytest

from payment_reliability.processor import (
    ProcessorError,
    ProcessorResult,
    SimulatedPaymentProcessor,
)
from payment_reliability.schemas import Payment, PaymentRequest
from payment_reliability.service import (
    IdempotencyConflictError,
    PaymentInProgressError,
    PaymentRecordMissingError,
    PaymentService,
    PaymentStateError,
)
from payment_reliability.store import PaymentStore


@pytest.fixture
def store():
    return create_autospec(PaymentStore, instance=True)


@pytest.fixture
def processor():
    return create_autospec(SimulatedPaymentProcessor, instance=True)


@pytest.fixture
def payment_request():
    return PaymentRequest(amount=Decimal("25.00"), currency="USD")


@pytest.fixture
def original_payment():
    return Payment(
        amount=Decimal("25.00"),
        currency="USD",
        status="succeeded",
        processor_transaction_id=uuid4(),
    )


def test_matching_completed_retry_returns_the_original_payment(
    store, processor, payment_request, original_payment
):
    store.get_by_idempotency_key.return_value = original_payment

    result = PaymentService(store, processor).resolve_retry("order-42", payment_request)

    assert result.id == original_payment.id
    assert result.amount == Decimal("25.00")
    assert result.currency == "USD"
    assert result.status == "succeeded"
    store.get_by_idempotency_key.assert_called_once_with("order-42")
    store.claim.assert_not_called()
    processor.process.assert_not_called()


@pytest.mark.parametrize("original_status", ["processing", "succeeded"])
def test_changed_amount_is_a_conflict_regardless_of_original_status(
    store, processor, original_payment, original_status
):
    original_payment.status = original_status
    if original_status == "processing":
        original_payment.processor_transaction_id = None
    store.get_by_idempotency_key.return_value = original_payment
    changed_request = PaymentRequest(amount=Decimal("40.00"), currency="USD")

    with pytest.raises(IdempotencyConflictError):
        PaymentService(store, processor).resolve_retry("order-42", changed_request)

    assert original_payment.amount == Decimal("25.00")
    assert original_payment.status == original_status
    store.claim.assert_not_called()


def test_matching_unfinished_retry_reports_in_progress(
    store, processor, payment_request, original_payment
):
    original_payment.status = "processing"
    original_payment.processor_transaction_id = None
    store.get_by_idempotency_key.return_value = original_payment

    with pytest.raises(PaymentInProgressError):
        PaymentService(store, processor).resolve_retry("order-42", payment_request)

    assert original_payment.status == "processing"
    store.claim.assert_not_called()


def test_missing_retry_record_raises_an_error_without_claiming_again(
    store, processor, payment_request
):
    store.get_by_idempotency_key.return_value = None

    with pytest.raises(PaymentRecordMissingError):
        PaymentService(store, processor).resolve_retry("order-42", payment_request)

    store.claim.assert_not_called()


def test_success_without_a_processor_reference_is_not_replayed(
    store, processor, payment_request, original_payment
):
    original_payment.processor_transaction_id = None
    store.get_by_idempotency_key.return_value = original_payment

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).resolve_retry("order-42", payment_request)

    processor.process.assert_not_called()


def test_new_payment_claims_then_processes_then_saves_confirmation(
    store, processor, payment_request
):
    claimed = Payment(amount=payment_request.amount, currency=payment_request.currency)
    confirmation = ProcessorResult(transaction_id=uuid4())
    completed = Payment(
        id=claimed.id,
        amount=claimed.amount,
        currency=claimed.currency,
        status="succeeded",
        processor_transaction_id=confirmation.transaction_id,
    )
    store.claim.return_value = claimed
    processor.process.return_value = confirmation
    store.mark_succeeded.return_value = completed
    sequence = Mock()
    sequence.attach_mock(store, "store")
    sequence.attach_mock(processor, "processor")

    result = PaymentService(store, processor).process_payment("order-42", payment_request)

    assert result.id == claimed.id
    assert result.status == "succeeded"
    assert result.processor_transaction_id == confirmation.transaction_id
    assert sequence.mock_calls == [
        call.store.claim("order-42", payment_request),
        call.processor.process(
            payment_id=claimed.id,
            amount=claimed.amount,
            currency=claimed.currency,
            idempotency_key="order-42",
        ),
        call.store.mark_succeeded(claimed.id, confirmation.transaction_id),
    ]


def test_existing_payment_is_replayed_without_processing_again(
    store, processor, payment_request, original_payment
):
    store.claim.return_value = None
    store.get_by_idempotency_key.return_value = original_payment

    result = PaymentService(store, processor).process_payment("order-42", payment_request)

    assert result.id == original_payment.id
    assert result.processor_transaction_id == original_payment.processor_transaction_id
    processor.process.assert_not_called()
    store.mark_succeeded.assert_not_called()


def test_processor_error_never_marks_a_payment_succeeded(store, processor, payment_request):
    claimed = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.claim.return_value = claimed
    processor.process.side_effect = ProcessorError("The processor did not confirm success.")

    with pytest.raises(ProcessorError):
        PaymentService(store, processor).process_payment("order-42", payment_request)

    store.mark_succeeded.assert_not_called()
    assert claimed.status == "processing"


def test_failed_state_update_does_not_return_a_successful_payment(
    store, processor, payment_request
):
    claimed = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.claim.return_value = claimed
    processor.process.return_value = ProcessorResult(transaction_id=uuid4())
    store.mark_succeeded.return_value = None

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).process_payment("order-42", payment_request)

    assert claimed.status == "processing"
