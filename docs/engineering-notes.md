These development notes were preserved from the original README. Start with the [current README](../README.md) for setup and the [demo guide](demo.md) for a walkthrough.

# payment-reliability-simulator

A backend learning project using FastAPI, SQLAlchemy, PostgreSQL, and a simulated
payment provider. The current milestone handles successful and declined payment
attempts: reserve an idempotency key, obtain a confirmed simulator outcome, save
it, and replay that saved outcome on matching retries. Successful payments also
retain the processor transaction ID and save a balanced ledger posting atomically
with the internal success update.
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
`processor_operations`, `ledger_postings`, `ledger_entries`, and
`reconciliation_runs` tables. This
creates missing tables without altering existing tables or payment records.

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

Building a posting still prepares data only. Saving a confirmed success now goes
through `PaymentStore.mark_succeeded()`, which uses one transaction to update
the payment and call `ledger_store.save_posting(connection, posting)`. That helper
receives the active connection and does not commit independently. The success
update becomes visible to other connections only when the header and both
entries have been saved and the transaction commits.

If a ledger write fails, the entire internal transaction rolls back: the payment
remains `processing`, its processor reference remains unset, and no partial
ledger data persists. The independent provider confirmation stays recorded.
The API returns `503` with code `outcome_unconfirmed`. After the storage problem
is resolved, `POST /payments/recover` can save the original confirmation and its
ledger without calling the processor's `process()` again.

`ledger_postings` has a unique payment ID and references `payments`.
`ledger_entries` references its posting and permits one entry per accounting
side. Database checks reject invalid amounts and account/direction combinations;
the validated posting and shared transaction enforce the complete balanced pair
on the application's write path. The ledger helper exposes insert operations
only. Matching retries and concurrent recoveries preserve the same posting;
declined or unconfirmed outcomes create none.

Existing successful payments created before ledger storage was introduced are
preserved without backfilling accounting entries. Replaying those historical
payments does not create a posting. Use a fresh key to demonstrate the new flow.

To inspect a fresh payment's ledger, first open a SQL prompt:

```bash
docker compose exec db psql -U simulator -d payment_reliability
```

Run the following query, replacing `your-payment-key` with the actual key:

```sql
SELECT p.id AS payment_id, p.status, l.id AS posting_id,
       e.account, e.direction, e.amount, l.currency
FROM payments AS p
LEFT JOIN ledger_postings AS l ON l.payment_id = p.id
LEFT JOIN ledger_entries AS e ON e.posting_id = l.id
WHERE p.idempotency_key = 'your-payment-key'
ORDER BY e.direction DESC;
```

A newly successful payment has two rows sharing one posting ID: a processor
clearing debit and merchant payable credit for the payment amount. Repeating the
payment or its recovery leaves those rows unchanged. Enter `\q` to exit psql.

Run the accounting and storage tests with:

```bash
python -m pytest tests/test_ledger.py tests/test_ledger_storage.py -q
```

The storage tests inject failures after the header, first entry, and second entry
are actually written, then verify rollback and recovery without another charge.

The first reconciliation comparison is in `payment_reliability/reconciliation.py`.
`reconcile_successful_payment(payment=..., provider=..., ledger=...)` accepts
records supplied by its caller and returns a report containing the payment ID,
posting ID when present, `consistent` or `discrepancy` status, and findings with
codes and explanations. A confirmed successful payment with no supplied posting
produces a `missing_ledger_posting` finding. Missing provider confirmation,
disagreeing provider details, incorrect ledger references, and inconsistent
ledger amounts or accounting sides also produce findings. This function checks
payments already marked `succeeded`; other states currently raise `ValueError`.

`LedgerSnapshot` and `LedgerEntrySnapshot` describe the records being inspected.
Unlike the strict posting model used to prepare writes, these read models permit
incomplete groups and inconsistent values so the checker can explain them. For
example, a posting containing only one entry can be inspected and reported.
The checker validates the payment-to-posting and entry-to-posting references,
requires the two expected accounting sides, and compares each entry's amount
with the payment amount. Equal debit and credit amounts alone do not establish
consistency.

The comparison component performs no database queries or repairs. Run its tests
without a running database:

```bash
python -m pytest tests/test_reconciliation.py -q
```

`ReconciliationStore.load(key)` in `payment_reliability/reconciliation_store.py`
now reads the actual payment, provider confirmation, posting, and entries. It
looks up the payment and provider by idempotency key, the posting by payment ID,
and the entries by posting ID. The returned `ReconciliationSnapshot` groups the
three sources for the comparison component. An unknown internal key returns
`None`, even if a provider-only record exists; this helper never creates an
internal payment or scans for orphan provider records.

The helper opens one transaction and sets
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` before its first
query. All queries then see the same committed database snapshot established by
the first query. A recovery that commits between reads becomes visible on the
next helper call. PostgreSQL also rejects writes in the helper's transaction.
These settings affect only that transaction; ordinary payment processing keeps
its existing transaction settings. See the PostgreSQL documentation for
[repeatable read](https://www.postgresql.org/docs/18/transaction-iso.html#XACT-REPEATABLE-READ)
and [transaction modes](https://www.postgresql.org/docs/18/sql-set-transaction.html).
This shared snapshot is possible because the simulated provider and internal
records live in the same PostgreSQL database.

`reconcile_stored_payment(store, key)` connects the loader to the comparison
function. It returns a report, or `None` for a missing internal payment. The
comparison still requires an internal `succeeded` payment; processing or declined
payments raise `UnsupportedReconciliationStateError`, a `ValueError` subclass.
Loading those states alone is supported
and does not trigger recovery. Reports are returned as data and do not repair
records or persist an audit report.

To inspect a successful payment from the terminal, run this from the project
root with the virtual environment active. Replace the key with one you used for
a payment that is now `succeeded`:

```bash
python - <<'PY'
from payment_reliability.database import engine
from payment_reliability.reconciliation_store import ReconciliationStore, reconcile_stored_payment

report = reconcile_stored_payment(ReconciliationStore(engine), "your-successful-payment-key")
print("No internal payment found." if report is None else report.model_dump_json(indent=2))
PY
```

Run the storage integration tests with PostgreSQL running:

```bash
python -m pytest tests/test_reconciliation_store.py -q
```

These tests use temporary schemas containing the application tables and remove only
their own schemas afterward. They cover recovery committing between reads,
database-enforced read-only loading, missing records, and reporting damaged
ledger entries without repairing them.

The same report is now available through `GET /payments/reconciliation`. Supply
the existing `Idempotency-Key` header and no request body:

```bash
curl -i http://127.0.0.1:8000/payments/reconciliation \
  -H 'Idempotency-Key: your-successful-payment-key'
```

In Swagger at <http://127.0.0.1:8000/docs>:

1. Create a fresh payment with **POST /payments**, using `25.00`, `USD`, and a new
   key. Check that its status is `succeeded`; if the response was lost, use
   **POST /payments/recover** first.
2. Expand **GET /payments/reconciliation** and click **Try it out**.
3. Enter that same key in **Idempotency-Key** and click **Execute**. No body is needed.
4. A matching payment, provider confirmation, and ledger return `200` with
   `status: "consistent"`, their payment and posting IDs, and an empty findings list.
   Repeat the check to inspect the same records without creating another charge.

The HTTP status describes whether the check could run. A `200` report may contain
`status: "discrepancy"` and findings such as `missing_ledger_posting`. That means
the check completed and found a problem; it does not repair the records. Older
successful payments without provider history or ledger entries can produce such
findings, so use a fresh key for the consistent-report demonstration.

| HTTP status | Meaning |
|---|---|
| `200` | The report was generated; inspect its status and findings. |
| `404` | No internal payment exists for the supplied key (`payment_not_found`). |
| `409` | The payment is processing or declined; this first version checks succeeded payments only (`reconciliation_not_supported`). |
| `422` | The idempotency header is missing or blank. |
| `503` | A database or stored-data validation error prevented the report (`reconciliation_unavailable`). |

Report responses use `Cache-Control: no-store`. Checking a processing payment
does not invoke recovery, and checking a declined payment does not start another
attempt. The route loads one read-only snapshot and passes it to the existing
comparison component. Run its API tests with PostgreSQL running:

```bash
python -m pytest tests/test_reconciliation_api.py -q
```

Reconciliation reports can now be saved as separate runs. Each run contains its
own `id`, the `payment_id`, a timezone-aware `checked_at`, and the original
`report`. The observation time comes from `statement_timestamp()` in the
snapshot's first source query, before any later changes become visible to that
snapshot. It is not the time at which the audit insert eventually completes.
See PostgreSQL's [current date/time functions](https://www.postgresql.org/docs/18/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT).

`create_reconciliation_run(reader, history, key)` loads one read-only snapshot,
compares its records, and then calls `ReconciliationRunStore.append()` in a
separate write transaction. That transaction inserts only the audit record.
It does not update the payment, provider, or ledger. The original report is
stored as a JSONB document and later decoded from that stored document; history
does not recompute old reports from live financial data. A database constraint
checks that the report's payment ID matches the run's payment ID.

A payment can have many runs. There is no previous-run pointer: reports are
grouped by payment ID and returned in `checked_at`, then run-ID order. The
application inserts and reads audit records and exposes no operation to edit or
delete them. A later consistent report preserves any earlier discrepancy report.
If saving fails before commit, the new audit row rolls back and earlier history
remains available.

| Action | Endpoint | Result |
|---|---|---|
| Inspect current records | `GET /payments/reconciliation` | `200` report; saves no run. |
| Run a check and save it | `POST /payments/reconciliation/runs` | `201` with a new run and its report. |
| Read saved history | `GET /payments/reconciliation/runs` | `200` list of saved runs; performs no comparison. |

All three actions use the same `Idempotency-Key` header to identify the existing
payment and take no request body. On the run endpoints, that key selects the
payment; it does not deduplicate audit requests. Each POST is a new check and
creates a distinct run, even when the report is unchanged. Repeating a POST
after a lost response can therefore save another audit row, while financial
records and charges are unaffected. Use GET history to inspect what was saved.

To try saved history in Swagger:

1. Use a key for a payment whose status is `succeeded`.
2. Expand **POST /payments/reconciliation/runs**, click **Try it out**, enter that
   key, and click **Execute**. The `201` response includes a run ID, observation
   time, and nested report. A discrepancy report can also be saved with `201`.
3. Repeat the POST and confirm it returns another run ID for the same payment.
4. Expand **GET /payments/reconciliation/runs** and execute it with that key.
   Both saved runs should appear. Repeating this GET adds no history rows.

Equivalent terminal requests:

```bash
curl -i -X POST http://127.0.0.1:8000/payments/reconciliation/runs \
  -H 'Idempotency-Key: your-successful-payment-key'

curl -i http://127.0.0.1:8000/payments/reconciliation/runs \
  -H 'Idempotency-Key: your-successful-payment-key'
```

Unknown internal keys return `404`. A known payment without any saved runs
returns `200` with `[]` from history. Saving still requires `succeeded`, so a
processing or declined payment returns `409` and saves nothing. Its existing
history can be read regardless of its current status. Missing or blank headers
return `422`. Save failures return `503` with code
`reconciliation_run_unavailable`; history read failures return `503` with code
`reconciliation_history_unavailable`, rather than pretending the history is empty.

Run the history and API tests with PostgreSQL running:

```bash
python -m pytest tests/test_reconciliation_runs.py tests/test_reconciliation_api.py -q
```

These tests preserve a missing-ledger finding across a later correction and
consistent check, verify observation time survives delayed saving, append
concurrent runs, and inject a failure after the real audit INSERT to verify
rollback. Checks for processing or declined payments and automatic recovery are
later steps. The next portfolio milestone is a repeatable demonstration and
setup polish.
