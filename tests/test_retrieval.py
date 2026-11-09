"""Hybrid retrieval against a real Postgres.

The fake provider's vectors are hash-derived, so vector *relevance* is not
testable here -- that is the eval harness's job. What is testable, and what
these cover, is the plumbing: scoping, filtering, fusion arithmetic, and the
query-side purpose.
"""

from pathlib import Path

import psycopg
import pytest

from conftest import DATABASE_URL, FakeEmbeddingProvider
from kb.adapters.code import CodeAdapter
from kb.config import Config, RetrievalConfig
from kb.ingest.pipeline import ingest
from kb.retrieval.hybrid import search

pytestmark = pytest.mark.integration


@pytest.fixture
def corpus(conn: psycopg.Connection, dataset: str, tmp_path: Path) -> Config:
    (tmp_path / "scheduler.py").write_text(
        '"""Scheduler loop."""\n\n\n'
        "def queue_task_instance(dag_id):\n"
        '    """Row level locking prevents duplicate queueing."""\n'
        "    return dag_id\n"
    )
    (tmp_path / "executor.py").write_text(
        '"""Executor."""\n\n\ndef unique_marker_xyzzy(payload):\n    return payload\n'
    )
    config = Config(dataset=dataset, database_url=DATABASE_URL)
    ingest(conn, config, [CodeAdapter(config.chunk)], tmp_path, FakeEmbeddingProvider())
    return config


def _search(conn: psycopg.Connection, config: Config, query: str, **kw: object):
    settings = RetrievalConfig(**kw) if kw else config.retrieval  # type: ignore[arg-type]
    return search(
        conn,
        query,
        dataset=config.dataset,
        provider=FakeEmbeddingProvider(),
        config=settings,
    )


def test_returns_results_ordered_by_fused_score(conn: psycopg.Connection, corpus: Config) -> None:
    results = _search(conn, corpus, "locking")
    assert results
    scores = [r.rrf_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_top_k_is_respected(conn: psycopg.Connection, corpus: Config) -> None:
    results = _search(conn, corpus, "scheduler", top_k=2, candidates_per_arm=20, rrf_k=60)
    assert len(results) <= 2


def test_keyword_arm_finds_a_rare_exact_identifier(
    conn: psycopg.Connection, corpus: Config
) -> None:
    """The reason the keyword arm exists: vector search misses exact tokens."""
    results = _search(conn, corpus, "unique_marker_xyzzy")
    top = results[0]
    assert top.keyword_rank == 1
    assert "unique_marker_xyzzy" in top.content


def test_results_carry_citable_locators(conn: psycopg.Connection, corpus: Config) -> None:
    for chunk in _search(conn, corpus, "scheduler"):
        assert chunk.source_type == "code"
        assert chunk.file_path
        assert chunk.start_line is not None
        assert chunk.citation.startswith("[")


def test_another_dataset_does_not_leak_in(
    conn: psycopg.Connection, corpus: Config, tmp_path: Path
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "secret.py").write_text("def unique_marker_xyzzy_other():\n    pass\n")
    other = Config(dataset=f"{corpus.dataset}-other", database_url=DATABASE_URL)
    ingest(conn, other, [CodeAdapter(other.chunk)], other_root, FakeEmbeddingProvider())
    try:
        results = _search(conn, corpus, "unique_marker_xyzzy_other")
        assert all("secret.py" not in (r.file_path or "") for r in results)
    finally:
        conn.execute("DELETE FROM documents WHERE dataset = %s", (other.dataset,))
        conn.commit()


def test_chunks_from_another_embedding_model_are_excluded(
    conn: psycopg.Connection, corpus: Config
) -> None:
    """Guardrail 7, enforced inside the query.

    Cosine distance between vectors from two models is a meaningless number
    that raises nothing, so the filter is the only thing standing between the
    agent and confident nonsense.
    """
    conn.execute(
        """
        UPDATE chunks SET embedding_model = 'some-other-model'
        WHERE document_id IN (SELECT id FROM documents WHERE dataset = %s)
        """,
        (corpus.dataset,),
    )
    conn.commit()
    assert _search(conn, corpus, "unique_marker_xyzzy") == []


def test_unindexed_documents_are_excluded(conn: psycopg.Connection, corpus: Config) -> None:
    conn.execute("UPDATE documents SET status = 'failed' WHERE dataset = %s", (corpus.dataset,))
    conn.commit()
    assert _search(conn, corpus, "scheduler") == []


def test_the_query_is_embedded_as_a_query_not_a_document(
    conn: psycopg.Connection, corpus: Config
) -> None:
    """On a provider that distinguishes them this is the difference between a
    good search and a degraded one (ADR-0006)."""
    provider = FakeEmbeddingProvider()
    search(
        conn,
        "scheduler",
        dataset=corpus.dataset,
        provider=provider,
        config=corpus.retrieval,
    )
    assert provider.purposes == ["query"]


def test_rrf_arithmetic_is_the_textbook_formula(conn: psycopg.Connection, corpus: Config) -> None:
    """A chunk found by both arms at rank r scores 2/(k+r).

    Pinned because a silently wrong fusion constant would reorder every
    result while looking entirely plausible.
    """
    results = _search(
        conn, corpus, "unique_marker_xyzzy", top_k=10, candidates_per_arm=20, rrf_k=60
    )
    top = results[0]
    expected = 0.0
    if top.keyword_rank is not None:
        expected += 1.0 / (60 + top.keyword_rank)
    if top.vector_rank is not None:
        expected += 1.0 / (60 + top.vector_rank)
    assert top.rrf_score == pytest.approx(expected)


def test_rrf_k_changes_the_weighting(conn: psycopg.Connection, corpus: Config) -> None:
    small = _search(conn, corpus, "scheduler", top_k=5, candidates_per_arm=20, rrf_k=1)
    large = _search(conn, corpus, "scheduler", top_k=5, candidates_per_arm=20, rrf_k=1000)
    assert small[0].rrf_score > large[0].rrf_score


def test_blank_query_is_refused_before_either_arm_runs(
    conn: psycopg.Connection, corpus: Config
) -> None:
    """Vector search has no concept of "no match".

    It always returns its nearest k, so without this guard a whitespace query
    retrieves arbitrary chunks that the agent could present as relevant.
    """
    provider = FakeEmbeddingProvider()
    results = search(
        conn,
        "   ",
        dataset=corpus.dataset,
        provider=provider,
        config=corpus.retrieval,
    )
    assert results == []
    assert provider.calls == 0  # not even embedded


@pytest.mark.parametrize(
    "query",
    [
        "a & b | c ! d",
        "O'Brien's scheduler",
        "<-> !! (((",
        "'; DROP TABLE chunks; --",
        "scheduler_job.py:412",
    ],
)
def test_full_text_metacharacters_are_harmless(
    conn: psycopg.Connection, corpus: Config, query: str
) -> None:
    """tsquery syntax is a parser, so raw user text would be a syntax error at
    best and an injection at worst. The lexemes are built by Postgres."""
    _search(conn, corpus, query)
    row = conn.execute("SELECT count(*) FROM chunks").fetchone()
    assert row is not None and int(row[0]) > 0
