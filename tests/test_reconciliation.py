"""Read-only reconciliation rules tested with records supplied in memory."""

from decimal import Decimal
from uuid import uuid4

import pytest

from payment_reliability.ledger import build_payment_posting
from payment_reliability.processor import ProcessorResult
from payment_reliability.reconciliation import (
    LedgerEntrySnapshot,
    LedgerSnapshot,
    reconcile_successful_payment,
)
from payment_reliability.schemas import Payment


@pytest.fixture
def matching_records():
    transaction_id = uuid4()
    payment = Payment(
        amount=Decimal("25.00"), currency="USD", status="succeeded",
        processor_transaction_id=transaction_id,
    )
    provider = ProcessorResult(
        payment_id=payment.id, amount=payment.amount, currency=payment.currency,
        transaction_id=transaction_id,
    )
    posting = build_payment_posting(payment)
    ledger = LedgerSnapshot(
        id=posting.id, payment_id=posting.payment_id,
        amount=posting.amount, currency=posting.currency,
        entries=tuple(
            LedgerEntrySnapshot(posting_id=posting.id, **entry.model_dump())
            for entry in posting.entries
        ),
    )
    return {"payment": payment, "provider": provider, "ledger": ledger}


def test_confirmed_success_without_a_posting_reports_missing_ledger(matching_records):
    matching_records["ledger"] = None
    payment = matching_records["payment"]
    before = payment.model_dump()

    report = reconcile_successful_payment(**matching_records)

    assert report.payment_id == payment.id
    assert report.posting_id is None
    assert report.status == "discrepancy"
    assert len(report.findings) == 1
    assert report.findings[0].code == "missing_ledger_posting"
    assert report.findings[0].message == (
        "Successful payment for 25.00 USD has no ledger posting."
    )
    assert report.model_dump(mode="json")["payment_id"] == str(payment.id)
    assert payment.model_dump() == before


def test_matching_records_are_consistent_without_changing_any_input(matching_records):
    before = {name: record.model_dump() for name, record in matching_records.items()}

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "consistent"
    assert report.findings == ()
    assert report.payment_id == matching_records["payment"].id
    assert report.posting_id == matching_records["ledger"].id
    # Payment IDs and posting IDs identify different kinds of records.
    assert report.payment_id != report.posting_id
    assert {name: record.model_dump() for name, record in matching_records.items()} == before


def test_posting_for_another_payment_is_reported_without_relinking_it(matching_records):
    wrong_payment_id = uuid4()
    matching_records["ledger"] = matching_records["ledger"].model_copy(
        update={"payment_id": wrong_payment_id}
    )

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert [finding.code for finding in report.findings] == ["ledger_payment_id_mismatch"]
    assert str(wrong_payment_id) in report.findings[0].message
    assert str(matching_records["payment"].id) in report.findings[0].message
    assert matching_records["ledger"].payment_id == wrong_payment_id


def test_entry_linked_to_another_posting_is_reported(matching_records):
    ledger = matching_records["ledger"]
    wrong_entry = ledger.entries[0].model_copy(update={"posting_id": uuid4()})
    matching_records["ledger"] = ledger.model_copy(
        update={"entries": (wrong_entry, ledger.entries[1])}
    )

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert [finding.code for finding in report.findings] == ["ledger_entry_posting_mismatch"]


@pytest.mark.parametrize("field", ["payment_id", "amount", "currency", "transaction_id", "status"])
def test_provider_disagreements_cannot_be_reported_as_consistent(matching_records, field):
    values = matching_records["provider"].model_dump()
    values[field] = {
        "payment_id": uuid4(), "amount": Decimal("40.00"), "currency": "EUR",
        "transaction_id": uuid4(), "status": "declined",
    }[field]
    if field == "status":
        values["transaction_id"] = None
    matching_records["provider"] = ProcessorResult(**values)

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert f"provider_{field}_mismatch" in {finding.code for finding in report.findings}


def test_missing_provider_confirmation_is_reported(matching_records):
    matching_records["provider"] = None

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert [finding.code for finding in report.findings] == ["missing_provider_record"]


@pytest.mark.parametrize("field,value", [("amount", Decimal("40.00")), ("currency", "EUR")])
def test_posting_details_must_match_the_payment(matching_records, field, value):
    matching_records["ledger"] = matching_records["ledger"].model_copy(update={field: value})

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert f"ledger_{field}_mismatch" in {finding.code for finding in report.findings}


def test_balanced_entries_for_the_wrong_amount_are_reported(matching_records):
    ledger = matching_records["ledger"]
    matching_records["ledger"] = ledger.model_copy(update={
        "entries": tuple(entry.model_copy(update={"amount": Decimal("40.00")}) for entry in ledger.entries)
    })

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert [finding.code for finding in report.findings] == [
        "ledger_entry_amount_mismatch", "ledger_entry_amount_mismatch",
    ]


@pytest.mark.parametrize("entry_count", [0, 1, 3])
def test_incomplete_or_duplicated_entries_can_be_inspected_and_reported(
    matching_records, entry_count
):
    ledger = matching_records["ledger"]
    entries = (ledger.entries + ledger.entries)[:entry_count]
    # Read snapshots must accept broken groups that the write model rejects.
    matching_records["ledger"] = LedgerSnapshot(
        id=ledger.id, payment_id=ledger.payment_id,
        amount=ledger.amount, currency=ledger.currency, entries=entries,
    )

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert "ledger_structure_mismatch" in {finding.code for finding in report.findings}


def test_success_without_an_internal_processor_reference_is_reported(matching_records):
    matching_records["payment"].processor_transaction_id = None

    report = reconcile_successful_payment(**matching_records)

    assert report.status == "discrepancy"
    assert "missing_processor_transaction_id" in {finding.code for finding in report.findings}


@pytest.mark.parametrize("status", ["processing", "declined"])
def test_this_first_check_explicitly_requires_a_successful_payment(matching_records, status):
    matching_records["payment"].status = status
    matching_records["payment"].processor_transaction_id = None

    with pytest.raises(ValueError, match="requires a succeeded payment"):
        reconcile_successful_payment(**matching_records)
