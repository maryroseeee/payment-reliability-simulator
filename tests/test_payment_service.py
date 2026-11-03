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


@pytest.mark.parametrize("original_status", ["processing", "succeeded", "declined"])
def test_changed_amount_is_a_conflict_regardless_of_original_status(
    store, processor, original_payment, original_status
):
    original_payment.status = original_status
    if original_status != "succeeded":
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
    confirmation = ProcessorResult(
        payment_id=claimed.id, amount=claimed.amount, currency=claimed.currency,
        transaction_id=uuid4(),
    )
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
    processor.process.return_value = ProcessorResult(
        payment_id=claimed.id, amount=claimed.amount, currency=claimed.currency,
        transaction_id=uuid4(),
    )
    store.mark_succeeded.return_value = None

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).process_payment("order-42", payment_request)

    assert claimed.status == "processing"


def test_new_decline_is_saved_as_a_known_outcome(store, processor, payment_request):
    claimed = Payment(amount=payment_request.amount, currency=payment_request.currency)
    declined = Payment(
        id=claimed.id,
        amount=claimed.amount,
        currency=claimed.currency,
        status="declined",
    )
    store.claim.return_value = claimed
    processor.process.return_value = ProcessorResult(
        payment_id=claimed.id, amount=claimed.amount, currency=claimed.currency,
        status="declined",
    )
    store.mark_declined.return_value = declined
    sequence = Mock()
    sequence.attach_mock(store, "store")
    sequence.attach_mock(processor, "processor")

    result = PaymentService(store, processor).process_payment("order-42", payment_request)

    assert result.id == claimed.id
    assert result.status == "declined"
    assert result.processor_transaction_id is None
    assert sequence.mock_calls == [
        call.store.claim("order-42", payment_request),
        call.processor.process(
            payment_id=claimed.id,
            amount=claimed.amount,
            currency=claimed.currency,
            idempotency_key="order-42",
        ),
        call.store.mark_declined(claimed.id),
    ]


def test_declined_retry_returns_the_original_without_another_attempt(
    store, processor, payment_request, original_payment
):
    original_payment.status = "declined"
    original_payment.processor_transaction_id = None
    store.claim.return_value = None
    store.get_by_idempotency_key.return_value = original_payment

    result = PaymentService(store, processor).process_payment("order-42", payment_request)

    assert result.id == original_payment.id
    assert result.status == "declined"
    assert result.processor_transaction_id is None
    processor.process.assert_not_called()
    store.mark_succeeded.assert_not_called()
    store.mark_declined.assert_not_called()


def test_decline_with_a_transaction_reference_is_not_replayed(
    store, processor, payment_request, original_payment
):
    original_payment.status = "declined"
    store.get_by_idempotency_key.return_value = original_payment

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).resolve_retry("order-42", payment_request)

    processor.process.assert_not_called()


def test_failed_decline_update_does_not_return_a_declined_payment(
    store, processor, payment_request
):
    claimed = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.claim.return_value = claimed
    processor.process.return_value = ProcessorResult(
        payment_id=claimed.id, amount=claimed.amount, currency=claimed.currency,
        status="declined",
    )
    store.mark_declined.return_value = None

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).process_payment("order-42", payment_request)

    store.mark_succeeded.assert_not_called()
    assert claimed.status == "processing"


@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
def test_recovery_saves_the_existing_processors_confirmation(
    store, processor, payment_request, outcome
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    transaction_id = uuid4() if outcome == "succeeded" else None
    confirmation = ProcessorResult(
        payment_id=original.id, amount=original.amount, currency=original.currency,
        status=outcome, transaction_id=transaction_id,
    )
    completed = Payment(
        id=original.id,
        amount=original.amount,
        currency=original.currency,
        status=outcome,
        processor_transaction_id=transaction_id,
    )
    store.get_by_idempotency_key.return_value = original
    processor.get_result.return_value = confirmation
    if outcome == "succeeded":
        store.mark_succeeded.return_value = completed
    else:
        store.mark_declined.return_value = completed

    result = PaymentService(store, processor).recover_payment("order-42")

    assert result.id == original.id
    assert result.amount == original.amount
    assert result.currency == original.currency
    assert result.status == outcome
    assert result.processor_transaction_id == transaction_id
    processor.get_result.assert_called_once_with("order-42")
    processor.process.assert_not_called()
    store.claim.assert_not_called()
    if outcome == "succeeded":
        store.mark_succeeded.assert_called_once_with(original.id, transaction_id)
        store.mark_declined.assert_not_called()
    else:
        store.mark_declined.assert_called_once_with(original.id)
        store.mark_succeeded.assert_not_called()


def test_recovery_without_confirmation_keeps_the_payment_unresolved(
    store, processor, payment_request
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.get_by_idempotency_key.return_value = original
    processor.get_result.return_value = None

    result = PaymentService(store, processor).recover_payment("order-42")

    assert result.id == original.id
    assert result.status == "processing"
    assert result.processor_transaction_id is None
    processor.get_result.assert_called_once_with("order-42")
    processor.process.assert_not_called()
    store.claim.assert_not_called()
    store.mark_succeeded.assert_not_called()
    store.mark_declined.assert_not_called()


@pytest.mark.parametrize("operation", ["process", "recover"])
@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
@pytest.mark.parametrize("difference", ["payment_id", "amount", "currency"])
def test_mismatched_provider_confirmation_never_changes_the_payment(
    store, processor, payment_request, operation, outcome, difference
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    details = {
        "payment_id": original.id,
        "amount": original.amount,
        "currency": original.currency,
    }
    details[difference] = {
        "payment_id": uuid4(),
        "amount": Decimal("40.00"),
        "currency": "EUR",
    }[difference]
    confirmation = ProcessorResult(
        **details,
        status=outcome,
        transaction_id=uuid4() if outcome == "succeeded" else None,
    )
    store.claim.return_value = original
    store.get_by_idempotency_key.return_value = original
    processor.process.return_value = confirmation
    processor.get_result.return_value = confirmation
    completed = original.model_copy(update={
        "status": outcome,
        "processor_transaction_id": confirmation.transaction_id,
    })
    store.mark_succeeded.return_value = completed
    store.mark_declined.return_value = completed
    service = PaymentService(store, processor)

    with pytest.raises(PaymentStateError, match="confirmation does not match"):
        if operation == "process":
            service.process_payment("order-42", payment_request)
        else:
            service.recover_payment("order-42")

    store.mark_succeeded.assert_not_called()
    store.mark_declined.assert_not_called()
    assert original.status == "processing"
    assert original.processor_transaction_id is None
    if operation == "recover":
        processor.process.assert_not_called()
        store.claim.assert_not_called()
    else:
        assert processor.process.call_count == 1


@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
def test_repeated_recovery_returns_a_completed_record_without_processor_calls(
    store, processor, payment_request, outcome
):
    original = Payment(
        amount=payment_request.amount,
        currency=payment_request.currency,
        status=outcome,
        processor_transaction_id=uuid4() if outcome == "succeeded" else None,
    )
    store.get_by_idempotency_key.return_value = original

    result = PaymentService(store, processor).recover_payment("order-42")

    assert result.id == original.id
    assert result.status == outcome
    assert result.processor_transaction_id == original.processor_transaction_id
    processor.get_result.assert_not_called()
    processor.process.assert_not_called()
    store.claim.assert_not_called()
    store.mark_succeeded.assert_not_called()
    store.mark_declined.assert_not_called()


def test_recovery_does_not_create_a_missing_internal_payment(store, processor):
    store.get_by_idempotency_key.return_value = None

    with pytest.raises(PaymentRecordMissingError):
        PaymentService(store, processor).recover_payment("unknown-key")

    processor.get_result.assert_not_called()
    processor.process.assert_not_called()
    store.claim.assert_not_called()


def test_recovery_lookup_error_does_not_change_the_payment(
    store, processor, payment_request
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.get_by_idempotency_key.return_value = original
    processor.get_result.side_effect = ProcessorError("Lookup is unavailable.")

    with pytest.raises(ProcessorError):
        PaymentService(store, processor).recover_payment("order-42")

    assert original.status == "processing"
    processor.process.assert_not_called()
    store.claim.assert_not_called()
    store.mark_succeeded.assert_not_called()
    store.mark_declined.assert_not_called()


def test_recovery_does_not_report_completion_when_the_save_did_not_apply(
    store, processor, payment_request
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    store.get_by_idempotency_key.return_value = original
    processor.get_result.return_value = ProcessorResult(
        payment_id=original.id, amount=original.amount, currency=original.currency,
        transaction_id=uuid4(),
    )
    store.mark_succeeded.return_value = None

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).recover_payment("order-42")

    processor.process.assert_not_called()
    store.claim.assert_not_called()


@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
def test_concurrent_recovery_accepts_the_same_outcome_already_saved(
    store, processor, payment_request, outcome
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    transaction_id = uuid4() if outcome == "succeeded" else None
    confirmation = ProcessorResult(
        payment_id=original.id, amount=original.amount, currency=original.currency,
        status=outcome, transaction_id=transaction_id,
    )
    already_saved = Payment(
        id=original.id,
        amount=original.amount,
        currency=original.currency,
        status=outcome,
        processor_transaction_id=transaction_id,
    )
    store.get_by_idempotency_key.side_effect = [original, already_saved]
    processor.get_result.return_value = confirmation
    store.mark_succeeded.return_value = None
    store.mark_declined.return_value = None

    result = PaymentService(store, processor).recover_payment("order-42")

    assert result.model_dump() == already_saved.model_dump()
    assert store.get_by_idempotency_key.call_args_list == [
        call("order-42"), call("order-42")
    ]
    processor.get_result.assert_called_once_with("order-42")
    processor.process.assert_not_called()
    store.claim.assert_not_called()


@pytest.mark.parametrize("difference", ["transaction_id", "status", "payment_id", "amount"])
def test_recovery_rejects_a_conflicting_saved_record(
    store, processor, payment_request, difference
):
    original = Payment(amount=payment_request.amount, currency=payment_request.currency)
    confirmation = ProcessorResult(
        payment_id=original.id, amount=original.amount, currency=original.currency,
        transaction_id=uuid4(),
    )
    conflicting = Payment(
        id=original.id,
        amount=original.amount,
        currency=original.currency,
        status="succeeded",
        processor_transaction_id=confirmation.transaction_id,
    )
    if difference == "transaction_id":
        conflicting.processor_transaction_id = uuid4()
    elif difference == "status":
        conflicting.status = "declined"
        conflicting.processor_transaction_id = None
    elif difference == "payment_id":
        conflicting.id = uuid4()
    else:
        conflicting.amount = Decimal("40.00")
    before = conflicting.model_dump()
    store.get_by_idempotency_key.side_effect = [original, conflicting]
    processor.get_result.return_value = confirmation
    store.mark_succeeded.return_value = None

    with pytest.raises(PaymentStateError):
        PaymentService(store, processor).recover_payment("order-42")

    assert conflicting.model_dump() == before
    processor.process.assert_not_called()
    store.claim.assert_not_called()
