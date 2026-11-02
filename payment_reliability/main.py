from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from payment_reliability.database import engine
from payment_reliability.processor import ProcessorError, SimulatedPaymentProcessor
from payment_reliability.schemas import Payment, PaymentRequest
from payment_reliability.service import (
    IdempotencyConflictError,
    PaymentInProgressError,
    PaymentRecordMissingError,
    PaymentService,
    PaymentStateError,
)
from payment_reliability.store import PaymentStore


app = FastAPI(title="Payment Reliability & Reconciliation Simulator")
_processor = SimulatedPaymentProcessor()


def get_payment_service() -> PaymentService:
    return PaymentService(PaymentStore(engine), _processor)


@app.post("/payments", status_code=status.HTTP_201_CREATED, response_model=Payment)
def create_payment(
    payment_request: PaymentRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    service: Annotated[PaymentService, Depends(get_payment_service)],
) -> Payment:
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain a non-whitespace character.",
        )

    try:
        return service.process_payment(idempotency_key, payment_request)
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_conflict", "message": str(error)},
        ) from error
    except PaymentInProgressError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "payment_in_progress", "message": str(error)},
        ) from error
    except (
        PaymentRecordMissingError,
        PaymentStateError,
        ProcessorError,
        SQLAlchemyError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "outcome_unconfirmed",
                "message": (
                    "The outcome could not be confirmed. "
                    "Retry using the same idempotency key."
                ),
            },
        ) from error
