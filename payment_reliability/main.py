from fastapi import FastAPI, HTTPException, status

from payment_reliability.schemas import PaymentRequest


app = FastAPI(title="Payment Reliability & Reconciliation Simulator")


@app.post("/payments", status_code=status.HTTP_201_CREATED)
def create_payment(payment_request: PaymentRequest):
    # FastAPI validates the request before this function runs.
    # The next implementation step will call the payment component here.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Payment processing is not implemented yet.",
    )
