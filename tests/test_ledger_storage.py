"""Ledger persistence, transaction ownership, and failure recovery in PostgreSQL."""

from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from payment_reliability.database import engine
from payment_reliability.ledger import build_payment_posting
from payment_reliability.ledger_store import save_posting
from payment_reliability.models import LedgerEntryRecord, LedgerPostingRecord
from payment_reliability.processor import SimulatedPaymentProcessor
from payment_reliability.schemas import PaymentRequest
from payment_reliability.service import PaymentInProgressError, PaymentService
from payment_reliability.store import PaymentStore


@pytest.fixture
def payment_request():
    return PaymentRequest(amount=Decimal("25.00"), currency="USD")


def test_ledger_helper_leaves_commit_and_rollback_to_the_caller(
    idempotency_key, payment_request, ledger_records
):
    payment = PaymentStore(engine).claim(idempotency_key, payment_request)
    assert payment is not None
    posting = build_payment_posting(payment)

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            save_posting(connection, posting)
            assert connection.in_transaction()
            assert connection.execute(
                select(func.count()).select_from(LedgerEntryRecord)
                .where(LedgerEntryRecord.posting_id == posting.id)
            ).scalar_one() == 2
            # Another connection must not see the caller's uncommitted writes.
            assert ledger_records(payment.id) == ([], [])
        finally:
            transaction.rollback()

    assert ledger_records(payment.id) == ([], [])


def test_ledger_helper_requires_an_active_transaction(
    idempotency_key, payment_request, ledger_records
):
    payment = PaymentStore(engine).claim(idempotency_key, payment_request)
    assert payment is not None

    with engine.connect() as connection:
        with pytest.raises(ValueError, match="active transaction"):
            save_posting(connection, build_payment_posting(payment))

    assert ledger_records(payment.id) == ([], [])


def test_database_rejects_a_second_posting_for_the_same_payment(
    idempotency_key, payment_request, ledger_records
):
    store = PaymentStore(engine)
    payment = store.claim(idempotency_key, payment_request)
    assert payment is not None
    assert store.mark_succeeded(payment.id, uuid4()) is not None
    original = ledger_records(payment.id)
    duplicate = build_payment_posting(payment)
    # Different posting IDs isolate the unique payment-ID constraint.
    assert duplicate.id != original[0][0]["id"]

    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            save_posting(connection, duplicate)

    assert error.value.orig.sqlstate == "23505"
    assert ledger_records(payment.id) == original


def test_database_rejects_a_duplicate_accounting_side(
    idempotency_key, payment_request, ledger_records
):
    store = PaymentStore(engine)
    payment = store.claim(idempotency_key, payment_request)
    assert payment is not None
    assert store.mark_succeeded(payment.id, uuid4()) is not None
    original = ledger_records(payment.id)

    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            connection.execute(insert(LedgerEntryRecord).values(
                posting_id=original[0][0]["id"], direction="debit",
                account="processor_clearing", amount=payment.amount,
            ))

    assert error.value.orig.sqlstate == "23505"
    assert ledger_records(payment.id) == original


@pytest.mark.parametrize(
    "failed_table,entries_written",
    [("ledger_postings", 0), ("ledger_entries", 1), ("ledger_entries", 2)],
)
def test_partial_ledger_failure_rolls_back_success_and_recovery_posts_once(
    idempotency_key, payment_request, provider_store, ledger_records, monkeypatch,
    failed_table, entries_written,
):
    processor = SimulatedPaymentProcessor(provider_store)
    process = Mock(wraps=processor.process)
    monkeypatch.setattr(processor, "process", process)
    store = PaymentStore(engine)
    service = PaymentService(store, processor)
    inserted_entries = 0

    def fail_after_write(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserted_entries
        if not context.isinsert or context.compiled is None:
            return
        table = context.compiled.statement.table.name
        if table == "ledger_entries":
            inserted_entries += 1
        if table != failed_table or inserted_entries != entries_written:
            return
        values = context.compiled_parameters[0]
        posting_id = values["id"] if table == "ledger_postings" else values["posting_id"]
        # The real INSERT has happened; prove partial data exists before failing.
        assert connection.execute(
            select(func.count()).select_from(LedgerEntryRecord)
            .where(LedgerEntryRecord.posting_id == posting_id)
        ).scalar_one() == entries_written
        raise SQLAlchemyError("Injected failure during ledger storage.")

    event.listen(engine, "after_cursor_execute", fail_after_write)
    try:
        with pytest.raises(SQLAlchemyError, match="Injected failure"):
            service.process_payment(idempotency_key, payment_request)
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_write)

    payment = store.get_by_idempotency_key(idempotency_key)
    assert payment is not None
    assert payment.status == "processing"
    assert payment.processor_transaction_id is None
    assert ledger_records(payment.id) == ([], [])
    confirmation = processor.get_result(idempotency_key)
    assert confirmation is not None
    assert confirmation.status == "succeeded"
    assert processor.charge_count == 1

    with pytest.raises(PaymentInProgressError):
        service.process_payment(idempotency_key, payment_request)

    recovered = service.recover_payment(idempotency_key)

    assert recovered.status == "succeeded"
    assert recovered.id == payment.id
    assert recovered.processor_transaction_id == confirmation.transaction_id
    saved = ledger_records(payment.id)
    assert len(saved[0]) == 1
    assert len(saved[1]) == 2
    assert {entry["amount"] for entry in saved[1]} == {Decimal("25.00")}
    assert service.recover_payment(idempotency_key) == recovered
    assert service.process_payment(idempotency_key, payment_request) == recovered
    assert ledger_records(payment.id) == saved
    process.assert_called_once()
    assert processor.charge_count == 1
