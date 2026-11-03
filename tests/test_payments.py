from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from unittest.mock import Mock, create_autospec
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from payment_reliability.database import engine
from payment_reliability.main import app, get_payment_service
from payment_reliability.processor import ProcessorError, SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def api_processor(provider_store):
    return SimulatedPaymentProcessor(provider_store)


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


def test_fresh_service_replays_the_persisted_payment(
    client, api_processor, idempotency_key, provider_store, monkeypatch
):
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}
    original = client.post("/payments", headers=headers, json=payload)
    fresh_processor = SimulatedPaymentProcessor(ProcessorStore(provider_store.engine))
    process = Mock(wraps=fresh_processor.process)
    monkeypatch.setattr(fresh_processor, "process", process)
    fresh_service = PaymentService(PaymentStore(engine), fresh_processor)
    app.dependency_overrides[get_payment_service] = lambda: fresh_service

    retry = client.post("/payments", headers=headers, json=payload)

    assert original.status_code == 201
    assert retry.status_code == 201
    assert retry.json() == original.json()
    assert api_processor.charge_count == 1
    assert fresh_processor.charge_count == 1
    process.assert_not_called()


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


def test_decline_is_saved_and_replayed_without_another_attempt(
    client, api_processor, idempotency_key, monkeypatch
):
    api_processor.decline = True
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    headers = {"Idempotency-Key": idempotency_key}
    payload = {"amount": "25.00", "currency": "USD"}

    original = client.post("/payments", headers=headers, json=payload)
    api_processor.decline = False
    retry = client.post("/payments", headers=headers, json=payload)

    assert original.status_code == 201
    assert original.json()["status"] == "declined"
    assert original.json()["processor_transaction_id"] is None
    assert retry.status_code == 201
    assert retry.json() == original.json()
    assert process.call_count == 1
    assert api_processor.charge_count == 0
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert str(stored.id) == original.json()["id"]
    assert stored.status == "declined"
    assert stored.processor_transaction_id is None


def test_new_key_creates_a_new_attempt_and_preserves_the_original_decline(
    client, api_processor, idempotency_keys
):
    payload = {"amount": "25.00", "currency": "USD"}
    api_processor.decline = True
    original = client.post(
        "/payments", headers={"Idempotency-Key": idempotency_keys[0]}, json=payload
    )

    api_processor.decline = False
    new_attempt = client.post(
        "/payments", headers={"Idempotency-Key": idempotency_keys[1]}, json=payload
    )
    original_retry = client.post(
        "/payments", headers={"Idempotency-Key": idempotency_keys[0]}, json=payload
    )

    assert original.status_code == 201
    assert original.json()["status"] == "declined"
    assert new_attempt.status_code == 201
    assert new_attempt.json()["status"] == "succeeded"
    assert new_attempt.json()["processor_transaction_id"]
    assert new_attempt.json()["id"] != original.json()["id"]
    assert original_retry.json() == original.json()
    assert api_processor.charge_count == 1
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_keys[0])
    assert stored is not None
    assert stored.status == "declined"
    assert str(stored.id) == original.json()["id"]


def test_lost_response_leaves_an_unresolved_payment_and_only_one_charge(
    client, api_processor, idempotency_key, monkeypatch
):
    api_processor.lose_response = True
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}

    failure = client.post("/payments", headers=headers, json=payload)

    assert failure.status_code == 503
    assert failure.json()["detail"]["code"] == "outcome_unconfirmed"
    assert api_processor.charge_count == 1
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert stored.status == "processing"
    assert stored.processor_transaction_id is None

    provider_result = api_processor.get_result(idempotency_key)
    assert provider_result is not None
    assert provider_result.status == "succeeded"
    assert provider_result.transaction_id is not None

    retry = client.post("/payments", headers=headers, json=payload)

    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "payment_in_progress"
    assert process.call_count == 1
    assert api_processor.charge_count == 1
    unresolved = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert unresolved is not None
    assert unresolved.id == stored.id
    assert unresolved.status == "processing"


def test_recovery_saves_lost_success_without_another_charge(
    client, api_processor, idempotency_key, monkeypatch
):
    api_processor.lose_response = True
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    payload = {"amount": "25.00", "currency": "USD"}
    headers = {"Idempotency-Key": idempotency_key}

    failure = client.post("/payments", headers=headers, json=payload)

    assert failure.status_code == 503
    assert failure.json()["detail"]["code"] == "outcome_unconfirmed"
    assert api_processor.charge_count == 1
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert stored is not None
    assert stored.status == "processing"
    assert stored.processor_transaction_id is None
    provider_result = api_processor.get_result(idempotency_key)
    assert provider_result is not None
    assert provider_result.status == "succeeded"
    assert provider_result.transaction_id is not None

    response = client.post("/payments/recover", headers=headers)

    assert response.status_code == 200
    recovered = response.json()
    assert recovered["status"] == "succeeded"
    assert recovered["id"] == str(stored.id)
    assert recovered["amount"] == "25.00"
    assert recovered["currency"] == "USD"
    assert recovered["processor_transaction_id"] == str(provider_result.transaction_id)
    saved = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert saved is not None
    assert saved.id == stored.id
    assert saved.status == "succeeded"
    assert saved.processor_transaction_id == provider_result.transaction_id

    repeated = client.post("/payments/recover", headers=headers)
    assert repeated.status_code == 200
    assert repeated.json() == recovered
    assert process.call_count == 1
    assert api_processor.charge_count == 1


def test_recovery_restores_a_lost_decline_without_a_charge(
    client, api_processor, idempotency_key, monkeypatch
):
    api_processor.decline = True
    api_processor.lose_response = True
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    headers = {"Idempotency-Key": idempotency_key}
    payload = {"amount": "25.00", "currency": "USD"}
    failure = client.post("/payments", headers=headers, json=payload)
    stored = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert failure.status_code == 503
    assert stored is not None
    assert stored.status == "processing"

    response = client.post("/payments/recover", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == str(stored.id)
    assert response.json()["status"] == "declined"
    assert response.json()["processor_transaction_id"] is None
    saved = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert saved is not None
    assert saved.status == "declined"
    assert saved.processor_transaction_id is None
    assert process.call_count == 1
    assert api_processor.charge_count == 0


def test_recovery_without_provider_confirmation_returns_processing(
    client, api_processor, idempotency_key, monkeypatch
):
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = PaymentStore(engine).claim(idempotency_key, request)
    assert original is not None

    response = client.post(
        "/payments/recover", headers={"Idempotency-Key": idempotency_key}
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(original.id)
    assert response.json()["status"] == "processing"
    assert response.json()["processor_transaction_id"] is None
    saved = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert saved is not None
    assert saved.status == "processing"
    process.assert_not_called()
    assert api_processor.charge_count == 0


def test_recovery_unknown_key_returns_not_found_without_processing(
    client, api_processor, idempotency_key, monkeypatch
):
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)

    response = client.post(
        "/payments/recover", headers={"Idempotency-Key": idempotency_key}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "payment_not_found"
    assert PaymentStore(engine).get_by_idempotency_key(idempotency_key) is None
    process.assert_not_called()
    assert api_processor.charge_count == 0


def test_recovery_lookup_failure_returns_unconfirmed_and_keeps_processing(
    client, api_processor, idempotency_key, monkeypatch
):
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    lookup = Mock(side_effect=ProcessorError("Processor lookup is unavailable."))
    monkeypatch.setattr(api_processor, "get_result", lookup)
    request = PaymentRequest.model_validate({"amount": "25.00", "currency": "USD"})
    original = PaymentStore(engine).claim(idempotency_key, request)
    assert original is not None

    response = client.post(
        "/payments/recover", headers={"Idempotency-Key": idempotency_key}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "outcome_unconfirmed"
    saved = PaymentStore(engine).get_by_idempotency_key(idempotency_key)
    assert saved is not None
    assert saved.id == original.id
    assert saved.status == "processing"
    assert saved.processor_transaction_id is None
    process.assert_not_called()
    assert api_processor.charge_count == 0


@pytest.mark.parametrize("difference", ["amount", "payment_id"])
@pytest.mark.parametrize("outcome", ["succeeded", "declined"])
def test_recovery_rejects_disagreeing_provider_records_without_changing_them(
    client, api_processor, idempotency_key, monkeypatch, difference, outcome
):
    store = PaymentStore(engine)
    original = store.claim(
        idempotency_key, PaymentRequest(amount=Decimal("25.00"), currency="USD")
    )
    assert original is not None
    provider_details = {
        "payment_id": original.id,
        "amount": original.amount,
        "currency": original.currency,
        "idempotency_key": idempotency_key,
    }
    provider_details[difference] = (
        Decimal("40.00") if difference == "amount" else uuid4()
    )
    api_processor.decline = outcome == "declined"
    confirmation = api_processor.process(**provider_details)
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)

    for _ in range(2):
        response = client.post(
            "/payments/recover", headers={"Idempotency-Key": idempotency_key}
        )

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "outcome_unconfirmed"
        saved = store.get_by_idempotency_key(idempotency_key)
        assert saved is not None
        assert saved.model_dump() == original.model_dump()
        assert api_processor.get_result(idempotency_key) == confirmation
        assert api_processor.charge_count == (1 if outcome == "succeeded" else 0)

    process.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "   "])
def test_recovery_requires_a_nonblank_idempotency_key(client, key):
    service = create_autospec(PaymentService, instance=True)
    app.dependency_overrides[get_payment_service] = lambda: service
    headers = {} if key is None else {"Idempotency-Key": key}

    response = client.post("/payments/recover", headers=headers)

    assert response.status_code == 422
    service.recover_payment.assert_not_called()
    service.process_payment.assert_not_called()
