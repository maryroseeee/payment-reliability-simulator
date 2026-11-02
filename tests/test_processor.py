from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from payment_reliability.processor import (
    ProcessorError,
    ProcessorResult,
    ProcessorTimeoutError,
    SimulatedPaymentProcessor,
)


@pytest.fixture
def processor_request():
    return {
        "payment_id": uuid4(),
        "amount": Decimal("25.00"),
        "currency": "USD",
        "idempotency_key": "order-42",
    }


def test_processor_confirms_success_with_a_transaction_id(processor_request):
    processor = SimulatedPaymentProcessor()

    result = processor.process(**processor_request)

    assert result.status == "succeeded"
    assert isinstance(result.transaction_id, UUID)
    assert processor.charge_count == 1


def test_processor_retry_returns_the_same_transaction(processor_request):
    processor = SimulatedPaymentProcessor()

    original = processor.process(**processor_request)
    retry = processor.process(**processor_request)

    assert retry.transaction_id == original.transaction_id
    assert processor.charge_count == 1


def test_processor_rejects_changed_details_under_the_same_key(processor_request):
    processor = SimulatedPaymentProcessor()
    processor.process(**processor_request)
    processor_request["amount"] = Decimal("40.00")

    with pytest.raises(ProcessorError):
        processor.process(**processor_request)

    assert processor.charge_count == 1


def test_concurrent_processor_retries_create_one_charge(processor_request):
    processor = SimulatedPaymentProcessor()
    start = Barrier(2)

    def process_at_same_time():
        start.wait(timeout=5)
        return processor.process(**processor_request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(process_at_same_time) for _ in range(2)]
        results = [attempt.result(timeout=10) for attempt in attempts]

    assert results[0].transaction_id == results[1].transaction_id
    assert processor.charge_count == 1


def test_successful_processor_result_requires_a_transaction_id():
    with pytest.raises(ValidationError):
        ProcessorResult()


def test_declined_processor_result_has_no_transaction_id():
    result = ProcessorResult(status="declined")

    assert result.status == "declined"
    assert result.transaction_id is None


def test_declined_processor_result_cannot_include_a_transaction_id():
    with pytest.raises(ValidationError, match="Declined results cannot include"):
        ProcessorResult(status="declined", transaction_id=uuid4())


def test_decline_is_replayed_and_a_new_key_can_start_a_new_attempt(processor_request):
    processor = SimulatedPaymentProcessor(decline=True)

    original = processor.process(**processor_request)
    assert original.status == "declined"
    assert original.transaction_id is None
    assert processor.charge_count == 0

    # Changing the simulator's current behavior must not change an old outcome.
    processor.decline = False
    retry = processor.process(**processor_request)
    assert retry.status == "declined"
    assert retry.transaction_id is None
    assert processor.charge_count == 0

    new_attempt = dict(
        processor_request, payment_id=uuid4(), idempotency_key="another-attempt"
    )
    result = processor.process(**new_attempt)
    assert result.status == "succeeded"
    assert result.transaction_id is not None
    assert processor.charge_count == 1


def test_lost_response_keeps_the_original_charge_and_receipt_available(processor_request):
    processor = SimulatedPaymentProcessor(lose_response=True)

    with pytest.raises(ProcessorTimeoutError):
        processor.process(**processor_request)

    assert processor.charge_count == 1
    recorded = processor.get_result(processor_request["idempotency_key"])
    assert recorded is not None
    assert recorded.status == "succeeded"
    assert recorded.transaction_id is not None
    assert processor.charge_count == 1

    provider_retry = processor.process(**processor_request)
    assert provider_retry.transaction_id == recorded.transaction_id
    assert processor.charge_count == 1


def test_changed_details_cannot_replace_an_outcome_after_a_lost_response(processor_request):
    processor = SimulatedPaymentProcessor(lose_response=True)
    with pytest.raises(ProcessorTimeoutError):
        processor.process(**processor_request)
    recorded = processor.get_result(processor_request["idempotency_key"])
    processor_request["amount"] = Decimal("40.00")

    with pytest.raises(ProcessorError):
        processor.process(**processor_request)

    assert processor.get_result(processor_request["idempotency_key"]) == recorded
    assert processor.charge_count == 1


def test_unknown_processor_lookup_does_not_start_a_payment():
    processor = SimulatedPaymentProcessor()

    assert processor.get_result("unknown-operation") is None
    assert processor.charge_count == 0
