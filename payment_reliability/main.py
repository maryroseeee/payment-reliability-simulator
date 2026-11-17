import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from payment_reliability.database import engine
from payment_reliability.processor import ProcessorError, SimulatedPaymentProcessor
from payment_reliability.processor_store import ProcessorStore
from payment_reliability.reconciliation import (
    ReconciliationReport,
    UnsupportedReconciliationStateError,
)
from payment_reliability.reconciliation_runs import (
    ReconciliationRun,
    ReconciliationRunStore,
    create_reconciliation_run,
)
from payment_reliability.reconciliation_store import ReconciliationStore, reconcile_stored_payment
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
_processor = SimulatedPaymentProcessor(
    ProcessorStore(engine),
    decline=os.getenv("SIMULATE_DECLINES", "0") == "1",
    lose_response=os.getenv("SIMULATE_LOST_RESPONSE", "0") == "1",
)


def get_payment_service() -> PaymentService:
    return PaymentService(PaymentStore(engine), _processor)


def get_reconciliation_store() -> ReconciliationStore:
    return ReconciliationStore(engine)


def get_reconciliation_run_store() -> ReconciliationRunStore:
    return ReconciliationRunStore(engine)


def get_idempotency_key(
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> str:
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain a non-whitespace character.",
        )
    return idempotency_key


def _unconfirmed_outcome_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "outcome_unconfirmed",
            "message": (
                "The outcome could not be confirmed. "
                "Retry using the same idempotency key."
            ),
        },
    )


def _reconciliation_error(http_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/payments", status_code=status.HTTP_201_CREATED, response_model=Payment)
def create_payment(
    payment_request: PaymentRequest,
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    service: Annotated[PaymentService, Depends(get_payment_service)],
) -> Payment:

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
        raise _unconfirmed_outcome_error() from error


@app.post(
    "/payments/recover",
    status_code=status.HTTP_200_OK,
    response_model=Payment,
    responses={
        404: {"description": "No internal payment exists for this key."},
        503: {"description": "The recovery outcome could not be confirmed."},
    },
)
def recover_payment(
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    service: Annotated[PaymentService, Depends(get_payment_service)],
) -> Payment:
    """Recover an existing outcome; missing confirmation leaves it processing."""
    try:
        return service.recover_payment(idempotency_key)
    except PaymentRecordMissingError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "payment_not_found", "message": str(error)},
        ) from error
    except (PaymentStateError, ProcessorError, SQLAlchemyError) as error:
        raise _unconfirmed_outcome_error() from error


@app.get(
    "/payments/reconciliation",
    response_model=ReconciliationReport,
    responses={
        200: {"description": "A completed check, including any discrepancy findings."},
        404: {"description": "No internal payment exists for this key."},
        409: {"description": "This version checks succeeded payments only."},
        503: {"description": "The reconciliation report could not be generated."},
    },
)
def inspect_reconciliation(
    response: Response,
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    store: Annotated[ReconciliationStore, Depends(get_reconciliation_store)],
) -> ReconciliationReport:
    """Inspect a succeeded payment and report findings without changing its records."""
    # This URL selects different payment reports by header; avoid caching those reports.
    response.headers["Cache-Control"] = "no-store"
    try:
        report = reconcile_stored_payment(store, idempotency_key)
    except UnsupportedReconciliationStateError as error:
        raise _reconciliation_error(
            status.HTTP_409_CONFLICT, "reconciliation_not_supported", str(error)
        ) from error
    except (SQLAlchemyError, ValidationError) as error:
        raise _reconciliation_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "reconciliation_unavailable",
            "The reconciliation report could not be generated.",
        ) from error

    if report is None:
        raise _reconciliation_error(
            status.HTTP_404_NOT_FOUND, "payment_not_found",
            "No internal payment exists for this key.",
        )
    return report


@app.post(
    "/payments/reconciliation/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=ReconciliationRun,
    responses={
        404: {"description": "No internal payment exists for this key."},
        409: {"description": "This version checks succeeded payments only."},
        503: {"description": "The reconciliation run could not be saved."},
    },
)
def save_reconciliation_run(
    response: Response,
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    reader: Annotated[ReconciliationStore, Depends(get_reconciliation_store)],
    history: Annotated[ReconciliationRunStore, Depends(get_reconciliation_run_store)],
) -> ReconciliationRun:
    """Run a check and save a new historical report; each POST creates a distinct run."""
    response.headers["Cache-Control"] = "no-store"
    try:
        run = create_reconciliation_run(reader, history, idempotency_key)
    except UnsupportedReconciliationStateError as error:
        raise _reconciliation_error(
            status.HTTP_409_CONFLICT, "reconciliation_not_supported", str(error)
        ) from error
    except (SQLAlchemyError, ValidationError) as error:
        raise _reconciliation_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "reconciliation_run_unavailable",
            "The reconciliation run could not be saved.",
        ) from error
    if run is None:
        raise _reconciliation_error(
            status.HTTP_404_NOT_FOUND, "payment_not_found",
            "No internal payment exists for this key.",
        )
    return run


@app.get(
    "/payments/reconciliation/runs",
    response_model=list[ReconciliationRun],
    responses={
        404: {"description": "No internal payment exists for this key."},
        503: {"description": "The reconciliation history could not be loaded."},
    },
)
def list_reconciliation_runs(
    response: Response,
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    history: Annotated[ReconciliationRunStore, Depends(get_reconciliation_run_store)],
) -> list[ReconciliationRun]:
    """Read saved reports in observation order without checking the current financial records."""
    response.headers["Cache-Control"] = "no-store"
    try:
        runs = history.list_for_key(idempotency_key)
    except (SQLAlchemyError, ValidationError) as error:
        raise _reconciliation_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "reconciliation_history_unavailable",
            "The reconciliation history could not be loaded.",
        ) from error
    if runs is None:
        raise _reconciliation_error(
            status.HTTP_404_NOT_FOUND, "payment_not_found",
            "No internal payment exists for this key.",
        )
    return runs
