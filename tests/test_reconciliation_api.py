"""Reconciliation inspection, saved runs, and history through HTTP and Swagger."""

from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

import payment_reliability.main as main
from payment_reliability.models import LedgerEntryRecord, LedgerPostingRecord
from payment_reliability.processor import SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.reconciliation_runs import ReconciliationRunStore
from payment_reliability.reconciliation_store import ReconciliationStore
from payment_reliability.schemas import Payment, PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def api_processor(reconciliation_engine):
    return SimulatedPaymentProcessor(ProcessorStore(reconciliation_engine))


@pytest.fixture
def payment_store(reconciliation_engine):
    return PaymentStore(reconciliation_engine)


@pytest.fixture
def reader(reconciliation_engine):
    return ReconciliationStore(reconciliation_engine)


@pytest.fixture
def history(reconciliation_engine):
    return ReconciliationRunStore(reconciliation_engine)


@pytest.fixture
def api_client(payment_store, api_processor, reader, history, monkeypatch):
    service = PaymentService(payment_store, api_processor)
    monkeypatch.setitem(main.app.dependency_overrides, main.get_payment_service, lambda: service)
    monkeypatch.setitem(main.app.dependency_overrides, main.get_reconciliation_store, lambda: reader)
    monkeypatch.setitem(main.app.dependency_overrides, main.get_reconciliation_run_store, lambda: history)
    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize("key", [None, "", "   "])
@pytest.mark.parametrize("method,path", [
    ("get", "/payments/reconciliation"),
    ("post", "/payments/reconciliation/runs"),
    ("get", "/payments/reconciliation/runs"),
])
def test_reconciliation_requires_header(key, method, path):
    headers = {} if key is None else {"Idempotency-Key": key}
    with TestClient(main.app) as client:
        response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 422


def test_reconciliation_returns_the_consistent_report_without_changing_records(
    api_client, reader, api_processor, monkeypatch
):
    headers = {"Idempotency-Key": "consistent-key"}
    created = api_client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )
    assert created.status_code == 201
    before = reader.load("consistent-key")
    assert before is not None and before.ledger is not None
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)

    response = api_client.get("/payments/reconciliation", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "payment_id": created.json()["id"],
        "posting_id": str(before.ledger.id),
        "status": "consistent",
        "findings": [],
    }
    assert response.headers["cache-control"] == "no-store"
    repeated = api_client.get("/payments/reconciliation", headers=headers)
    assert repeated.json() == response.json()
    after = reader.load("consistent-key")
    assert after is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert api_processor.charge_count == 1
    process.assert_not_called()


def test_missing_ledger_returns_a_successful_check_with_findings_and_no_repair(
    api_client, reader, api_processor, reconciliation_engine, monkeypatch
):
    headers = {"Idempotency-Key": "missing-ledger-key"}
    created = api_client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )
    assert created.status_code == 201
    original = reader.load("missing-ledger-key")
    assert original is not None and original.ledger is not None
    # Simulate missing accounting history using only this test's isolated schema.
    with reconciliation_engine.begin() as connection:
        connection.execute(delete(LedgerEntryRecord).where(
            LedgerEntryRecord.posting_id == original.ledger.id
        ))
        connection.execute(delete(LedgerPostingRecord).where(
            LedgerPostingRecord.id == original.ledger.id
        ))
    before = reader.load("missing-ledger-key")
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)

    response = api_client.get("/payments/reconciliation", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "payment_id": created.json()["id"],
        "posting_id": None,
        "status": "discrepancy",
        "findings": [{
            "code": "missing_ledger_posting",
            "message": "Successful payment for 25.00 USD has no ledger posting.",
        }],
    }
    assert response.headers["cache-control"] == "no-store"
    repeated = api_client.get("/payments/reconciliation", headers=headers)
    assert repeated.json() == response.json()
    after = reader.load("missing-ledger-key")
    assert after is not None and before is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert api_processor.charge_count == 1
    process.assert_not_called()


def test_unknown_key_returns_not_found_without_creating_a_payment(
    api_client, payment_store, api_processor
):
    response = api_client.get(
        "/payments/reconciliation", headers={"Idempotency-Key": "unknown-key"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "payment_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert payment_store.get_by_idempotency_key("unknown-key") is None
    assert api_processor.charge_count == 0


@pytest.mark.parametrize("payment_status", ["processing", "declined"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_unsupported_state_returns_conflict_without_recovery_or_reprocessing(
    api_client, payment_store, api_processor, reader, history, monkeypatch, payment_status, method
):
    headers = {"Idempotency-Key": "unsupported-key"}
    if payment_status == "processing":
        payment_store.claim("unsupported-key", PaymentRequest(amount="25.00", currency="USD"))
    else:
        api_processor.decline = True
        created = api_client.post(
            "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
        )
        assert created.status_code == 201
    before = reader.load("unsupported-key")
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)

    path = "/payments/reconciliation" if method == "get" else "/payments/reconciliation/runs"
    response = getattr(api_client, method)(path, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reconciliation_not_supported"
    assert response.headers["cache-control"] == "no-store"
    after = reader.load("unsupported-key")
    assert after is not None and before is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert api_processor.charge_count == 0
    assert history.list_for_key("unsupported-key") == []
    process.assert_not_called()


@pytest.mark.parametrize("failure", ["database", "invalid_stored_data"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_report_generation_failures_return_unavailable_instead_of_a_false_report(
    api_client, reader, monkeypatch, failure, method
):
    if failure == "database":
        error = SQLAlchemyError("Internal database failure.")
    else:
        with pytest.raises(ValidationError) as invalid:
            Payment(amount="25.001", currency="USD")
        error = invalid.value
    load = Mock(side_effect=error)
    monkeypatch.setattr(reader, "load", load)

    path = "/payments/reconciliation" if method == "get" else "/payments/reconciliation/runs"
    response = getattr(api_client, method)(
        path, headers={"Idempotency-Key": "failure-key"}
    )

    assert response.status_code == 503
    expected_code = "reconciliation_unavailable" if method == "get" else "reconciliation_run_unavailable"
    assert response.json()["detail"]["code"] == expected_code
    assert response.headers["cache-control"] == "no-store"
    load.assert_called_once_with("failure-key")


@pytest.mark.parametrize("method,path,responses", [
    ("get", "/payments/reconciliation", {"200", "404", "409", "422", "503"}),
    ("post", "/payments/reconciliation/runs", {"201", "404", "409", "422", "503"}),
    ("get", "/payments/reconciliation/runs", {"200", "404", "422", "503"}),
])
def test_swagger_describes_reconciliation_actions_with_required_key_and_no_body(method, path, responses):
    with TestClient(main.app) as client:
        schema = client.get("/openapi.json").json()

    operation = schema["paths"][path][method]
    assert "requestBody" not in operation
    key = next(parameter for parameter in operation["parameters"] if parameter["name"] == "Idempotency-Key")
    assert key["in"] == "header"
    assert key["required"] is True
    assert responses <= set(operation["responses"])


def test_post_saves_separate_runs_and_gets_only_inspect_existing_data(
    api_client, reader, history, api_processor, monkeypatch
):
    headers = {"Idempotency-Key": "saved-key"}
    created = api_client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )
    assert created.status_code == 201
    before = reader.load("saved-key")
    assert before is not None
    process = Mock(wraps=api_processor.process)
    monkeypatch.setattr(api_processor, "process", process)
    inspection = api_client.get("/payments/reconciliation", headers=headers)
    assert inspection.status_code == 200
    assert api_client.get("/payments/reconciliation/runs", headers=headers).json() == []

    first = api_client.post("/payments/reconciliation/runs", headers=headers)
    second = api_client.post("/payments/reconciliation/runs", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["payment_id"] == second.json()["payment_id"] == created.json()["id"]
    assert datetime.fromisoformat(first.json()["checked_at"]).tzinfo is not None
    assert first.json()["report"] == second.json()["report"] == inspection.json()
    with monkeypatch.context() as patch:
        patch.setattr(reader, "load", Mock(side_effect=AssertionError("History must not rerun a check.")))
        saved = api_client.get("/payments/reconciliation/runs", headers=headers)
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json() == [first.json(), second.json()]
    assert len(history.list_for_key("saved-key")) == 2
    after = reader.load("saved-key")
    assert after is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert api_processor.charge_count == 1
    process.assert_not_called()


def test_saving_a_discrepancy_returns_created_and_preserves_its_findings(
    api_client, reader, history, reconciliation_engine
):
    headers = {"Idempotency-Key": "save-discrepancy-key"}
    created = api_client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    )
    assert created.status_code == 201
    original = reader.load("save-discrepancy-key")
    assert original is not None and original.ledger is not None
    with reconciliation_engine.begin() as connection:
        connection.execute(delete(LedgerEntryRecord).where(
            LedgerEntryRecord.posting_id == original.ledger.id,
            LedgerEntryRecord.direction == "credit",
        ))

    response = api_client.post("/payments/reconciliation/runs", headers=headers)

    assert response.status_code == 201
    assert response.json()["report"]["status"] == "discrepancy"
    assert response.json()["report"]["findings"][0]["code"] == "ledger_structure_mismatch"
    stored = history.list_for_key("save-discrepancy-key")
    assert stored is not None and len(stored) == 1
    assert stored[0].model_dump(mode="json") == response.json()
    after = reader.load("save-discrepancy-key")
    assert after is not None and after.ledger is not None
    assert len(after.ledger.entries) == 1


@pytest.mark.parametrize("method", ["get", "post"])
def test_unknown_payment_cannot_be_saved_or_given_an_empty_history(api_client, history, method):
    response = getattr(api_client, method)(
        "/payments/reconciliation/runs", headers={"Idempotency-Key": "unknown-key"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "payment_not_found"
    assert history.list_for_key("unknown-key") is None


def test_failed_save_returns_unavailable_without_a_new_history_row(
    api_client, reader, history, api_processor, monkeypatch
):
    headers = {"Idempotency-Key": "failed-save-key"}
    assert api_client.post(
        "/payments", headers=headers, json={"amount": "25.00", "currency": "USD"}
    ).status_code == 201
    before = reader.load("failed-save-key")
    monkeypatch.setattr(history, "append", Mock(side_effect=SQLAlchemyError("Audit storage failed.")))

    response = api_client.post("/payments/reconciliation/runs", headers=headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "reconciliation_run_unavailable"
    assert history.list_for_key("failed-save-key") == []
    after = reader.load("failed-save-key")
    assert after is not None and before is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert api_processor.charge_count == 1


def test_failed_history_read_returns_unavailable_instead_of_an_empty_list(api_client, history, monkeypatch):
    monkeypatch.setattr(history, "list_for_key", Mock(side_effect=SQLAlchemyError("Read failed.")))

    response = api_client.get(
        "/payments/reconciliation/runs", headers={"Idempotency-Key": "failure-key"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "reconciliation_history_unavailable"
