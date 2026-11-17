"""Shared cleanup for tests that commit records to the local database."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.schema import CreateSchema, DropSchema

from payment_reliability.database import engine
from payment_reliability.models import (
    Base,
    LedgerEntryRecord,
    LedgerPostingRecord,
    PaymentRecord,
    ProcessorOperationRecord,
    ReconciliationRunRecord,
)
from payment_reliability.processor_store import ProcessorStore


@pytest.fixture
def reconciliation_engine():
    """All tables share an isolated schema for reconciliation and API tests."""
    schema = f"test_reconciliation_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_engine = engine.execution_options(schema_translate_map={None: schema})
    try:
        with scoped_engine.begin() as connection:
            Base.metadata.create_all(connection)
        yield scoped_engine
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


@pytest.fixture
def provider_store():
    """Each provider test has an isolated, committed PostgreSQL history."""
    schema = f"test_provider_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_engine = engine.execution_options(schema_translate_map={None: schema})
    try:
        with scoped_engine.begin() as connection:
            ProcessorOperationRecord.__table__.create(connection)
        yield ProcessorStore(scoped_engine)
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


@pytest.fixture
def idempotency_keys():
    keys = [f"test-payment-{uuid4().hex}" for _ in range(2)]
    try:
        yield keys
    finally:
        with engine.begin() as connection:
            payment_ids = select(PaymentRecord.id).where(
                PaymentRecord.idempotency_key.in_(keys)
            )
            posting_ids = select(LedgerPostingRecord.id).where(
                LedgerPostingRecord.payment_id.in_(payment_ids)
            )
            connection.execute(
                delete(ReconciliationRunRecord).where(ReconciliationRunRecord.payment_id.in_(payment_ids))
            )
            connection.execute(
                delete(LedgerEntryRecord).where(LedgerEntryRecord.posting_id.in_(posting_ids))
            )
            connection.execute(
                delete(LedgerPostingRecord).where(LedgerPostingRecord.payment_id.in_(payment_ids))
            )
            connection.execute(
                delete(PaymentRecord).where(PaymentRecord.idempotency_key.in_(keys))
            )


@pytest.fixture
def idempotency_key(idempotency_keys):
    return idempotency_keys[0]


@pytest.fixture
def ledger_records():
    """Read committed ledger data through an independent connection."""
    def read(payment_id):
        with engine.connect() as connection:
            postings = connection.execute(
                select(LedgerPostingRecord).where(LedgerPostingRecord.payment_id == payment_id)
            ).mappings().all()
            entries = connection.execute(
                select(LedgerEntryRecord)
                .join(LedgerPostingRecord)
                .where(LedgerPostingRecord.payment_id == payment_id)
                .order_by(LedgerEntryRecord.direction)
            ).mappings().all()
        return postings, entries

    return read
