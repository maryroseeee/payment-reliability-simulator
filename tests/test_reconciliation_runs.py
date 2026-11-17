"""Saved observations survive later checks and changes to financial records."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from payment_reliability.ledger import build_payment_posting
from payment_reliability.ledger_store import save_posting
from payment_reliability.models import LedgerEntryRecord, LedgerPostingRecord, ReconciliationRunRecord
from payment_reliability.processor import SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.reconciliation_runs import ReconciliationRunStore, create_reconciliation_run
from payment_reliability.reconciliation_store import ReconciliationStore
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def system(reconciliation_engine):
    store = PaymentStore(reconciliation_engine)
    processor = SimulatedPaymentProcessor(ProcessorStore(reconciliation_engine))
    return store, processor, PaymentService(store, processor)


@pytest.fixture
def reader(reconciliation_engine):
    return ReconciliationStore(reconciliation_engine)


@pytest.fixture
def history(reconciliation_engine):
    return ReconciliationRunStore(reconciliation_engine)


@pytest.fixture
def successful_payment(system):
    return system[2].process_payment("run-key", PaymentRequest(amount="25.00", currency="USD"))


def test_two_runs_preserve_the_original_discrepancy_after_the_ledger_is_corrected(
    reconciliation_engine, system, reader, history, successful_payment
):
    original = reader.load("run-key")
    assert original is not None and original.ledger is not None
    with reconciliation_engine.begin() as connection:
        connection.execute(delete(LedgerEntryRecord).where(
            LedgerEntryRecord.posting_id == original.ledger.id
        ))
        connection.execute(delete(LedgerPostingRecord).where(LedgerPostingRecord.id == original.ledger.id))

    first = create_reconciliation_run(reader, history, "run-key")
    assert first is not None
    first_copy = first.model_dump(mode="json")
    assert first.report.status == "discrepancy"
    assert first.report.findings[0].code == "missing_ledger_posting"

    # Correct only this test's simulated accounting history, then inspect it again.
    with reconciliation_engine.begin() as connection:
        save_posting(connection, build_payment_posting(successful_payment))
    second = create_reconciliation_run(reader, history, "run-key")

    assert second is not None
    assert first.id != second.id
    assert first.payment_id == second.payment_id == successful_payment.id
    assert first.checked_at.tzinfo is not None
    assert first.checked_at <= second.checked_at
    assert second.report.status == "consistent"
    saved = ReconciliationRunStore(reconciliation_engine).list_for_key("run-key")
    assert saved is not None
    assert [run.id for run in saved] == [first.id, second.id]
    assert saved[0].model_dump(mode="json") == first_copy
    assert saved[1] == second
    assert system[1].charge_count == 1


def test_saved_run_keeps_the_original_observation_time_and_report_if_sources_change(
    reconciliation_engine, reader, history, successful_payment, monkeypatch
):
    observed = reader.load("run-key")
    assert observed is not None and observed.ledger is not None
    monkeypatch.setattr(reader, "load", lambda key: observed)
    with reconciliation_engine.begin() as connection:
        connection.execute(update(LedgerEntryRecord).where(
            LedgerEntryRecord.posting_id == observed.ledger.id
        ).values(amount="40.00"))

    old_observation = create_reconciliation_run(reader, history, "run-key")
    new_observation = create_reconciliation_run(
        ReconciliationStore(reconciliation_engine), history, "run-key"
    )

    assert old_observation is not None and new_observation is not None
    assert old_observation.checked_at == observed.checked_at
    assert old_observation.report.status == "consistent"
    assert new_observation.report.status == "discrepancy"
    assert history.list_for_key("run-key") == [old_observation, new_observation]


def test_concurrent_checks_append_separate_runs_without_another_charge(
    system, reader, history, successful_payment
):
    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(create_reconciliation_run, reader, history, "run-key") for _ in range(2)]
        runs = [attempt.result(timeout=10) for attempt in attempts]

    assert all(run is not None and run.report.status == "consistent" for run in runs)
    assert len({run.id for run in runs}) == 2
    saved = history.list_for_key("run-key")
    assert saved is not None
    assert {run.id for run in saved} == {run.id for run in runs}
    assert system[1].charge_count == 1


def test_failed_audit_insert_rolls_back_without_losing_history_or_changing_finances(
    reconciliation_engine, reader, history, successful_payment, system
):
    first = create_reconciliation_run(reader, history, "run-key")
    before = reader.load("run-key")

    def fail_after_insert(connection, cursor, statement, parameters, context, executemany):
        if context.isinsert and context.compiled.statement.table.name == "reconciliation_runs":
            raise SQLAlchemyError("Injected audit failure before commit.")

    event.listen(reconciliation_engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(SQLAlchemyError, match="Injected audit failure"):
            create_reconciliation_run(reader, history, "run-key")
    finally:
        event.remove(reconciliation_engine, "after_cursor_execute", fail_after_insert)

    assert history.list_for_key("run-key") == [first]
    after = reader.load("run-key")
    assert before is not None and after is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert system[1].charge_count == 1


def test_unknown_payment_and_known_payment_with_no_runs_are_distinct(
    reader, history, successful_payment
):
    assert history.list_for_key("run-key") == []
    assert history.list_for_key("unknown-key") is None
    assert create_reconciliation_run(reader, history, "unknown-key") is None
    assert history.list_for_key("run-key") == []


@pytest.mark.parametrize("decline", [False, True])
def test_unsupported_payments_do_not_create_a_saved_run(system, reader, history, decline):
    store, processor, service = system
    request = PaymentRequest(amount="25.00", currency="USD")
    if decline:
        processor.decline = True
        service.process_payment("unsupported-key", request)
    else:
        store.claim("unsupported-key", request)

    with pytest.raises(ValueError, match="requires a succeeded payment"):
        create_reconciliation_run(reader, history, "unsupported-key")

    assert history.list_for_key("unsupported-key") == []
    assert processor.charge_count == 0


def test_database_rejects_a_report_attached_to_the_wrong_payment(
    reconciliation_engine, reader, history, successful_payment
):
    snapshot = reader.load("run-key")
    assert snapshot is not None
    invalid_report = {
        "payment_id": str(uuid4()), "posting_id": None,
        "status": "consistent", "findings": [],
    }

    with pytest.raises(IntegrityError) as error:
        with reconciliation_engine.begin() as connection:
            connection.execute(insert(ReconciliationRunRecord).values(
                id=uuid4(), payment_id=successful_payment.id,
                checked_at=snapshot.checked_at, report=invalid_report,
            ))

    assert error.value.orig.sqlstate == "23514"
    assert history.list_for_key("run-key") == []
