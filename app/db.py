"""Short lived PostgreSQL connections; each `with` block is one transaction."""

import os

import psycopg
from psycopg.rows import dict_row


def connection():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
