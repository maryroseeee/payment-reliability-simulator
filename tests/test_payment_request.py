from decimal import Decimal

import pytest
from pydantic import ValidationError

from payment_reliability.schemas import PaymentRequest


@pytest.mark.parametrize("amount", ["0.01", "25.00"])
def test_valid_usd_amount_is_preserved_as_a_decimal(amount):
    request = PaymentRequest.model_validate({"amount": amount, "currency": "USD"})

    assert isinstance(request.amount, Decimal)
    assert request.amount == Decimal(amount)
    assert request.currency == "USD"


@pytest.mark.parametrize("amount", ["25.001", "-25.00", "0.00"])
def test_invalid_usd_amount_is_rejected(amount):
    with pytest.raises(ValidationError) as error:
        PaymentRequest.model_validate({"amount": amount, "currency": "USD"})

    assert error.value.errors()[0]["loc"] == ("amount",)
