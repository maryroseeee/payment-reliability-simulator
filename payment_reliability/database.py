"""SQLAlchemy connection configuration for the simulator."""

import os

from sqlalchemy import create_engine


# The default URL matches our local database settings in compose.yaml.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://simulator:simulator_dev@127.0.0.1:5432/payment_reliability",
)

engine = create_engine(DATABASE_URL)
