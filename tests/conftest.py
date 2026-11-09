"""Shared fixtures and fakes.

The fakes live here because two suites need them and because a fake that
drifts from the real provider's contract makes every test using it a test of a
system that does not exist. One definition, one guard (see
test_pipeline_integration.test_fake_provider_matches_the_real_contract).
"""

import hashlib
import math
import os
from collections.abc import Iterator, Sequence
from uuid import uuid4

import psycopg
import pytest

from kb.db.connection import connect
from kb.providers.base import EmbeddingPurpose

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://kb:kb_local_dev@localhost:5432/company_kb"
)
DIMENSION = 768
"""The serving column's width, from the configured model (ADR-0022)."""

CEILING_DIMENSION = 1024
"""The ceiling table's width, which deliberately differs (ADR-0006)."""


class FakeEmbeddingProvider:
    """Deterministic vectors derived from the text, and a call counter.

    Mirrors the real provider's contract deliberately, including unit-norm
    output. An earlier version returned raw bytes scaled to [0, 1], giving
    vectors with norm ~13.9 while the real provider emits ~1.0 -- which would
    let a test pass while depending on a property production does not have.
    """

    def __init__(self, dimension: int = DIMENSION) -> None:
        self._dimension = dimension
        self.calls = 0
        self.texts_seen = 0
        self.purposes: list[str] = []

    @property
    def model_name(self) -> str:
        return "fake-test-model"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> list[list[float]]:
        self.calls += 1
        self.texts_seen += len(texts)
        self.purposes.append(purpose)
        vectors: list[list[float]] = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            raw = [seed[i % len(seed)] / 255.0 for i in range(self._dimension)]
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            vectors.append([v / norm for v in raw])
        return vectors


class StubCeilingProvider:
    """A hosted provider at the ceiling table's width, counting tokens.

    Tokens, not requests: that is what a hosted provider bills for.
    """

    def __init__(self, dimension: int = CEILING_DIMENSION) -> None:
        self._dimension = dimension
        self.calls = 0
        self.purposes: list[str] = []
        self.total_tokens = 0

    @property
    def model_name(self) -> str:
        return "stub-voyage-model"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> list[list[float]]:
        self.calls += 1
        self.purposes.append(purpose)
        self.total_tokens += 11 * len(texts)
        unit = 1.0 / math.sqrt(self._dimension)
        return [[unit] * self._dimension for _ in texts]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    try:
        with connect(DATABASE_URL) as connection:
            yield connection
    except psycopg.OperationalError as error:
        pytest.skip(f"no database reachable: {error}")


@pytest.fixture
def dataset(conn: psycopg.Connection) -> Iterator[str]:
    """A unique dataset per test, torn down afterwards."""
    name = f"test-{uuid4().hex[:12]}"
    yield name
    conn.execute("DELETE FROM documents WHERE dataset = %s", (name,))
    conn.commit()
