"""Create missing database tables for local development."""

from payment_reliability.database import engine
from payment_reliability.models import Base


def main():
    Base.metadata.create_all(engine)
    print("Database tables are ready.")


if __name__ == "__main__":
    main()
