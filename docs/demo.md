# Payment reliability demo

This demo follows a simulated $25 payment whose response is lost. You will retry
the request, restart the API, recover the result, and save two reconciliation
reports. Use the local PostgreSQL setup from the [README](../README.md).

Use two terminals, both at the repository root. Complete the README's database
setup first. Keep PostgreSQL running during the demo. If an API is
already running on port 8000, stop that API with Ctrl+C before starting this one.

**1. Start the API in lost-response mode in terminal A.**

```bash
source .venv/bin/activate
SIMULATE_LOST_RESPONSE=1 SIMULATE_DECLINES=0 python -m uvicorn payment_reliability.main:app --reload
```

For each new key, the provider saves its result and then raises a timeout.

**2. Generate a fresh key in terminal B and submit the payment.**

```bash
source .venv/bin/activate
export DEMO_PAYMENT_KEY="demo-$(python -c 'from uuid import uuid4; print(uuid4().hex)')"
printf '%s\n' "$DEMO_PAYMENT_KEY"

curl -i http://127.0.0.1:8000/payments \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"amount":"25.00","currency":"USD"}'
```

Expect `503` with code `outcome_unconfirmed`. The provider has one recorded
successful charge. The internal payment remains `processing` and has no ledger
posting yet.

**3. Repeat the exact request using the same key.**

```bash
curl -i http://127.0.0.1:8000/payments \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"amount":"25.00","currency":"USD"}'
```

Expect `409` with code `payment_in_progress`. The service does not call the
provider again for an unresolved payment. Keep this key for recovery.

**4. Restart only the API.**

In terminal A, press Ctrl+C, then run:

```bash
SIMULATE_LOST_RESPONSE=0 SIMULATE_DECLINES=0 python -m uvicorn payment_reliability.main:app --reload
```

The provider confirmation survives because it was committed in PostgreSQL.
Terminal B still has `DEMO_PAYMENT_KEY`; use it for the remaining commands.

**5. Recover the payment from terminal B.**

```bash
curl -i -X POST http://127.0.0.1:8000/payments/recover \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY"
```

Expect `200`, `status: "succeeded"`, and a processor transaction ID. Recovery
reads the provider's saved result. The success update and both ledger entries
commit together. Repeating recovery returns the same result.

**6. Retry the completed payment, then change the amount.**

```bash
curl -i http://127.0.0.1:8000/payments \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"amount":"25.00","currency":"USD"}'

curl -i http://127.0.0.1:8000/payments \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"amount":"40.00","currency":"USD"}'
```

The matching request returns `201` with the original payment and processor IDs.
The changed amount returns `409` with code `idempotency_conflict`. Neither
request adds another charge or ledger posting.

**7. Check the payment and save two reports.**

```bash
curl -i http://127.0.0.1:8000/payments/reconciliation \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY"

curl -i -X POST http://127.0.0.1:8000/payments/reconciliation/runs \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY"

curl -i -X POST http://127.0.0.1:8000/payments/reconciliation/runs \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY"

curl -i http://127.0.0.1:8000/payments/reconciliation/runs \
  -H "Idempotency-Key: $DEMO_PAYMENT_KEY"
```

The check returns `200`, `status: "consistent"`, and empty findings. Each POST
returns `201` with a different run ID for the same payment. GET history returns
both saved reports. GET requests do not save reports, and none of these requests
submits a charge or repairs financial records.

You can perform the same requests in [Swagger](http://127.0.0.1:8000/docs): click
**Try it out**, paste the printed key into **Idempotency-Key**, and click
**Execute**. Only the payment submission takes a JSON body.

**8. Inspect the committed records.**

Run this from terminal B. The psql variable syntax quotes the key as a SQL value:

```bash
docker compose exec -T db psql -U simulator -d payment_reliability \
  -v demo_key="$DEMO_PAYMENT_KEY" <<'SQL'
SELECT p.id AS payment_id, p.status,
       (SELECT count(*) FROM processor_operations AS o
        WHERE o.idempotency_key = p.idempotency_key
          AND o.status = 'succeeded') AS provider_charges,
       (SELECT count(*) FROM ledger_postings AS l
        WHERE l.payment_id = p.id) AS postings,
       (SELECT count(*) FROM ledger_entries AS e
        JOIN ledger_postings AS l ON l.id = e.posting_id
        WHERE l.payment_id = p.id) AS entries,
       (SELECT count(*) FROM reconciliation_runs AS r
        WHERE r.payment_id = p.id) AS saved_runs
FROM payments AS p
WHERE p.idempotency_key = :'demo_key';
SQL
```

Run this query before or after recovery to compare the records:

| Point in demo | Internal status | Provider charges | Postings | Entries | Saved runs |
|---|---|---:|---:|---:|---:|
| After the lost response and unresolved retry | `processing` | 1 | 0 | 0 | 0 |
| After recovery and exactly two saved checks | `succeeded` | 1 | 1 | 2 | 2 |

Both reports describe the same payment. There should still be one charge.
Generate a new key when repeating the demo; an existing key keeps its original
payment result.

This test shows a missing ledger posting followed by a correction to the test
data and a second check. It verifies that the first discrepancy report is still
available after the second report is saved:

```bash
python -m pytest tests/test_reconciliation_runs.py::test_two_runs_preserve_the_original_discrepancy_after_the_ledger_is_corrected -v
```

The correction happens only in the test's temporary schema. The application
does not automatically repair discrepancies. To check rollback and recovery
after a failed ledger write, run:

```bash
python -m pytest tests/test_ledger_storage.py -q
```

See the [engineering notes](engineering-notes.md) for the transaction boundaries,
recovery logic, and decline scenarios.
