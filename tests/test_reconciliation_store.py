"""Reconciliation loads committed records from one read-only PostgreSQL snapshot."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.exc import DBAPIError
from payment_reliability.models import (
    LedgerEntryRecord,
    PaymentRecord,
)
from payment_reliability.processor import ProcessorTimeoutError, SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.reconciliation import reconcile_successful_payment
from payment_reliability.reconciliation_store import (
    ReconciliationStore,
    reconcile_stored_payment,
)
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def payment_system(reconciliation_engine):
    store = PaymentStore(reconciliation_engine)
    processor = SimulatedPaymentProcessor(ProcessorStore(reconciliation_engine))
    return store, processor, PaymentService(store, processor)


@pytest.fixture
def payment_request():
    return PaymentRequest(amount=Decimal("25.00"), currency="USD")


def test_loader_links_each_payment_to_its_provider_record_and_ledger(
    reconciliation_engine, payment_system, payment_request
):
    store, processor, service = payment_system
    first = service.process_payment("first-key", payment_request)
    second = service.process_payment("second-key", payment_request)
    reader = ReconciliationStore(reconciliation_engine)

    for key, payment in [("first-key", first), ("second-key", second)]:
        snapshot = reader.load(key)

        assert snapshot is not None
        assert snapshot.payment == payment
        assert snapshot.provider == processor.get_result(key)
        assert snapshot.provider is not None
        assert snapshot.provider.transaction_id == payment.processor_transaction_id
        assert snapshot.ledger is not None
        assert snapshot.ledger.payment_id == payment.id
        assert snapshot.ledger.id != payment.id
        assert snapshot.ledger.amount == Decimal("25.00")
        assert snapshot.ledger.currency == "USD"
        assert len(snapshot.ledger.entries) == 2
        assert all(entry.posting_id == snapshot.ledger.id for entry in snapshot.ledger.entries)
        report = reconcile_stored_payment(reader, key)
        assert report is not None
        assert report.status == "consistent"
        assert report.payment_id == payment.id
        assert report.posting_id == snapshot.ledger.id
        assert report.findings == ()
        reloaded = reader.load(key)
        assert reloaded is not None
        assert reloaded.model_dump(exclude={"checked_at"}) == snapshot.model_dump(exclude={"checked_at"})

    assert processor.charge_count == 2
    # Read-only settings must not leak into later ordinary payment transactions.
    assert store.claim("third-key", payment_request) is not None


def test_unknown_internal_key_does_not_create_a_payment_even_if_provider_record_exists(
    reconciliation_engine, payment_system
):
    store, processor, _ = payment_system
    processor.process(
        payment_id=uuid4(), amount=Decimal("25.00"), currency="USD",
        idempotency_key="provider-only-key",
    )
    reader = ReconciliationStore(reconciliation_engine)

    assert reader.load("provider-only-key") is None
    assert reconcile_stored_payment(reader, "provider-only-key") is None
    assert store.get_by_idempotency_key("provider-only-key") is None
    assert processor.get_result("provider-only-key") is not None
    assert processor.charge_count == 1


def test_legacy_success_without_a_ledger_is_reported_without_backfilling(
    reconciliation_engine, payment_system, payment_request
):
    store, processor, _ = payment_system
    payment = store.claim("legacy-key", payment_request)
    assert payment is not None
    confirmation = processor.process(
        payment_id=payment.id, amount=payment.amount, currency=payment.currency,
        idempotency_key="legacy-key",
    )
    # Model an existing success from before ledger storage was introduced.
    with reconciliation_engine.begin() as connection:
        connection.execute(update(PaymentRecord).where(PaymentRecord.id == payment.id).values(
            status="succeeded", processor_transaction_id=confirmation.transaction_id,
        ))
    reader = ReconciliationStore(reconciliation_engine)
    before = reader.load("legacy-key")

    report = reconcile_stored_payment(reader, "legacy-key")

    assert report is not None
    assert report.status == "discrepancy"
    assert [finding.code for finding in report.findings] == ["missing_ledger_posting"]
    after = reader.load("legacy-key")
    assert after is not None and before is not None
    assert after.model_dump(exclude={"checked_at"}) == before.model_dump(exclude={"checked_at"})
    assert before is not None and before.ledger is None
    assert processor.charge_count == 1


def test_missing_provider_record_is_loaded_as_missing_evidence(
    reconciliation_engine, payment_system, payment_request
):
    store, processor, _ = payment_system
    payment = store.claim("no-provider-key", payment_request)
    assert payment is not None
    assert store.mark_succeeded(payment.id, uuid4()) is not None
    reader = ReconciliationStore(reconciliation_engine)

    snapshot = reader.load("no-provider-key")
    report = reconcile_stored_payment(reader, "no-provider-key")

    assert snapshot is not None
    assert snapshot.provider is None
    assert snapshot.ledger is not None
    assert report is not None
    assert [finding.code for finding in report.findings] == ["missing_provider_record"]
    assert processor.charge_count == 0


@pytest.mark.parametrize("problem", ["missing_entry", "wrong_amount"])
def test_loader_passes_inconsistent_stored_entries_to_the_checker_without_repair(
    reconciliation_engine, payment_system, payment_request, problem
):
    _, processor, service = payment_system
    service.process_payment("damaged-key", payment_request)
    reader = ReconciliationStore(reconciliation_engine)
    original = reader.load("damaged-key")
    assert original is not None and original.ledger is not None
    with reconciliation_engine.begin() as connection:
        if problem == "missing_entry":
            connection.execute(delete(LedgerEntryRecord).where(
                LedgerEntryRecord.posting_id == original.ledger.id,
                LedgerEntryRecord.direction == "credit",
            ))
        else:
            connection.execute(update(LedgerEntryRecord).where(
                LedgerEntryRecord.posting_id == original.ledger.id,
            ).values(amount=Decimal("40.00")))
    damaged = reader.load("damaged-key")

    report = reconcile_stored_payment(reader, "damaged-key")

    assert report is not None
    assert report.status == "discrepancy"
    if problem == "missing_entry":
        assert [finding.code for finding in report.findings] == ["ledger_structure_mismatch"]
        assert damaged is not None and damaged.ledger is not None
        assert len(damaged.ledger.entries) == 1
    else:
        assert [finding.code for finding in report.findings] == [
            "ledger_entry_amount_mismatch", "ledger_entry_amount_mismatch",
        ]
    after = reader.load("damaged-key")
    assert after is not None and damaged is not None
    assert after.model_dump(exclude={"checked_at"}) == damaged.model_dump(exclude={"checked_at"})
    assert processor.charge_count == 1


def test_recovery_committed_between_reads_does_not_mix_database_versions(
    reconciliation_engine, payment_system, payment_request
):
    store, processor, service = payment_system
    processor.lose_response = True
    with pytest.raises(ProcessorTimeoutError):
        service.process_payment("recovering-key", payment_request)
    reader = ReconciliationStore(reconciliation_engine)
    recovery_started = False
    recovered = None

    def recover_after_payment_query(connection, cursor, statement, parameters, context, executemany):
        nonlocal recovery_started, recovered
        if recovery_started or context.compiled is None:
            return
        query = context.compiled.statement
        if query.is_select and PaymentRecord.__table__ in query.get_final_froms():
            # Prevent recursion when recovery uses another connection on this engine.
            recovery_started = True
            recovered = service.recover_payment("recovering-key")

    event.listen(reconciliation_engine, "after_cursor_execute", recover_after_payment_query)
    try:
        snapshot = reader.load("recovering-key")
    finally:
        event.remove(reconciliation_engine, "after_cursor_execute", recover_after_payment_query)

    assert recovery_started and recovered is not None
    assert recovered.status == "succeeded"
    assert store.get_by_idempotency_key("recovering-key").status == "succeeded"
    assert snapshot is not None
    assert snapshot.payment.status == "processing"
    assert snapshot.payment.processor_transaction_id is None
    assert snapshot.provider is not None and snapshot.provider.status == "succeeded"
    # This reader must see the original absence, not recovery's newly committed ledger.
    assert snapshot.ledger is None
    fresh = reader.load("recovering-key")
    assert fresh is not None and fresh.ledger is not None
    assert fresh.payment.status == "succeeded"
    assert len(fresh.ledger.entries) == 2
    assert reconcile_successful_payment(
        payment=fresh.payment, provider=fresh.provider, ledger=fresh.ledger
    ).status == "consistent"
    assert processor.charge_count == 1


def test_postgresql_rejects_writes_during_snapshot_loading(
    reconciliation_engine, payment_system, payment_request
):
    store, _, service = payment_system
    payment = service.process_payment("read-only-key", payment_request)
    attempted = False

    def attempt_write_after_query(connection, cursor, statement, parameters, context, executemany):
        nonlocal attempted
        if attempted or context.compiled is None:
            return
        query = context.compiled.statement
        if query.is_select and PaymentRecord.__table__ in query.get_final_froms():
            attempted = True
            connection.execute(update(PaymentRecord).where(
                PaymentRecord.id == payment.id
            ).values(amount=Decimal("40.00")))

    event.listen(reconciliation_engine, "after_cursor_execute", attempt_write_after_query)
    try:
        with pytest.raises(DBAPIError) as error:
            ReconciliationStore(reconciliation_engine).load("read-only-key")
    finally:
        event.remove(reconciliation_engine, "after_cursor_execute", attempt_write_after_query)

    assert attempted
    assert error.value.orig.sqlstate == "25006"  # read_only_sql_transaction
    assert store.get_by_idempotency_key("read-only-key") == payment


@pytest.mark.parametrize("decline", [False, True])
def test_loader_supports_other_states_while_the_first_checker_remains_success_only(
    reconciliation_engine, payment_system, payment_request, decline
):
    store, processor, service = payment_system
    if decline:
        processor.decline = True
        service.process_payment("unfinished-check-key", payment_request)
    else:
        store.claim("unfinished-check-key", payment_request)
    reader = ReconciliationStore(reconciliation_engine)

    snapshot = reader.load("unfinished-check-key")

    assert snapshot is not None
    assert snapshot.payment.status == ("declined" if decline else "processing")
    assert snapshot.ledger is None
    with pytest.raises(ValueError, match="requires a succeeded payment"):
        reconcile_stored_payment(reader, "unfinished-check-key")
