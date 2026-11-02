"""Shared cleanup for tests that commit records to the local database."""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from payment_reliability.database import engine
from payment_reliability.models import PaymentRecord


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
