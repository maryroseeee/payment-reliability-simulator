"""Prepare accounting data for one simulated USD payment; no database writes."""

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from payment_reliability.schemas import Payment


class LedgerEntry(BaseModel):
    """One accounting side of a payment's financial effect."""

    model_config = ConfigDict(frozen=True)

    account: Literal["processor_clearing", "merchant_payable"]
    direction: Literal["debit", "credit"]
    amount: Decimal = Field(gt=0, decimal_places=2)


class LedgerPosting(BaseModel):
    """Exactly two balanced entries, tied to a payment and its expected amount."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    payment_id: UUID
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: Literal["USD"]
    entries: tuple[LedgerEntry, LedgerEntry]

    @model_validator(mode="after")
    def validate_payment_entries(self) -> "LedgerPosting":
        accounts_and_directions = {
            (entry.account, entry.direction) for entry in self.entries
        }
        if accounts_and_directions != {
            ("processor_clearing", "debit"),
            ("merchant_payable", "credit"),
        }:
            raise ValueError(
                "Payment postings require a processor clearing debit and merchant payable credit."
            )

        debit = next(entry for entry in self.entries if entry.direction == "debit")
        credit = next(entry for entry in self.entries if entry.direction == "credit")
        # There is exactly one of each, so compare directly without arithmetic rounding.
        if debit.amount != credit.amount:
            raise ValueError("The payment's debit and credit must balance.")
        if debit.amount != self.amount:
            raise ValueError("Both entries must match the payment amount.")
        return self


def build_payment_posting(payment: Payment) -> LedgerPosting:
    """Prepare a posting; the caller decides when a confirmed outcome can be saved."""
    return LedgerPosting(
        payment_id=payment.id,
        amount=payment.amount,
        currency=payment.currency,
        entries=(
            LedgerEntry(
                account="processor_clearing", direction="debit", amount=payment.amount
            ),
            LedgerEntry(
                account="merchant_payable", direction="credit", amount=payment.amount
            ),
        ),
    )
