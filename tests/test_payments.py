from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, create_autospec

import pytest
from fastapi.testclient import TestClient

from payment_reliability.database import engine
from payment_reliability.main import app, get_payment_service
from payment_reliability.processor import ProcessorError, SimulatedPaymentProcessor
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def api_processor():
    return SimulatedPaymentProcessor()


@pytest.fixture
def client(api_processor):
    service = PaymentService(PaymentStore(engine), api_processor)
    app.dependency_overrides[get_payment_service] = lambda: service
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_payment_service, None)


def test_new_payment_returns_a_successful_payment(client, api_processor, idempotency_key):
    # Given a new request to simulate a $25 USD payment.
    payment_request = {"amount": "25.00", "currency": "USD"}

    # When the client submits it with an idempotency key.
    response = client.post(
        "/payments",
        headers={"Idempotency-Key": idempotency_key},
        json=payment_request,
    )

    # Then the API reports a newly created, successful payment.
    assert response.status_code == 201
    payment = response.json()
    assert isinstance(payment["id"], str)
    assert payment["id"]
    assert payment["amount"] == "25.00"
    assert payment["currency"] == "USD"
    assert payment["status"] == "succeeded"
    assert payment["processor_transaction_id"]
    assert api_processor.charge_count == 1
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert str(stored.id) == payment["id"]
    assert str(stored.processor_transaction_id) == payment["processor_transaction_id"]
    assert stored.status == "succeeded"


@pytest.mark.parametrize("amount", ["25.001", "-25.00", "0.00"])
def test_api_rejects_invalid_payment_amount(client, api_processor, idempotency_key, amount):
    response = client.post(
        "/payments",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount": amount, "currency": "USD"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "amount"]
    assert api_processor.charge_count == 0


def test_completed_retry_returns_the_same_response_without_another_charge(
    client, api_processor, idempotency_key
):
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}

    original = client.post("/payments", headers=headers, json=payload)
    retry = client.post("/payments", headers=headers, json=payload)

    assert original.status_code == 201
    assert retry.status_code == 201
    assert retry.json() == original.json()
    assert api_processor.charge_count == 1


def test_changed_amount_returns_conflict_and_preserves_the_original(
    client, api_processor, idempotency_key
):
    headers = {"Idempotency-Key": idempotency_key}
    original = client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )
    conflict = client.post(
        "/payments", headers=headers, json={"amount": "40.00", "currency": "USD"}
    )

    assert original.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert str(stored.id) == original.json()["id"]
    assert str(stored.amount) == "25.00"
    assert api_processor.charge_count == 1


def test_existing_processing_payment_returns_conflict_without_a_charge(
    client, api_processor, idempotency_key
):
    payload = {"amount": "25.00", "currency": "USD"}
    PaymentStore(engine).claim(idempotency_key, PaymentRequest.model_validate(payload))

    response = client.post(
        "/payments", headers={"Idempotency-Key": idempotency_key}, json=payload
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "payment_in_progress"
    assert api_processor.charge_count == 0


@pytest.mark.parametrize("key", [None, "", "   "])
def test_api_requires_a_nonblank_idempotency_key(client, key):
    service = create_autospec(PaymentService, instance=True)
    app.dependency_overrides[get_payment_service] = lambda: service
    headers = {} if key is None else {"Idempotency-Key": key}

    response = client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )

    assert response.status_code == 422
    service.process_payment.assert_not_called()


def test_fresh_service_replays_the_persisted_payment(client, api_processor, idempotency_key):
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}
    original = client.post("/payments", headers=headers, json=payload)
    fresh_processor = SimulatedPaymentProcessor()
    fresh_service = PaymentService(PaymentStore(engine), fresh_processor)
    app.dependency_overrides[get_payment_service] = lambda: fresh_service

    retry = client.post("/payments", headers=headers, json=payload)

    assert original.status_code == 201
    assert retry.status_code == 201
    assert retry.json() == original.json()
    assert api_processor.charge_count == 1
    assert fresh_processor.charge_count == 0


def test_duplicate_request_during_processing_does_not_start_another_charge(
    client, api_processor, idempotency_key, monkeypatch
):
    started = Event()
    release = Event()
    calls = []
    original_process = api_processor.process
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}

    def paused_process(**kwargs):
        calls.append(kwargs)
        started.set()
        if not release.wait(timeout=5):
            raise ProcessorError("The test did not release the simulator.")
        return original_process(**kwargs)

    monkeypatch.setattr(api_processor, "process", paused_process)

    def submit_first_request():
        with TestClient(app) as first_client:
            return first_client.post("/payments", headers=headers, json=payload)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(submit_first_request)
        try:
            assert started.wait(timeout=5)
            duplicate = client.post("/payments", headers=headers, json=payload)
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"]["code"] == "payment_in_progress"
        finally:
            release.set()
        original = first.result(timeout=10)

    retry = client.post("/payments", headers=headers, json=payload)
    assert original.status_code == 201
    assert retry.status_code == 201
    assert retry.json() == original.json()
    assert len(calls) == 1
    assert api_processor.charge_count == 1


def test_processor_error_leaves_processing_and_does_not_blindly_retry(
    client, api_processor, idempotency_key, monkeypatch
):
    process = Mock(side_effect=ProcessorError("The simulator is unavailable."))
    monkeypatch.setattr(api_processor, "process", process)
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}

    failure = client.post("/payments", headers=headers, json=payload)
    retry = client.post("/payments", headers=headers, json=payload)

    assert failure.status_code == 503
    assert failure.json()["detail"]["code"] == "outcome_unconfirmed"
    assert retry.status_code == 409
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert stored.status == "processing"
    assert stored.processor_transaction_id is None
    assert process.call_count == 1
