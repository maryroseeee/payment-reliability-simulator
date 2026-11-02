"""Read-only check of Python's connection to PostgreSQL."""

from sqlalchemy import text

from payment_reliability.database import engine


def main():
    with engine.connect() as connection:
        result = connection.execute(text("SELECT current_database()"))
        database_name = result.scalar_one()

    print(f"Connected to PostgreSQL database: {database_name}")


if __name__ == "__main__":
    main()
