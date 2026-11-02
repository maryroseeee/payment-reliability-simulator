"""Tests against PostgreSQL; every test rolls back its sample rows."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from payment_reliability.database import engine
from payment_reliability.models import PaymentRecord


@pytest.fixture
def db_connection():
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def payment_values():
    return {
        "id": uuid4(),
        "idempotency_key": uuid4().hex,
        "amount": Decimal("25.00"),
        "currency": "USD",
        "status": "processing",
    }


@pytest.mark.parametrize("duplicated_field", ["id", "idempotency_key"])
def test_database_rejects_duplicate_payment_identity(db_connection, duplicated_field):
    first_payment = payment_values()
    second_payment = payment_values()
    second_payment[duplicated_field] = first_payment[duplicated_field]

    db_connection.execute(insert(PaymentRecord), first_payment)

    with pytest.raises(IntegrityError) as error:
        db_connection.execute(insert(PaymentRecord), second_payment)

    # PostgreSQL's unique_violation code covers primary keys and unique keys.
    assert error.value.orig.sqlstate == "23505"
