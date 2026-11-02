ALTER TABLE payments
ADD COLUMN IF NOT EXISTS processor_transaction_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_processor_transaction_id
ON payments (processor_transaction_id);
