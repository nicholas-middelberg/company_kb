"""The ingestion pipeline: discover -> parse -> chunk -> embed -> upsert.

Milestone 1 runs this in-process with no queue, but through the same stage
boundaries the Milestone 4 workers will use, so that milestone changes the
transport rather than rewriting the stages.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from kb.adapters.base import Adapter, Chunk, SourceDocument
from kb.config import Config
from kb.db import queries
from kb.ingest.hashing import content_hash
from kb.providers.base import EmbeddingProvider


@dataclass
class IngestReport:
    """What a run did. Printed, and asserted on by tests.

    `embedding_calls` is reported because embedding is the expensive stage and
    exit criterion 1 is that a second run over unchanged content performs zero
    of them.
    """

    discovered: int = 0
    skipped_unchanged: int = 0
    indexed: int = 0
    failed: int = 0
    chunks_written: int = 0
    embedding_calls: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def render(self) -> str:
        lines = [
            f"  discovered        {self.discovered}",
            f"  skipped unchanged {self.skipped_unchanged}",
            f"  indexed           {self.indexed}",
            f"  failed            {self.failed}",
            f"  chunks written    {self.chunks_written}",
            f"  embedding calls   {self.embedding_calls}",
        ]
        lines.extend(f"  FAILED {uri}: {error}" for uri, error in self.failures)
        return "\n".join(lines)


def ingest(
    conn: psycopg.Connection,
    config: Config,
    adapters: Sequence[Adapter],
    root: Path,
    provider: EmbeddingProvider,
) -> IngestReport:
    """Run every adapter over root, writing results into the database."""
    if provider.dimension != config.embedding.dimension:
        msg = (
            f"provider emits {provider.dimension} dimensions, config says "
            f"{config.embedding.dimension}"
        )
        raise ValueError(msg)

    report = IngestReport()
    for adapter in adapters:
        for document in adapter.discover(root):
            report.discovered += 1
            _ingest_one(conn, config, adapter, document, provider, report)
    return report


def _ingest_one(
    conn: psycopg.Connection,
    config: Config,
    adapter: Adapter,
    document: SourceDocument,
    provider: EmbeddingProvider,
    report: IngestReport,
) -> None:
    """Ingest one document, isolating its failure from the rest of the run.

    One unparseable file marks that document failed and the run continues. A
    corpus of real mailing lists and third-party code will always contain
    something that does not parse, and aborting the run on the first one means
    never ingesting anything.
    """
    digest = content_hash(document.content)
    existing = queries.existing_state(conn, config.dataset, document.source_uri)

    # Skip only what is both unchanged *and* already indexed. Matching on the
    # hash alone would permanently skip a document that failed last run, since
    # its hash is recorded even though it was never indexed.
    if existing is not None and existing[1] == digest and existing[2] == "indexed":
        report.skipped_unchanged += 1
        return

    try:
        document_id = queries.upsert_document(
            conn, config.dataset, document, digest, status="processing"
        )
        # Commit the row before the work that can fail. The status enum is only
        # useful if the row outlives the failure: rolling the whole thing back
        # reports the failure in this run's output and leaves no trace of it in
        # the database, so `kb status` shows nothing wrong.
        conn.commit()
    except Exception as error:
        conn.rollback()
        report.failed += 1
        report.failures.append((document.source_uri, _describe(error)))
        return

    try:
        chunks: Sequence[Chunk] = adapter.chunk(document)
        embeddings, calls = _embed_in_batches(
            provider, [c.content for c in chunks], config.embedding.batch_size
        )
        report.embedding_calls += calls
        written = queries.replace_chunks(conn, document_id, chunks, embeddings, provider.model_name)
        queries.set_status(conn, document_id, "indexed")
        conn.commit()
        report.indexed += 1
        report.chunks_written += written
    except Exception as error:
        conn.rollback()
        # Clear any chunks from a previous version. A failed document must
        # contribute nothing to retrieval, or the agent will cite text that is
        # no longer in the source through a citation that looks entirely valid.
        queries.delete_chunks(conn, document_id)
        queries.set_status(conn, document_id, "failed")
        conn.commit()
        report.failed += 1
        report.failures.append((document.source_uri, _describe(error)))


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _embed_in_batches(
    provider: EmbeddingProvider, texts: Sequence[str], batch_size: int
) -> tuple[list[list[float]], int]:
    """Embed in batches, returning the vectors and the number of calls made."""
    vectors: list[list[float]] = []
    calls = 0
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        if not batch:
            continue
        vectors.extend(provider.embed(batch))
        calls += 1
    return vectors, calls
