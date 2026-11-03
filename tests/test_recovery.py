"""Recovery tested against PostgreSQL and the real in-process simulator."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import Mock

import pytest

from payment_reliability.database import engine
from payment_reliability.processor import ProcessorTimeoutError, SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.mark.parametrize("decline", [False, True])
def test_recovery_restores_a_lost_outcome_without_another_attempt(
    idempotency_key, monkeypatch, decline, provider_store
):
    processor = SimulatedPaymentProcessor(provider_store, decline=decline, lose_response=True)
    process = Mock(wraps=processor.process)
    monkeypatch.setattr(processor, "process", process)
    store = PaymentStore(engine)
    service = PaymentService(store, processor)
    request = PaymentRequest(amount=Decimal("25.00"), currency="USD")

    with pytest.raises(ProcessorTimeoutError):
        service.process_payment(idempotency_key, request)
    unresolved = store.get_by_idempotency_key(idempotency_key)
    assert unresolved is not None
    assert unresolved.status == "processing"
    confirmation = processor.get_result(idempotency_key)
    assert confirmation is not None
    expected_charges = 0 if decline else 1
    assert processor.charge_count == expected_charges

    recovered = service.recover_payment(idempotency_key)

    assert recovered.id == unresolved.id
    assert recovered.amount == Decimal("25.00")
    assert recovered.currency == "USD"
    assert recovered.status == confirmation.status
    assert recovered.processor_transaction_id == confirmation.transaction_id
    assert processor.charge_count == expected_charges
    persisted = store.get_by_idempotency_key(idempotency_key)
    assert persisted is not None
    assert persisted.model_dump() == recovered.model_dump()

    repeated = service.recover_payment(idempotency_key)
    replayed = service.process_payment(idempotency_key, request)
    assert repeated.model_dump() == recovered.model_dump()
    assert replayed.model_dump() == recovered.model_dump()
    assert process.call_count == 1
    assert processor.charge_count == expected_charges


@pytest.mark.parametrize("decline", [False, True])
def test_fresh_processor_recovers_persisted_history_without_another_attempt(
    idempotency_key, provider_store, monkeypatch, decline
):
    original_processor = SimulatedPaymentProcessor(
        provider_store, decline=decline, lose_response=True
    )
    store = PaymentStore(engine)
    request = PaymentRequest(amount=Decimal("25.00"), currency="USD")

    with pytest.raises(ProcessorTimeoutError):
        PaymentService(store, original_processor).process_payment(idempotency_key, request)
    unresolved = store.get_by_idempotency_key(idempotency_key)
    assert unresolved is not None
    confirmation = original_processor.get_result(idempotency_key)
    assert confirmation is not None
    # A fresh instance uses the default success setting, but must preserve declines.
    fresh_processor = SimulatedPaymentProcessor(ProcessorStore(provider_store.engine))
    process = Mock(wraps=fresh_processor.process)
    monkeypatch.setattr(fresh_processor, "process", process)

    result = PaymentService(store, fresh_processor).recover_payment(idempotency_key)

    assert result.id == unresolved.id
    assert result.status == confirmation.status
    assert result.processor_transaction_id == confirmation.transaction_id
    assert original_processor.charge_count == (0 if decline else 1)
    assert fresh_processor.charge_count == (0 if decline else 1)
    process.assert_not_called()
    persisted = store.get_by_idempotency_key(idempotency_key)
    assert persisted is not None
    assert persisted.model_dump() == result.model_dump()


def test_missing_provider_history_does_not_trigger_another_payment(
    idempotency_key, provider_store, monkeypatch
):
    store = PaymentStore(engine)
    original = store.claim(
        idempotency_key, PaymentRequest(amount=Decimal("25.00"), currency="USD")
    )
    assert original is not None
    processor = SimulatedPaymentProcessor(provider_store)
    process = Mock(wraps=processor.process)
    monkeypatch.setattr(processor, "process", process)

    result = PaymentService(store, processor).recover_payment(idempotency_key)

    assert result.model_dump() == original.model_dump()
    assert result.status == "processing"
    assert processor.get_result(idempotency_key) is None
    assert processor.charge_count == 0
    process.assert_not_called()
    persisted = store.get_by_idempotency_key(idempotency_key)
    assert persisted is not None
    assert persisted.model_dump() == original.model_dump()


@pytest.mark.parametrize("decline", [False, True])
def test_concurrent_recoveries_return_the_same_saved_outcome(
    idempotency_key, monkeypatch, decline, provider_store
):
    processor = SimulatedPaymentProcessor(provider_store, decline=decline, lose_response=True)
    process = Mock(wraps=processor.process)
    monkeypatch.setattr(processor, "process", process)
    store = PaymentStore(engine)
    service = PaymentService(store, processor)
    request = PaymentRequest(amount=Decimal("25.00"), currency="USD")
    with pytest.raises(ProcessorTimeoutError):
        service.process_payment(idempotency_key, request)
    original = store.get_by_idempotency_key(idempotency_key)
    assert original is not None

    both_read_processing = Barrier(2)
    original_lookup = store.get_by_idempotency_key

    def read_before_either_recovery_saves(key):
        payment = original_lookup(key)
        if payment is not None and payment.status == "processing":
            both_read_processing.wait(timeout=5)
        return payment

    monkeypatch.setattr(store, "get_by_idempotency_key", read_before_either_recovery_saves)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(service.recover_payment, idempotency_key) for _ in range(2)]
        results = [attempt.result(timeout=10) for attempt in attempts]

    assert results[0].model_dump() == results[1].model_dump()
    assert results[0].id == original.id
    assert results[0].status == ("declined" if decline else "succeeded")
    assert process.call_count == 1
    assert processor.charge_count == (0 if decline else 1)
