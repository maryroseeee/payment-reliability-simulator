# payment-reliability-simulator

A backend learning project using FastAPI, SQLAlchemy, PostgreSQL, and a simulated
payment provider. The current milestone implements one successful payment flow:
reserve an idempotency key, obtain simulator confirmation, save the payment and
processor transaction ID, and replay the saved result on matching retries.

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

The API documentation is available at <http://127.0.0.1:8000/docs>. In another
terminal, submit a simulated $25 USD payment:

```bash
curl -i http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-order-42' \
  -d '{"amount":"25.00","currency":"USD"}'
```

The response is `201` with a payment ID, decimal amount, currency, `succeeded`
status, and `processor_transaction_id`. Repeat the same command to replay the
same `201` response and IDs. A different intended payment needs a new key.

Changing the amount under that key returns `409` with code
`idempotency_conflict`. A matching request whose payment is still processing
returns `409` with code `payment_in_progress`. Missing or blank keys and invalid
amounts return `422`.

With the database running and initialized, run the tests:

```bash
python -m pytest -q
```

This first flow uses an in-process simulator that always confirms success. Its
provider-side history is held in memory; internal payment records and transaction
references persist in PostgreSQL. If processing raises an error, the API returns
`503` and retains `processing`; retries do not call the simulator again. Automatic
recovery of these unresolved records, declines, ledger accounting, and
reconciliation are later milestones.
