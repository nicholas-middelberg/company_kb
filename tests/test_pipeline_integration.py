"""Pipeline behaviour against a real Postgres.

Uses a fake EmbeddingProvider rather than the real model. That is not a
shortcut: it keeps torch and a 288 MB download out of the test path, and it
exercises the provider seam that ADR-0003 and ADR-0006 exist to create. If
these tests pass with a fake, the seam is real.

Skipped when no database is reachable, so `pytest` stays useful without
`docker compose up`.
"""

import math
from pathlib import Path

import psycopg
import pytest

from conftest import DATABASE_URL, DIMENSION, FakeEmbeddingProvider
from kb.adapters.code import CodeAdapter
from kb.adapters.doc import DocAdapter
from kb.config import Config
from kb.ingest.pipeline import IngestReport, ingest

pytestmark = pytest.mark.integration


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "good.py").write_text('"""Doc."""\n\nLIMIT = 5\n\n\ndef run(x):\n    return x\n')
    (tmp_path / "notes.md").write_text("# Title\n\nbody\n\n## Section\n\nmore\n")
    return tmp_path


def _run(
    conn: psycopg.Connection,
    dataset: str,
    root: Path,
    provider: FakeEmbeddingProvider,
) -> IngestReport:
    config = Config(dataset=dataset, database_url=DATABASE_URL)
    adapters = [CodeAdapter(config.chunk), DocAdapter(config.chunk)]
    return ingest(conn, config, adapters, root, provider)


def _rows(conn: psycopg.Connection, dataset: str) -> dict[str, tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT d.source_uri, d.status, count(c.id)
        FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
        WHERE d.dataset = %s GROUP BY 1, 2
        """,
        (dataset,),
    ).fetchall()
    return {str(r[0]): (str(r[1]), int(r[2])) for r in rows}


def test_fresh_ingest_indexes_everything(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    report = _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert report.discovered == 2
    assert report.indexed == 2
    assert report.failed == 0
    assert report.chunks_written > 0
    assert all(status == "indexed" for status, _ in _rows(conn, dataset).values())


def test_second_run_makes_zero_embedding_calls(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Milestone 1 exit criterion 1."""
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    second = FakeEmbeddingProvider()
    report = _run(conn, dataset, corpus, second)
    assert report.skipped_unchanged == 2
    assert report.indexed == 0
    assert report.embedding_calls == 0
    assert second.calls == 0


def test_one_unparseable_file_does_not_abort_the_run(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    (corpus / "broken.py").write_text("def broken(:\n  nope\n")
    report = _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert report.failed == 1
    assert report.indexed == 2
    assert "broken.py" in report.failures[0][0]


def test_a_failure_is_recorded_in_the_database(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Reporting it in the run output is not enough: `kb status` must show it,
    or a failed document is invisible the moment the terminal scrolls."""
    (corpus / "broken.py").write_text("def broken(:\n  nope\n")
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert _rows(conn, dataset)["broken.py"] == ("failed", 0)


def test_no_document_is_left_in_processing(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    (corpus / "broken.py").write_text("def broken(:\n  nope\n")
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert "processing" not in {s for s, _ in _rows(conn, dataset).values()}


def test_newly_broken_document_loses_its_stale_chunks(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Otherwise the agent cites text that is no longer in the source, with a
    citation that looks perfectly valid."""
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert _rows(conn, dataset)["good.py"][1] > 0

    (corpus / "good.py").write_text("def good(:\n  broken now\n")
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert _rows(conn, dataset)["good.py"] == ("failed", 0)


def test_a_failed_document_is_retried_not_skipped(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Its hash is recorded, so hash-only skip logic would strand it forever."""
    (corpus / "broken.py").write_text("def broken(:\n  nope\n")
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    report = _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert report.failed == 1
    assert report.skipped_unchanged == 2


def test_repairing_a_document_restores_it(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    (corpus / "good.py").write_text("def good(:\n  broken\n")
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    assert _rows(conn, dataset)["good.py"] == ("failed", 0)

    (corpus / "good.py").write_text('"""Fixed."""\n\n\ndef good(x):\n    return x\n')
    _run(conn, dataset, corpus, FakeEmbeddingProvider())
    status, chunks = _rows(conn, dataset)["good.py"]
    assert status == "indexed"
    assert chunks > 0


def test_every_chunk_records_its_embedding_model(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Guardrail 7 depends on this column being populated, always."""
    provider = FakeEmbeddingProvider()
    _run(conn, dataset, corpus, provider)
    rows = conn.execute(
        """
        SELECT DISTINCT c.embedding_model FROM chunks c
        JOIN documents d ON d.id = c.document_id WHERE d.dataset = %s
        """,
        (dataset,),
    ).fetchall()
    assert [str(r[0]) for r in rows] == [provider.model_name]


def test_dimension_disagreement_is_refused_before_any_work(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """The column would reject it anyway, but only after a whole run is spent."""
    with pytest.raises(ValueError, match="dimensions"):
        _run(conn, dataset, corpus, FakeEmbeddingProvider(dimension=384))
    assert _rows(conn, dataset) == {}


def test_embedding_is_batched_within_a_document(
    conn: psycopg.Connection, dataset: str, tmp_path: Path
) -> None:
    """Call count must reflect batches, not chunks."""
    root = tmp_path / "one"
    root.mkdir()
    (root / "many.py").write_text("\n\n".join(f"def f{i}():\n    return {i}" for i in range(70)))
    config = Config(dataset=dataset, database_url=DATABASE_URL)
    provider = FakeEmbeddingProvider()
    report = ingest(conn, config, [CodeAdapter(config.chunk)], root, provider)

    expected = -(-provider.texts_seen // config.embedding.batch_size)
    assert report.embedding_calls == expected
    assert provider.calls == expected
    assert provider.texts_seen > config.embedding.batch_size  # really batched


def test_batching_does_not_span_documents(
    conn: psycopg.Connection, dataset: str, tmp_path: Path
) -> None:
    """Documented behaviour, not an accident.

    Each document is embedded, written and committed on its own so that one
    failure cannot discard another document's work. The cost is that a corpus
    of many small documents makes one call per document rather than filling
    batches across them -- negligible for a local model, but worth knowing
    before pointing a metered provider at a few hundred documents, as the
    Voyage ceiling run (ADR-0006) will.
    """
    root = tmp_path / "several"
    root.mkdir()
    for i in range(3):
        (root / f"tiny{i}.py").write_text(f"X = {i}\n")

    config = Config(dataset=dataset, database_url=DATABASE_URL)
    provider = FakeEmbeddingProvider()
    report = ingest(conn, config, [CodeAdapter(config.chunk)], root, provider)

    assert report.indexed == 3
    assert provider.texts_seen == 3
    # Three tiny documents would fit one batch, yet take three calls.
    assert report.embedding_calls == 3


def test_fake_provider_matches_the_real_contract() -> None:
    """Guards the fake itself.

    If the fake drifts from the real provider's contract, every test using it
    is testing a system that does not exist.
    """
    vectors = FakeEmbeddingProvider().embed(["a", "b"])
    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == DIMENSION
        norm = math.sqrt(sum(v * v for v in vector))
        assert math.isclose(norm, 1.0, abs_tol=1e-6)


def test_ingestion_embeds_as_documents_not_queries(
    conn: psycopg.Connection, dataset: str, corpus: Path
) -> None:
    """Indexing must use the document prompt.

    On a provider that distinguishes them, embedding a corpus as queries
    degrades retrieval for every search against it (ADR-0006).
    """
    provider = FakeEmbeddingProvider()
    _run(conn, dataset, corpus, provider)
    assert set(provider.purposes) == {"document"}
