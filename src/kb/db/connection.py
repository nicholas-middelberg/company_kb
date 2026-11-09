"""Database connections, with the pgvector type registered (ADR-0030)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector


@contextmanager
def connect(database_url: str) -> Generator[psycopg.Connection]:
    """Open a connection with the vector type adapted.

    Without register_vector, every embedding is hand-formatted into a string
    literal on the way in and parsed back out by hand -- which is exactly
    where a dimension or float-precision bug hides without raising.
    """
    with psycopg.connect(database_url) as conn:
        register_vector(conn)
        yield conn
