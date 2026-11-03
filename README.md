# payment-reliability-simulator

A backend learning project using FastAPI, SQLAlchemy, PostgreSQL, and a simulated
payment provider. The current milestone handles successful and declined payment
attempts: reserve an idempotency key, obtain a confirmed simulator outcome, save
it, and replay that saved outcome on matching retries. Successful payments also
retain the processor transaction ID.
The simulator also demonstrates a completed payment whose response is lost,
including recovery after the API restarts.

For a fresh checkout, use Python 3.12 and start Docker Desktop, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
docker compose up -d --wait db
python -m payment_reliability.init_database
docker compose exec -T db psql -U simulator -d payment_reliability -v ON_ERROR_STOP=1 < migrations/001_add_processor_transaction_id.sql
python -m uvicorn payment_reliability.main:app --reload
```

The SQL migration adds the processor transaction reference to an existing table.
It can also be run after initializing a fresh database. Both operations preserve
existing rows. If Docker is missing from your Mac terminal's command search path,
run `export PATH="$HOME/.docker/bin:$PATH"` in that terminal first.

When upgrading an existing checkout, run
`python -m payment_reliability.init_database` again to create the new
`processor_operations` table. This creates missing tables without altering
existing tables or payment records.

The API documentation is available at <http://127.0.0.1:8000/docs>. In another
terminal, submit a simulated $25 USD payment:

```bash
curl -i http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-order-42' \
  -d '{"amount":"25.00","currency":"USD"}'
```

The default response is `201` with a payment ID, decimal amount, currency,
`succeeded` status, and `processor_transaction_id`. Repeat the same command to
replay the same `201` response and IDs. A different intended payment needs a new
key.

To make new attempts decline, stop the API and restart it with:

```bash
SIMULATE_DECLINES=1 python -m uvicorn payment_reliability.main:app --reload
```

Submit the request using a new key. A recorded decline returns `201` with status
`declined` and a null `processor_transaction_id`. Here `201` means the payment
attempt was recorded; the response's `status` describes its financial outcome.
Matching retries return the same decline, even after restarting the API in its
default successful mode. To intentionally try again, use another new key. The
original declined attempt retains its own payment ID and outcome.

Changing the amount under that key returns `409` with code
`idempotency_conflict`. A matching request whose payment is still processing
returns `409` with code `payment_in_progress`. Missing or blank keys and invalid
amounts return `422`.

To simulate a recorded payment whose first response is lost, restart the API with:

```bash
SIMULATE_LOST_RESPONSE=1 python -m uvicorn payment_reliability.main:app --reload
```

Submit a request with a fresh key. In the default successful mode, the simulator
records one charge, then raises a timeout instead of returning its confirmation.
The API returns `503` with code `outcome_unconfirmed`, while the internal payment
remains `processing` with no processor transaction reference. A matching API retry
returns `409` with code `payment_in_progress` and starts no second charge.

This deterministic mode loses the first response for each new operation; it does
not wait for a real network timeout. The simulator's `get_result(key)` operation
can inspect the recorded outcome without processing another payment. That lookup
reads the provider's own `processor_operations` table in PostgreSQL.

Each provider record stores the idempotency key, internal payment ID as a
reference, amount, currency, confirmed outcome, and processor transaction ID
(null for a decline). A unique key prevents duplicate provider operations across
simulator instances. The provider commits its record before simulating a lost
response; the service saves the internal outcome in a separate transaction.
That separation allows a provider charge to exist while the internal payment is
still `processing`.

Both tables share one local PostgreSQL database to keep the project small. The
provider never derives its outcome from the internal payment status and has no
foreign key to the internal table. Provider records are inserted and read through
`ProcessorStore`; there is no update or delete operation in that component.

`PaymentService.recover_payment(key)` can now read that confirmation and save the
known successful or declined outcome without calling `process()` or creating a
new payment. It preserves the internal payment ID and, for success, saves the
original processor transaction reference. Repeating recovery after completion
returns the saved outcome. If lookup finds no confirmation, the record remains
`processing`; lookup errors also leave it unresolved.

The provider confirmation includes payment ID, amount, and currency alongside its
outcome and transaction ID. Before saving either success or decline, the service
compares these details against the internal payment. A disagreement raises a
consistency error and leaves both records unchanged. This check applies to the
initial provider response as well as recovery. Recovery reports `503` with code
`outcome_unconfirmed` for a mismatch; it does not submit another charge or
overwrite either record to make them agree.

If concurrent recovery calls find the same confirmation, one updates the record.
The other rereads the internal payment and accepts it only when its identity,
amount, currency, outcome, and processor reference match. A disagreement raises
a consistency error rather than overwriting the saved record.

To recover through the API, call `POST /payments/recover` with the same
`Idempotency-Key` header and no request body. You can restart the API between the
lost-response request and recovery; the provider confirmation survives:

```bash
curl -i -X POST http://127.0.0.1:8000/payments/recover \
  -H 'Idempotency-Key: your-lost-response-key'
```

Use the actual key from the earlier failed request. Recovery returns `200` with
the existing payment and its current state. A known confirmation is saved as
`succeeded` or `declined`; a missing provider result returns the payment still
`processing`. No new charge is started. Repeating recovery returns the same
completed outcome. After successful recovery, repeating the original payment
request replays that saved result with `201`.

An unknown internal key returns `404` with code `payment_not_found`. Missing or
blank headers return `422`. Processor lookup, database, or consistency errors
return `503` with code `outcome_unconfirmed`.

In Swagger at <http://127.0.0.1:8000/docs>, expand **POST /payments/recover**,
click **Try it out**, enter the same key in **Idempotency-Key**, and click
**Execute**. Recovery takes no amount or currency body.

For the restart demo, submit a fresh payment while lost-response mode is enabled
and confirm the `503`. Stop the server with Ctrl+C, start it again with
`python -m uvicorn payment_reliability.main:app --reload`, then recover using the
same key. The result should be `200` with `succeeded` and the original provider
transaction ID. Repeating recovery returns the same result.

Old unresolved payments from the earlier in-memory simulator may have no provider
record. They remain unresolved: the upgrade does not invent a confirmation from
internal payment data. Use a fresh key for this demo.

Run the PostgreSQL-backed recovery scenarios with:

```bash
python -m pytest tests/test_recovery.py -q
```

With the database running and initialized, run the tests:

```bash
python -m pytest -q
```

Tests use temporary PostgreSQL schemas for isolated provider histories, removing
only their own schemas afterward. The simulator's `charge_count` counts successful
records in its provider store, including records made by previous instances.

Both internal payment records and independent provider outcomes persist in
PostgreSQL. A confirmed decline is a final outcome. If processing instead raises
an error, the API returns `503` and retains `processing`; retries do not call the
simulator again.

The first ledger component is available in `payment_reliability/ledger.py`.
`build_payment_posting(payment)` prepares two entries: a debit to processor
clearing (simulated funds held) and a credit to merchant payable (funds owed).
The posting carries the payment ID, expected amount, and USD currency. Validation
requires exactly those two accounting sides, equal debit and credit amounts,
and amounts that match the payment. The posting and its entries are frozen
Pydantic models to prevent ordinary field edits after construction.

This component currently prepares data only. The API does not yet save ledger
postings, and building a posting does not confirm a charge or change payment
status. Database storage, a unique posting per payment, and committing the ledger
with the internal success update in one transaction are the next ledger step.
Run its rules independently with `python -m pytest tests/test_ledger.py -q`.
Automatic recovery of unresolved records and reconciliation are later milestones.
