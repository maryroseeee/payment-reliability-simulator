import pytest
from fastapi.testclient import TestClient

from payment_reliability.main import app


def test_new_payment_returns_a_successful_payment():
    """API contract; intentionally fails until payment processing is implemented."""
    # Given a new request to simulate a $25 USD payment.
    payment_request = {"amount": "25.00", "currency": "USD"}

    # When the client submits it with an idempotency key.
    with TestClient(app) as client:
        response = client.post(
            "/payments",
            headers={"Idempotency-Key": "order-42"},
            json=payment_request,
        )

    # Then the API reports a newly created, successful payment.
    assert response.status_code == 201
    payment = response.json()
    assert isinstance(payment["id"], str)
    assert payment["id"]
    assert payment["amount"] == "25.00"
    assert payment["currency"] == "USD"
    assert payment["status"] == "succeeded"


@pytest.mark.parametrize("amount", ["25.001", "-25.00", "0.00"])
def test_api_rejects_invalid_payment_amount(amount):
    with TestClient(app) as client:
        response = client.post(
            "/payments",
            headers={"Idempotency-Key": "order-42"},
            json={"amount": amount, "currency": "USD"},
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "amount"]
