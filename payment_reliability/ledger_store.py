"""Write a validated posting using a transaction owned by the caller."""

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from payment_reliability.ledger import LedgerPosting
from payment_reliability.models import LedgerEntryRecord, LedgerPostingRecord


def save_posting(connection: Connection, posting: LedgerPosting) -> None:
    """Insert the header and both entries; never commit or roll back here."""
    if not connection.in_transaction():
        raise ValueError("Ledger writes require an active transaction owned by the caller.")

    connection.execute(
        insert(LedgerPostingRecord).values(
            id=posting.id,
            payment_id=posting.payment_id,
            amount=posting.amount,
            currency=posting.currency,
        )
    )
    for entry in posting.entries:
        connection.execute(
            insert(LedgerEntryRecord).values(
                posting_id=posting.id,
                direction=entry.direction,
                account=entry.account,
                amount=entry.amount,
            )
        )
