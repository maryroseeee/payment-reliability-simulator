"""Provider records enforce valid, independent financial outcomes in PostgreSQL."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from payment_reliability.models import ProcessorOperationRecord


def operation_values():
    return {
        "idempotency_key": uuid4().hex,
        "payment_id": uuid4(),
        "amount": Decimal("25.00"),
        "currency": "USD",
        "status": "succeeded",
        "transaction_id": uuid4(),
    }


def test_provider_can_record_an_outcome_without_an_internal_payment(provider_store):
    # The isolated schema contains only the provider table, with no payments table.
    values = operation_values()

    record, created = provider_store.record_operation(**values)

    assert created is True
    assert dict(record) == values
    assert provider_store.get_by_idempotency_key(values["idempotency_key"]) == record
    assert provider_store.count_charges() == 1


@pytest.mark.parametrize("amount", ["0", "-1", "25.001", "NaN", "Infinity"])
def test_provider_database_rejects_invalid_amounts(provider_store, amount):
    values = operation_values()
    values["amount"] = Decimal(amount)

    with pytest.raises(IntegrityError) as error:
        with provider_store.engine.begin() as connection:
            connection.execute(insert(ProcessorOperationRecord), values)

    assert error.value.orig.sqlstate == "23514"  # check_violation
    assert provider_store.get_by_idempotency_key(values["idempotency_key"]) is None
    assert provider_store.count_charges() == 0


@pytest.mark.parametrize(
    "status,has_transaction_id",
    [("succeeded", False), ("declined", True), ("processing", False)],
)
def test_provider_database_rejects_inconsistent_outcomes(
    provider_store, status, has_transaction_id
):
    values = operation_values()
    values.update(
        status=status, transaction_id=uuid4() if has_transaction_id else None
    )

    with pytest.raises(IntegrityError) as error:
        with provider_store.engine.begin() as connection:
            connection.execute(insert(ProcessorOperationRecord), values)

    assert error.value.orig.sqlstate == "23514"
    assert provider_store.get_by_idempotency_key(values["idempotency_key"]) is None


def test_provider_database_rejects_unsupported_currency(provider_store):
    values = operation_values()
    values["currency"] = "EUR"

    with pytest.raises(IntegrityError) as error:
        with provider_store.engine.begin() as connection:
            connection.execute(insert(ProcessorOperationRecord), values)

    assert error.value.orig.sqlstate == "23514"
    assert provider_store.get_by_idempotency_key(values["idempotency_key"]) is None


def test_provider_cannot_reuse_a_transaction_id_for_another_key(provider_store):
    first = operation_values()
    second = operation_values()
    second["transaction_id"] = first["transaction_id"]
    provider_store.record_operation(**first)

    with pytest.raises(IntegrityError) as error:
        provider_store.record_operation(**second)

    assert error.value.orig.sqlstate == "23505"  # unique_violation
    assert provider_store.get_by_idempotency_key(first["idempotency_key"]) is not None
    assert provider_store.get_by_idempotency_key(second["idempotency_key"]) is None
    assert provider_store.count_charges() == 1
