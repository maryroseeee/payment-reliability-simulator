from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class PaymentRequest(BaseModel):
    """Client-supplied details for an initial USD payment."""

    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: Literal["USD"]


class Payment(BaseModel):
    """Payment data; constructing this object does not save or process it."""

    id: UUID = Field(default_factory=uuid4)
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: Literal["USD"]
    status: Literal["processing", "succeeded"] = "processing"
    processor_transaction_id: UUID | None = None
