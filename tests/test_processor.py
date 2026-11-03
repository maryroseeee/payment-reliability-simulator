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
from payment_reliability.processor_store import ProcessorStore


@pytest.fixture
def processor_request():
    return {
        "payment_id": uuid4(),
        "amount": Decimal("25.00"),
        "currency": "USD",
        "idempotency_key": "order-42",
    }


def test_processor_confirms_success_with_a_transaction_id(processor_request, provider_store):
    processor = SimulatedPaymentProcessor(provider_store)

    result = processor.process(**processor_request)

    assert result.status == "succeeded"
    assert isinstance(result.transaction_id, UUID)
    assert result.payment_id == processor_request["payment_id"]
    assert result.amount == processor_request["amount"]
    assert result.currency == processor_request["currency"]
    assert processor.charge_count == 1


def test_processor_retry_returns_the_same_transaction(processor_request, provider_store):
    processor = SimulatedPaymentProcessor(provider_store)

    original = processor.process(**processor_request)
    retry = processor.process(**processor_request)

    assert retry.transaction_id == original.transaction_id
    assert processor.charge_count == 1


def test_processor_rejects_changed_details_under_the_same_key(processor_request, provider_store):
    processor = SimulatedPaymentProcessor(provider_store)
    processor.process(**processor_request)
    processor_request["amount"] = Decimal("40.00")

    with pytest.raises(ProcessorError):
        processor.process(**processor_request)

    assert processor.charge_count == 1


def test_concurrent_processor_retries_create_one_charge(processor_request, provider_store):
    processor = SimulatedPaymentProcessor(provider_store)
    start = Barrier(2)

    def process_at_same_time():
        start.wait(timeout=5)
        return processor.process(**processor_request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(process_at_same_time) for _ in range(2)]
        results = [attempt.result(timeout=10) for attempt in attempts]

    assert results[0].transaction_id == results[1].transaction_id
    assert processor.charge_count == 1


def test_separate_processors_share_one_charge_for_concurrent_retries(
    processor_request, provider_store
):
    processors = [
        SimulatedPaymentProcessor(ProcessorStore(provider_store.engine))
        for _ in range(2)
    ]
    start = Barrier(2)

    def process_at_same_time(processor):
        start.wait(timeout=5)
        return processor.process(**processor_request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(process_at_same_time, p) for p in processors]
        results = [attempt.result(timeout=10) for attempt in attempts]

    assert results[0].transaction_id == results[1].transaction_id
    assert all(processor.charge_count == 1 for processor in processors)


def test_successful_processor_result_requires_a_transaction_id(processor_request):
    with pytest.raises(ValidationError, match="Successful results require"):
        ProcessorResult(**processor_request)


def test_declined_processor_result_has_no_transaction_id(processor_request):
    result = ProcessorResult(**processor_request, status="declined")

    assert result.status == "declined"
    assert result.transaction_id is None


def test_declined_processor_result_cannot_include_a_transaction_id(processor_request):
    with pytest.raises(ValidationError, match="Declined results cannot include"):
        ProcessorResult(**processor_request, status="declined", transaction_id=uuid4())


@pytest.mark.parametrize("missing_field", ["payment_id", "amount", "currency"])
def test_processor_result_requires_payment_details(processor_request, missing_field):
    details = dict(processor_request, transaction_id=uuid4())
    del details[missing_field]

    with pytest.raises(ValidationError) as error:
        ProcessorResult(**details)

    assert any(
        item["loc"] == (missing_field,) and item["type"] == "missing"
        for item in error.value.errors()
    )


def test_decline_is_replayed_and_a_new_key_can_start_a_new_attempt(
    processor_request, provider_store
):
    processor = SimulatedPaymentProcessor(provider_store, decline=True)

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


def test_lost_response_keeps_the_original_charge_and_receipt_available(
    processor_request, provider_store
):
    processor = SimulatedPaymentProcessor(provider_store, lose_response=True)

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


def test_changed_details_cannot_replace_an_outcome_after_a_lost_response(
    processor_request, provider_store
):
    processor = SimulatedPaymentProcessor(provider_store, lose_response=True)
    with pytest.raises(ProcessorTimeoutError):
        processor.process(**processor_request)
    recorded = processor.get_result(processor_request["idempotency_key"])
    processor_request["amount"] = Decimal("40.00")

    with pytest.raises(ProcessorError):
        processor.process(**processor_request)

    assert processor.get_result(processor_request["idempotency_key"]) == recorded
    assert processor.charge_count == 1


def test_unknown_processor_lookup_does_not_start_a_payment(provider_store):
    processor = SimulatedPaymentProcessor(provider_store)

    assert processor.get_result("unknown-operation") is None
    assert processor.charge_count == 0


def test_new_processor_instance_can_read_the_previous_outcome(processor_request, provider_store):
    original = SimulatedPaymentProcessor(provider_store, lose_response=True)
    with pytest.raises(ProcessorTimeoutError):
        original.process(**processor_request)
    confirmation = original.get_result(processor_request["idempotency_key"])
    assert confirmation is not None

    fresh = SimulatedPaymentProcessor(ProcessorStore(provider_store.engine))
    recovered = fresh.get_result(processor_request["idempotency_key"])

    assert recovered is not None
    assert recovered.transaction_id == confirmation.transaction_id
    assert recovered.status == "succeeded"
    assert recovered.payment_id == processor_request["payment_id"]
    assert recovered.amount == processor_request["amount"]
    assert recovered.currency == processor_request["currency"]
