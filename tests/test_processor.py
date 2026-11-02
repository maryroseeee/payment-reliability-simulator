from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from payment_reliability.processor import (
    ProcessorError,
    ProcessorResult,
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
