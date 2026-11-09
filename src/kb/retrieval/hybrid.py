"""Hybrid retrieval: Postgres full-text search plus pgvector, fused by RRF.

Per ADR-0005. Both arms run, each contributing its own top candidates, and
reciprocal rank fusion combines them. Neither arm alone is sufficient for this
corpus: vector search misses exact identifiers (function names, error strings,
config keys) that make up half of what people ask about code, and keyword
search misses paraphrase, which is most of how people ask questions at all.

The fusion happens in SQL rather than in Python. One round trip, and the
database is already the thing holding both indexes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from kb.config import RetrievalConfig
from kb.providers.base import EmbeddingProvider


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk with enough context to cite it without a second query."""

    chunk_id: UUID
    document_id: UUID
    content: str
    source_type: str
    source_uri: str
    content_hash: str
    title: str | None
    author: str | None
    created_at: datetime | None
    file_path: str | None
    start_line: int | None
    end_line: int | None
    heading: str | None
    rrf_score: float
    keyword_rank: int | None
    vector_rank: int | None

    @property
    def found_by(self) -> str:
        """Which arms surfaced this chunk. Useful when a result looks wrong."""
        arms: list[str] = []
        if self.keyword_rank is not None:
            arms.append("keyword")
        if self.vector_rank is not None:
            arms.append("vector")
        return "+".join(arms)

    @property
    def citation(self) -> str:
        """The citation format from docs/SPECS.md.

        A citation must resolve to something a reader can open; one that is
        right about the document and wrong about the location is still a
        failure, which is why the locator fields are carried this far.
        """
        if self.source_type == "code" and self.file_path:
            if self.start_line and self.end_line and self.end_line != self.start_line:
                return f"[{self.file_path}:{self.start_line}-{self.end_line}]"
            if self.start_line:
                return f"[{self.file_path}:{self.start_line}]"
            return f"[{self.file_path}]"
        if self.source_type == "email":
            date = self.created_at.date().isoformat() if self.created_at else "undated"
            return f'[dev@ "{self.title or self.source_uri}" ({date})]'
        if self.source_type == "ticket":
            return f"[#{self.source_uri.lstrip('#')}]"
        if self.heading:
            return f"[{self.source_uri} § {self.heading}]"
        return f"[{self.source_uri}]"

    def as_log_entry(self) -> dict[str, Any]:
        """The shape the query log records (ADR-0023).

        `content_hash` is the version identity: it answers "which version of
        this document produced that answer", which is the audit question.
        """
        return {
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "content_hash": self.content_hash,
            "keyword_rank": self.keyword_rank,
            "vector_rank": self.vector_rank,
            "rrf_score": self.rrf_score,
        }


# Each arm filters independently rather than sharing a pre-filtered CTE, so the
# planner can still reach the HNSW and GIN indexes instead of scanning a
# materialised intermediate.
#
# The keyword arm's tsquery is built by Postgres from the query text: the text
# is lexed with the same 'english' configuration as the stored tsvector, then
# the lexemes are OR-ed. Postgres' own plainto_tsquery and websearch_to_tsquery
# both AND every term, which returns nothing at all for a natural-language
# question. quote_literal makes the construction injection-safe while keeping
# identifiers like `scheduler_job.py:412` intact as single lexemes.
_SEARCH_SQL = """
WITH q AS (
    SELECT to_tsquery('english', string_agg(quote_literal(lexeme), ' | ')) AS tsq
    FROM unnest(to_tsvector('english', %(query_text)s))
),
keyword AS (
    SELECT c.id,
           row_number() OVER (ORDER BY ts_rank(c.tsv, q.tsq) DESC, c.id) AS rank
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    CROSS JOIN q
    WHERE d.dataset = %(dataset)s
      AND d.status = 'indexed'
      AND c.embedding_model = %(embedding_model)s
      AND q.tsq IS NOT NULL
      AND c.tsv @@ q.tsq
    ORDER BY ts_rank(c.tsv, q.tsq) DESC, c.id
    LIMIT %(candidates)s
),
vector AS (
    SELECT c.id,
           row_number() OVER (ORDER BY c.embedding <=> %(query_vector)s::vector, c.id) AS rank
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.dataset = %(dataset)s
      AND d.status = 'indexed'
      AND c.embedding_model = %(embedding_model)s
    ORDER BY c.embedding <=> %(query_vector)s::vector, c.id
    LIMIT %(candidates)s
),
fused AS (
    SELECT id,
           sum(1.0 / (%(rrf_k)s + rank)) AS rrf_score,
           max(keyword_rank) AS keyword_rank,
           max(vector_rank) AS vector_rank
    FROM (
        SELECT id, rank, rank AS keyword_rank, NULL::bigint AS vector_rank
        FROM keyword
        UNION ALL
        SELECT id, rank, NULL::bigint AS keyword_rank, rank AS vector_rank
        FROM vector
    ) arms
    GROUP BY id
)
SELECT c.id, c.document_id, c.content, c.file_path, c.start_line, c.end_line,
       c.heading,
       d.source_type, d.source_uri, d.content_hash, d.title, d.author,
       d.created_at,
       f.rrf_score, f.keyword_rank, f.vector_rank
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
ORDER BY f.rrf_score DESC, c.id
LIMIT %(top_k)s
"""


def search(
    conn: psycopg.Connection,
    query: str,
    *,
    dataset: str,
    provider: EmbeddingProvider,
    config: RetrievalConfig | None = None,
) -> list[RetrievedChunk]:
    """Retrieve the top chunks for a query.

    The query is embedded with `purpose="query"`, not `"document"`. On a
    provider that distinguishes them this is the difference between a good and
    a degraded search; the local model ignores it, but getting it wrong here
    would silently rig the ceiling comparison (ADR-0006).
    """
    settings = config or RetrievalConfig()

    # A blank query is refused before it reaches either arm. The keyword arm
    # would correctly match nothing, but vector search has no concept of "no
    # match" -- it always returns its nearest k, so whitespace would retrieve
    # arbitrary chunks that the agent could then present as relevant.
    #
    # This guard covers blank input only. A meaningful query with no good
    # answer still returns nearest neighbours by design; deciding that none of
    # them support an answer is abstention, which is the agent's job.
    if not query.strip():
        return []

    query_vector = provider.embed([query], purpose="query")[0]

    rows = conn.execute(
        _SEARCH_SQL,
        {
            "query_text": query,
            "query_vector": query_vector,
            "dataset": dataset,
            "embedding_model": provider.model_name,
            "candidates": settings.candidates_per_arm,
            "rrf_k": settings.rrf_k,
            "top_k": settings.top_k,
        },
    ).fetchall()
    return [_to_chunk(row) for row in rows]


def _to_chunk(row: tuple[Any, ...]) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=UUID(str(row[0])),
        document_id=UUID(str(row[1])),
        content=str(row[2]),
        file_path=row[3],
        start_line=row[4],
        end_line=row[5],
        heading=row[6],
        source_type=str(row[7]),
        source_uri=str(row[8]),
        content_hash=str(row[9]),
        title=row[10],
        author=row[11],
        created_at=row[12],
        rrf_score=float(row[13]),
        keyword_rank=int(row[14]) if row[14] is not None else None,
        vector_rank=int(row[15]) if row[15] is not None else None,
    )
