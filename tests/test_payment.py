from uuid import UUID

from payment_reliability.schemas import Payment


def test_new_payment_starts_processing_with_a_generated_id():
    payment = Payment.model_validate({"amount": "25.00", "currency": "USD"})

    assert payment.status == "processing"
    assert isinstance(payment.id, UUID)


def test_payment_serializes_amount_as_decimal_text_and_id_as_text():
    payment = Payment.model_validate(
        {"amount": "25.00", "currency": "USD", "status": "processing"}
    )

    serialized = payment.model_dump(mode="json")

    assert serialized["amount"] == "25.00"
    assert serialized["id"] == str(payment.id)
