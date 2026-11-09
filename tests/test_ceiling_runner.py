"""The ceiling runner, against real Postgres with a stubbed hosted provider.

No API calls: the point of the provider seam is that a measurement path can be
exercised without paying for it.
"""

import re
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from conftest import DATABASE_URL, FakeEmbeddingProvider, StubCeilingProvider
from kb.adapters.code import CodeAdapter
from kb.ceiling import runner
from kb.config import CeilingConfig, Config
from kb.db import queries
from kb.ingest.pipeline import ingest

pytestmark = pytest.mark.integration


@pytest.fixture
def ingested(conn: psycopg.Connection, dataset: str, tmp_path: Path) -> Config:
    """A small indexed corpus for the ceiling run to work over."""
    for i in range(5):
        (tmp_path / f"mod{i}.py").write_text(
            f'"""Module {i}."""\n\n\ndef handler_{i}(x):\n    return x + {i}\n'
        )
    config = Config(dataset=dataset, database_url=DATABASE_URL)
    ingest(conn, config, [CodeAdapter(config.chunk)], tmp_path, FakeEmbeddingProvider())
    return config


def _ceiling_rows(conn: psycopg.Connection, dataset: str) -> int:
    row = conn.execute(
        """
        SELECT count(*) FROM ceiling_vectors cv
        JOIN chunks c ON c.id = cv.chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE d.dataset = %s
        """,
        (dataset,),
    ).fetchone()
    return int(row[0]) if row else 0


def test_plan_spends_nothing(conn: psycopg.Connection, ingested: Config) -> None:
    """Estimating must not call the provider. That is the whole point."""
    provider = StubCeilingProvider()
    estimate = runner.plan(conn, ingested.dataset, provider, ingested.ceiling)
    assert provider.calls == 0
    assert estimate.pending_chunks > 0
    assert estimate.already_done == 0
    assert estimate.estimated_tokens > 0


def test_run_embeds_every_chunk(conn: psycopg.Connection, ingested: Config) -> None:
    provider = StubCeilingProvider()
    report = runner.run(conn, ingested.dataset, provider, ingested.ceiling)
    assert report.embedded == _ceiling_rows(conn, ingested.dataset)
    assert report.embedded > 0
    assert report.total_tokens == 11 * report.embedded


def test_run_is_resumable_and_does_not_re_pay(conn: psycopg.Connection, ingested: Config) -> None:
    """It bills per token, so a second run must cost nothing."""
    runner.run(conn, ingested.dataset, StubCeilingProvider(), ingested.ceiling)
    second = StubCeilingProvider()
    report = runner.run(conn, ingested.dataset, second, ingested.ceiling)
    assert report.embedded == 0
    assert second.calls == 0
    assert report.total_tokens == 0


def test_partial_run_resumes_where_it_stopped(conn: psycopg.Connection, ingested: Config) -> None:
    first = runner.run(conn, ingested.dataset, StubCeilingProvider(), ingested.ceiling, limit=2)
    assert first.embedded == 2

    estimate = runner.plan(conn, ingested.dataset, StubCeilingProvider(), ingested.ceiling)
    assert estimate.already_done == 2

    rest = runner.run(conn, ingested.dataset, StubCeilingProvider(), ingested.ceiling)
    assert rest.embedded == estimate.pending_chunks
    assert _ceiling_rows(conn, ingested.dataset) == estimate.total_chunks


def test_corpus_is_embedded_as_documents(conn: psycopg.Connection, ingested: Config) -> None:
    """Embedding the corpus as queries would invert the ceiling (ADR-0006)."""
    provider = StubCeilingProvider()
    runner.run(conn, ingested.dataset, provider, ingested.ceiling)
    assert set(provider.purposes) == {"document"}


def test_batches_span_documents(conn: psycopg.Connection, ingested: Config) -> None:
    """Unlike ingestion, which batches per document to protect its transaction.

    Here there is no per-document transaction to protect, so batches fill
    across the corpus -- which is the one place batching actually pays.
    """
    provider = StubCeilingProvider()
    config = Config(
        dataset=ingested.dataset,
        database_url=DATABASE_URL,
        ceiling=CeilingConfig(batch_size=1000),
    )
    report = runner.run(conn, config.dataset, provider, config.ceiling)
    assert report.embedded > 1
    assert report.requests == 1  # five documents, one request


def test_wrong_dimension_is_refused_against_the_column(
    conn: psycopg.Connection, ingested: Config
) -> None:
    """Checked against the catalogue, not a constant, so the column is the
    single authority on its own width."""
    with pytest.raises(ValueError, match=re.escape("ceiling_vectors.embedding is vector")):
        runner.run(
            conn,
            ingested.dataset,
            StubCeilingProvider(dimension=768),
            ingested.ceiling,
        )
    assert _ceiling_rows(conn, ingested.dataset) == 0


def test_replacing_a_chunk_drops_its_ceiling_vector(
    conn: psycopg.Connection, ingested: Config, tmp_path: Path
) -> None:
    """ON DELETE CASCADE matters: a stale ceiling vector would describe text
    that no longer exists, and the comparison would silently be against it."""
    runner.run(conn, ingested.dataset, StubCeilingProvider(), ingested.ceiling)
    before = _ceiling_rows(conn, ingested.dataset)
    assert before > 0

    (tmp_path / "mod0.py").write_text('"""Changed."""\n\n\ndef other(y):\n    return y\n')
    ingest(
        conn,
        ingested,
        [CodeAdapter(ingested.chunk)],
        tmp_path,
        FakeEmbeddingProvider(),
    )
    assert _ceiling_rows(conn, ingested.dataset) < before


def test_coverage_reports_progress(conn: psycopg.Connection, ingested: Config) -> None:
    provider = StubCeilingProvider()
    done, total = queries.ceiling_coverage(conn, ingested.dataset, provider.model_name)
    assert (done, total) == (0, total)
    runner.run(conn, ingested.dataset, provider, ingested.ceiling)
    done, total = queries.ceiling_coverage(conn, ingested.dataset, provider.model_name)
    assert done == total > 0


def test_only_indexed_documents_are_embedded(
    conn: psycopg.Connection, ingested: Config, tmp_path: Path
) -> None:
    """A failed document's text must not reach a metered API."""
    (tmp_path / "broken.py").write_text("def broken(:\n  nope\n")
    ingest(
        conn,
        ingested,
        [CodeAdapter(ingested.chunk)],
        tmp_path,
        FakeEmbeddingProvider(),
    )
    pending = queries.chunks_awaiting_ceiling(conn, ingested.dataset, "stub-voyage-model")
    ids: list[UUID] = [chunk_id for chunk_id, _ in pending]
    rows = conn.execute(
        """
        SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.id = ANY(%s) AND d.status <> 'indexed'
        """,
        (ids,),
    ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 0
