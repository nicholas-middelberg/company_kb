"""Runs the hosted embedding model over the serving corpus (ADR-0006).

This is a measurement, not a serving path. It exists so a weak `recall@k` can
be attributed: without a known upper bound, a poor score is equally explained
by the local embedding model or by the chunk parameters (ADR-0019).

Two properties follow from it costing money per token rather than per request:

- **Resumable.** Only chunks without a vector for this model are fetched, so a
  run that dies partway through does not re-pay for what already succeeded.
- **Estimable.** `plan()` reports what a run would cost without calling the
  API, because "roughly how much is this?" should not be answered by spending.

Batching here is free of the constraint that keeps ingestion batching
per-document: there is no per-document transaction to protect, so batches fill
across the whole corpus. The place where batching actually pays is the place
where nothing prevents it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg

from kb.config import CeilingConfig
from kb.db import queries
from kb.providers.base import EmbeddingProvider

CHARS_PER_TOKEN = 4.0
"""Rough English/code average, used only for pre-flight estimates.

Deliberately approximate and never used for billing: the provider reports the
real token count after the fact. An estimate that pretends to be exact is
worse than one that announces itself.
"""


@dataclass
class CeilingPlan:
    """What a run would do, computed without calling the API."""

    pending_chunks: int
    already_done: int
    total_chunks: int
    estimated_tokens: int
    estimated_requests: int

    def estimated_cost(self, price_per_mtok: float | None) -> float | None:
        if price_per_mtok is None:
            return None
        return self.estimated_tokens / 1_000_000 * price_per_mtok

    def render(self, price_per_mtok: float | None = None) -> str:
        lines = [
            f"  chunks total       {self.total_chunks}",
            f"  already embedded   {self.already_done}",
            f"  pending            {self.pending_chunks}",
            f"  estimated requests {self.estimated_requests}",
            f"  estimated tokens   ~{self.estimated_tokens:,} (approximate)",
        ]
        cost = self.estimated_cost(price_per_mtok)
        if cost is not None:
            # 4dp, not 2: rendering a real cost as $0.00 reads as free, which
            # is the single most misleading thing this line could say.
            lines.append(f"  estimated cost     ~${cost:,.4f}")
        else:
            lines.append(
                "  estimated cost     unknown - pass --price-per-mtok "
                "(check current pricing; it changes)"
            )
        return "\n".join(line for line in lines if line.strip())


@dataclass
class CeilingReport:
    """What a run actually did. Tokens, not requests, are the cost."""

    embedded: int = 0
    requests: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0.0

    def actual_cost(self, price_per_mtok: float | None) -> float | None:
        if price_per_mtok is None:
            return None
        return self.total_tokens / 1_000_000 * price_per_mtok

    def render(self, price_per_mtok: float | None = None) -> str:
        lines = [
            f"  embedded      {self.embedded}",
            f"  requests      {self.requests}",
            f"  billed tokens {self.total_tokens:,}",
            f"  elapsed       {self.elapsed_seconds:.1f}s",
        ]
        cost = self.actual_cost(price_per_mtok)
        if cost is not None:
            lines.append(f"  cost          ${cost:,.4f}")
        return "\n".join(lines)


def plan(
    conn: psycopg.Connection,
    dataset: str,
    provider: EmbeddingProvider,
    config: CeilingConfig,
) -> CeilingPlan:
    """Estimate a run without calling the API."""
    pending = queries.chunks_awaiting_ceiling(conn, dataset, provider.model_name)
    done, total = queries.ceiling_coverage(conn, dataset, provider.model_name)
    characters = sum(len(content) for _, content in pending)
    estimated_tokens = int(characters / CHARS_PER_TOKEN)
    batches = -(-len(pending) // config.batch_size) if pending else 0
    return CeilingPlan(
        pending_chunks=len(pending),
        already_done=done,
        total_chunks=total,
        estimated_tokens=estimated_tokens,
        estimated_requests=batches,
    )


def run(
    conn: psycopg.Connection,
    dataset: str,
    provider: EmbeddingProvider,
    config: CeilingConfig,
    *,
    limit: int | None = None,
) -> CeilingReport:
    """Embed every chunk lacking a ceiling vector for this model."""
    _assert_dimension(conn, provider)

    pending = queries.chunks_awaiting_ceiling(conn, dataset, provider.model_name, limit=limit)
    report = CeilingReport()
    started = time.perf_counter()

    for start in range(0, len(pending), config.batch_size):
        batch = pending[start : start + config.batch_size]
        ids = [chunk_id for chunk_id, _ in batch]
        texts = [content for _, content in batch]

        # purpose="document": the corpus is being indexed, not searched.
        # Voyage prepends a different instruction per type, so getting this
        # wrong would understate the hosted model and invert the ceiling.
        vectors = provider.embed(texts, purpose="document")
        queries.insert_ceiling_vectors(conn, ids, vectors, provider.model_name)
        # Commit per batch, so an interruption keeps what was already paid for.
        conn.commit()

        report.embedded += len(batch)
        report.requests += 1

    report.total_tokens = int(getattr(provider, "total_tokens", 0) or 0)
    report.elapsed_seconds = time.perf_counter() - started
    return report


def _assert_dimension(conn: psycopg.Connection, provider: EmbeddingProvider) -> None:
    """Check the provider against the actual column width.

    Read from the catalogue rather than compared to a constant: the column is
    the authority, and a literal here would be a second place to keep in sync.

    pgvector stores the dimension in atttypmod directly. The familiar
    `atttypmod - 4` is varchar's length convention and does not apply here --
    it yielded 1020 for a vector(1024) column and blocked every run until a
    test caught it.
    """
    row = conn.execute(
        """
        SELECT a.atttypmod
        FROM pg_attribute a
        WHERE a.attrelid = 'ceiling_vectors'::regclass AND a.attname = 'embedding'
        """
    ).fetchone()
    column_dimension = int(row[0]) if row else -1
    if provider.dimension != column_dimension:
        msg = (
            f"{provider.model_name} emits {provider.dimension} dimensions but "
            f"ceiling_vectors.embedding is vector({column_dimension}). A "
            f"candidate that cannot emit {column_dimension} needs its own "
            f"table (ADR-0006)."
        )
        raise ValueError(msg)
