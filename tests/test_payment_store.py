"""PostgreSQL tests for committed payment reservations."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from payment_reliability.database import engine
from payment_reliability.models import PaymentRecord
from payment_reliability.schemas import PaymentRequest
from payment_reliability.store import PaymentStore


def stored_payment(key):
    with engine.connect() as connection:
        return connection.execute(
            select(PaymentRecord).where(PaymentRecord.idempotency_key == key)
        ).mappings().one()


def test_new_claim_is_committed_and_visible_to_another_connection(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})

    # Holding this connection forces the store to use a separate connection.
    with engine.connect() as observer:
        payment = store.claim(idempotency_key, request)

        assert payment is not None
        row = observer.execute(
            select(PaymentRecord).where(
                PaymentRecord.idempotency_key == idempotency_key
            )
        ).mappings().one()

    assert row["id"] == payment.id
    assert row["amount"] == Decimal("25.00")
    assert row["currency"] == "USD"
    assert row["status"] == "processing"
    assert payment.status == "processing"


@pytest.mark.parametrize("retry_amount", ["25.00", "40.00"])
def test_existing_key_cannot_be_claimed_or_overwritten(idempotency_key, retry_amount):
    store = PaymentStore(engine)
    original_request = PaymentRequest.model_validate(
        {"amount": "25.00", "currency": "USD"}
    )
    retry_request = PaymentRequest.model_validate(
        {"amount": retry_amount, "currency": "USD"}
    )

    original = store.claim(idempotency_key, original_request)
    retry = store.claim(idempotency_key, retry_request)

    assert original is not None
    assert retry is None
    row = stored_payment(idempotency_key)
    assert row["id"] == original.id
    assert row["amount"] == Decimal("25.00")
    assert row["status"] == "processing"


def test_concurrent_requests_have_only_one_successful_claim(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    start = Barrier(2)

    def claim_at_same_time():
        start.wait(timeout=5)
        return store.claim(idempotency_key, request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(claim_at_same_time) for _ in range(2)]
        results = [attempt.result(timeout=10) for attempt in attempts]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    row = stored_payment(idempotency_key)
    assert row["id"] == winners[0].id
    assert row["status"] == "processing"


def test_lookup_returns_the_original_payment_for_each_key(idempotency_keys):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    originals = {key: store.claim(key, request) for key in idempotency_keys}

    # Equal amounts are not enough to distinguish these two payments.
    for key, original in originals.items():
        assert original is not None
        retrieved = store.get_by_idempotency_key(key)

        assert retrieved is not None
        assert retrieved.id == original.id
        assert retrieved.amount == Decimal("25.00")
        assert retrieved.currency == "USD"
        assert retrieved.status == "processing"


def test_lookup_returns_none_for_an_unknown_key(idempotency_key):
    store = PaymentStore(engine)

    assert store.get_by_idempotency_key(idempotency_key) is None


def test_confirmed_success_is_committed_and_preserves_payment_details(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = store.claim(idempotency_key, request)
    assert original is not None
    transaction_id = uuid4()

    with engine.connect() as observer:
        completed = store.mark_succeeded(original.id, transaction_id)
        row = observer.execute(
            select(PaymentRecord).where(PaymentRecord.id == original.id)
        ).mappings().one()

    assert completed is not None
    assert completed.id == original.id
    assert completed.amount == Decimal("25.00")
    assert completed.currency == "USD"
    assert completed.status == "succeeded"
    assert completed.processor_transaction_id == transaction_id
    assert row["status"] == "succeeded"
    assert row["idempotency_key"] == idempotency_key
    assert row["processor_transaction_id"] == transaction_id


def test_unknown_payment_cannot_be_completed(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = store.claim(idempotency_key, request)
    assert original is not None

    assert store.mark_succeeded(uuid4(), uuid4()) is None
    assert stored_payment(idempotency_key)["status"] == "processing"


def test_completed_payment_is_not_transitioned_again(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = store.claim(idempotency_key, request)
    assert original is not None
    transaction_id = uuid4()
    completed = store.mark_succeeded(original.id, transaction_id)
    assert completed is not None

    assert store.mark_succeeded(original.id, uuid4()) is None
    row = stored_payment(idempotency_key)
    assert row["id"] == original.id
    assert row["amount"] == Decimal("25.00")
    assert row["status"] == "succeeded"
    assert row["processor_transaction_id"] == transaction_id


def test_success_requires_a_processor_transaction_id(idempotency_key):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = store.claim(idempotency_key, request)
    assert original is not None

    with pytest.raises(ValueError, match="transaction ID"):
        store.mark_succeeded(original.id, None)

    assert stored_payment(idempotency_key)["status"] == "processing"


def test_processor_transaction_cannot_be_linked_to_two_payments(idempotency_keys):
    store = PaymentStore(engine)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    first = store.claim(idempotency_keys[0], request)
    second = store.claim(idempotency_keys[1], request)
    assert first is not None
    assert second is not None
    transaction_id = uuid4()
    store.mark_succeeded(first.id, transaction_id)

    with pytest.raises(IntegrityError) as error:
        store.mark_succeeded(second.id, transaction_id)

    assert error.value.orig.sqlstate == "23505"
    row = stored_payment(idempotency_keys[1])
    assert row["status"] == "processing"
    assert row["processor_transaction_id"] is None
