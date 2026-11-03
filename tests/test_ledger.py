"""Accounting rules tested without HTTP requests or a database."""

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from payment_reliability.ledger import LedgerEntry, LedgerPosting, build_payment_posting
from payment_reliability.schemas import Payment


@pytest.fixture
def payment():
    return Payment(amount=Decimal("25.00"), currency="USD")


def test_build_payment_posting_records_funds_held_and_money_owed(payment):
    original = payment.model_dump()

    posting = build_payment_posting(payment)

    assert isinstance(posting.id, UUID)
    assert posting.payment_id == payment.id
    assert posting.amount == Decimal("25.00")
    assert posting.currency == "USD"
    assert [(entry.account, entry.direction, entry.amount) for entry in posting.entries] == [
        ("processor_clearing", "debit", Decimal("25.00")),
        ("merchant_payable", "credit", Decimal("25.00")),
    ]
    # Preparing accounting data does not mark a payment successful or save it.
    assert payment.model_dump() == original


@pytest.mark.parametrize(
    "debit,credit,expected_error",
    [
        ("25.00", "24.00", "must balance"),
        ("40.00", "40.00", "must match the payment amount"),
    ],
)
def test_posting_rejects_unbalanced_or_incorrect_amounts(
    payment, debit, credit, expected_error
):
    with pytest.raises(ValidationError, match=expected_error):
        LedgerPosting(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            entries=(
                LedgerEntry(account="processor_clearing", direction="debit", amount=debit),
                LedgerEntry(account="merchant_payable", direction="credit", amount=credit),
            ),
        )


@pytest.mark.parametrize(
    "accounts_and_directions",
    [
        (("processor_clearing", "credit"), ("merchant_payable", "debit")),
        (("processor_clearing", "debit"), ("processor_clearing", "credit")),
        (("processor_clearing", "debit"), ("merchant_payable", "debit")),
    ],
)
def test_payment_posting_rejects_wrong_accounts_or_accounting_sides(
    payment, accounts_and_directions
):
    entries = tuple(
        LedgerEntry(account=account, direction=direction, amount=payment.amount)
        for account, direction in accounts_and_directions
    )

    with pytest.raises(ValidationError, match="processor clearing debit and merchant payable credit"):
        LedgerPosting(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            entries=entries,
        )


@pytest.mark.parametrize("entry_count", [0, 1, 3])
def test_payment_posting_requires_exactly_two_entries(payment, entry_count):
    entry = LedgerEntry(
        account="processor_clearing", direction="debit", amount=payment.amount
    )

    with pytest.raises(ValidationError):
        LedgerPosting(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            entries=(entry,) * entry_count,
        )


@pytest.mark.parametrize("amount", ["0.00", "-25.00", "25.001", "NaN", "Infinity"])
def test_ledger_entry_rejects_invalid_amounts(amount):
    with pytest.raises(ValidationError):
        LedgerEntry(account="processor_clearing", direction="debit", amount=amount)


def test_posting_and_its_entries_cannot_be_edited(payment):
    posting = build_payment_posting(payment)

    with pytest.raises(ValidationError, match="frozen"):
        posting.amount = Decimal("40.00")
    with pytest.raises(ValidationError, match="frozen"):
        posting.entries[0].amount = Decimal("40.00")

    assert posting.amount == Decimal("25.00")
    assert posting.entries[0].amount == Decimal("25.00")


def test_posting_compares_large_amounts_without_rounding_away_a_cent(payment):
    amount = Decimal("123456789012345678901234567890.00")
    payment.amount = amount
    posting = build_payment_posting(payment)
    assert posting.entries[0].amount == amount
    assert posting.entries[1].amount == amount

    with pytest.raises(ValidationError, match="must balance"):
        LedgerPosting(
            payment_id=payment.id,
            amount=amount,
            currency=payment.currency,
            entries=(
                LedgerEntry(account="processor_clearing", direction="debit", amount=amount),
                LedgerEntry(
                    account="merchant_payable", direction="credit",
                    amount=Decimal("123456789012345678901234567890.01"),
                ),
            ),
        )
