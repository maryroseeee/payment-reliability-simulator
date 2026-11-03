"""Shared cleanup for tests that commit records to the local database."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.schema import CreateSchema, DropSchema

from payment_reliability.database import engine
from payment_reliability.models import PaymentRecord, ProcessorOperationRecord
from payment_reliability.processor_store import ProcessorStore


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
            connection.execute(
                delete(PaymentRecord).where(PaymentRecord.idempotency_key.in_(keys))
            )


@pytest.fixture
def idempotency_key(idempotency_keys):
    return idempotency_keys[0]
