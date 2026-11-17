"""Compare supplied records for a successful payment without reading or writing a database."""

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from payment_reliability.processor import ProcessorResult
from payment_reliability.schemas import Payment


class UnsupportedReconciliationStateError(ValueError):
    """This first reconciliation check supports succeeded payments only."""


class LedgerEntrySnapshot(BaseModel):
    """Describe a stored entry, including values a reconciliation check may reject."""

    model_config = ConfigDict(frozen=True)

    posting_id: UUID
    account: str
    direction: str
    amount: Decimal


class LedgerSnapshot(BaseModel):
    """Describe a stored posting even when its entries are incomplete or inconsistent."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    payment_id: UUID
    amount: Decimal
    currency: str
    entries: tuple[LedgerEntrySnapshot, ...]


class ReconciliationFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    payment_id: UUID
    posting_id: UUID | None
    status: Literal["consistent", "discrepancy"]
    findings: tuple[ReconciliationFinding, ...]


def _compare_fields(
    source: str, comparisons: Iterable[tuple[str, object, object]]
) -> list[ReconciliationFinding]:
    findings = []
    for field, expected, actual in comparisons:
        if expected != actual:
            findings.append(ReconciliationFinding(
                code=f"{source}_{field}_mismatch",
                message=(
                    f"{source.capitalize()} {field.replace('_', ' ')} is {actual}; "
                    f"expected {expected}."
                ),
            ))
    return findings


def reconcile_successful_payment(
    *, payment: Payment, provider: ProcessorResult | None, ledger: LedgerSnapshot | None
) -> ReconciliationReport:
    """Report disagreements for a succeeded payment; perform no repairs."""
    if payment.status != "succeeded":
        raise UnsupportedReconciliationStateError(
            "This reconciliation check requires a succeeded payment."
        )

    findings = []
    if payment.processor_transaction_id is None:
        findings.append(ReconciliationFinding(
            code="missing_processor_transaction_id",
            message="The successful internal payment has no processor transaction ID.",
        ))

    if provider is None:
        findings.append(ReconciliationFinding(
            code="missing_provider_record",
            message="The successful internal payment has no supplied provider confirmation.",
        ))
    else:
        findings.extend(_compare_fields("provider", (
            ("payment_id", payment.id, provider.payment_id),
            ("amount", payment.amount, provider.amount),
            ("currency", payment.currency, provider.currency),
            ("status", "succeeded", provider.status),
            ("transaction_id", payment.processor_transaction_id, provider.transaction_id),
        )))

    if ledger is None:
        findings.append(ReconciliationFinding(
            code="missing_ledger_posting",
            message=f"Successful payment for {payment.amount} {payment.currency} has no ledger posting.",
        ))
    else:
        findings.extend(_compare_fields("ledger", (
            ("payment_id", payment.id, ledger.payment_id),
            ("amount", payment.amount, ledger.amount),
            ("currency", payment.currency, ledger.currency),
        )))
        sides = sorted((entry.account, entry.direction) for entry in ledger.entries)
        if sides != [("merchant_payable", "credit"), ("processor_clearing", "debit")]:
            findings.append(ReconciliationFinding(
                code="ledger_structure_mismatch",
                message=(
                    "The posting must contain exactly one processor clearing debit "
                    "and one merchant payable credit."
                ),
            ))
        for entry in ledger.entries:
            if entry.posting_id != ledger.id:
                findings.append(ReconciliationFinding(
                    code="ledger_entry_posting_mismatch",
                    message=(
                        f"The {entry.account} {entry.direction} entry references posting "
                        f"{entry.posting_id}; expected {ledger.id}."
                    ),
                ))
            if entry.amount != payment.amount:
                findings.append(ReconciliationFinding(
                    code="ledger_entry_amount_mismatch",
                    message=(
                        f"The {entry.account} {entry.direction} entry records {entry.amount}; "
                        f"expected {payment.amount} {payment.currency}."
                    ),
                ))

    return ReconciliationReport(
        payment_id=payment.id,
        posting_id=None if ledger is None else ledger.id,
        status="discrepancy" if findings else "consistent",
        findings=tuple(findings),
    )
