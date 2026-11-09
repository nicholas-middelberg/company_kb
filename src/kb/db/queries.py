"""Parameterised queries. No SQL is built by string concatenation."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import psycopg

from kb.adapters.base import Chunk, SourceDocument


def existing_state(
    conn: psycopg.Connection, dataset: str, source_uri: str
) -> tuple[UUID, str, str] | None:
    """Stored (id, content_hash, status), or None if never ingested.

    Status is part of the answer because the skip decision depends on it: an
    unchanged document that previously *failed* must not be skipped as though
    it were already indexed.
    """
    row = conn.execute(
        "SELECT id, content_hash, status FROM documents WHERE dataset = %s AND source_uri = %s",
        (dataset, source_uri),
    ).fetchone()
    if row is None:
        return None
    return UUID(str(row[0])), str(row[1]), str(row[2])


def delete_chunks(conn: psycopg.Connection, document_id: UUID) -> int:
    """Remove a document's chunks.

    Used when a document fails: a failed document must contribute nothing to
    retrieval. Leaving the previous version's chunks in place would have the
    agent cite text that no longer exists in the source, via a citation that
    looks perfectly valid.
    """
    cur = conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
    return cur.rowcount


def upsert_document(
    conn: psycopg.Connection,
    dataset: str,
    document: SourceDocument,
    content_hash: str,
    status: str = "processing",
) -> UUID:
    """Insert or update by (dataset, source_uri), returning the id."""
    row = conn.execute(
        """
        INSERT INTO documents (
            dataset, source_type, source_uri, title, author,
            created_at, content_hash, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset, source_uri) DO UPDATE SET
            source_type  = EXCLUDED.source_type,
            title        = EXCLUDED.title,
            author       = EXCLUDED.author,
            created_at   = EXCLUDED.created_at,
            content_hash = EXCLUDED.content_hash,
            status       = EXCLUDED.status,
            ingested_at  = now()
        RETURNING id
        """,
        (
            dataset,
            document.source_type,
            document.source_uri,
            document.title,
            document.author,
            document.created_at,
            content_hash,
            status,
        ),
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        msg = "upsert_document returned no id"
        raise RuntimeError(msg)
    return UUID(str(row[0]))


def replace_chunks(
    conn: psycopg.Connection,
    document_id: UUID,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Delete this document's chunks and write the new set.

    Replace rather than merge: chunk boundaries shift when content changes, so
    ordinals from a previous version do not line up and a merge would leave
    orphaned text that still answers questions.
    """
    if len(chunks) != len(embeddings):
        msg = f"{len(chunks)} chunks but {len(embeddings)} embeddings"
        raise ValueError(msg)

    conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (
                document_id, ordinal, content, embedding, embedding_model,
                file_path, start_line, end_line, heading, speaker, message_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    document_id,
                    chunk.ordinal,
                    chunk.content,
                    list(vector),
                    embedding_model,
                    chunk.file_path,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.heading,
                    chunk.speaker,
                    chunk.message_id,
                )
                for chunk, vector in zip(chunks, embeddings, strict=True)
            ],
        )
    return len(chunks)


def set_status(conn: psycopg.Connection, document_id: UUID, status: str) -> None:
    conn.execute("UPDATE documents SET status = %s WHERE id = %s", (status, document_id))


def status_counts(conn: psycopg.Connection, dataset: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) FROM documents WHERE dataset = %s GROUP BY status",
        (dataset,),
    ).fetchall()
    return {str(status): int(count) for status, count in rows}


def chunk_count(conn: psycopg.Connection, dataset: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE d.dataset = %s",
        (dataset,),
    ).fetchone()
    return int(row[0]) if row else 0


def embedding_models(conn: psycopg.Connection, dataset: str) -> list[str]:
    """Distinct models present. More than one means retrieval must filter."""
    rows = conn.execute(
        "SELECT DISTINCT c.embedding_model FROM chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE d.dataset = %s",
        (dataset,),
    ).fetchall()
    return sorted(str(row[0]) for row in rows)


def chunks_awaiting_ceiling(
    conn: psycopg.Connection,
    dataset: str,
    embedding_model: str,
    limit: int | None = None,
) -> list[tuple[UUID, str]]:
    """Chunks with no ceiling vector yet for this model.

    The anti-join is what makes the ceiling run resumable. It bills per token,
    so a run that dies two thirds of the way through must not re-pay for the
    part that already succeeded.
    """
    sql = """
        SELECT c.id, c.content
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN ceiling_vectors cv
          ON cv.chunk_id = c.id AND cv.embedding_model = %s
        WHERE d.dataset = %s AND d.status = 'indexed' AND cv.chunk_id IS NULL
        ORDER BY c.document_id, c.ordinal
    """
    params: list[object] = [embedding_model, dataset]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [(UUID(str(row[0])), str(row[1])) for row in rows]


def insert_ceiling_vectors(
    conn: psycopg.Connection,
    chunk_ids: Sequence[UUID],
    embeddings: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Write one batch of ceiling vectors.

    ON CONFLICT DO NOTHING rather than DO UPDATE: a vector already present was
    produced by the same model over the same chunk text, so overwriting it
    would spend tokens to replace a row with its own value.
    """
    if len(chunk_ids) != len(embeddings):
        msg = f"{len(chunk_ids)} chunks but {len(embeddings)} embeddings"
        raise ValueError(msg)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ceiling_vectors (chunk_id, embedding_model, embedding)
            VALUES (%s, %s, %s)
            ON CONFLICT (chunk_id, embedding_model) DO NOTHING
            """,
            [
                (chunk_id, embedding_model, list(vector))
                for chunk_id, vector in zip(chunk_ids, embeddings, strict=True)
            ],
        )
    return len(chunk_ids)


def ceiling_coverage(
    conn: psycopg.Connection, dataset: str, embedding_model: str
) -> tuple[int, int]:
    """(chunks with a ceiling vector, total indexed chunks) for this dataset."""
    row = conn.execute(
        """
        SELECT count(cv.chunk_id), count(c.id)
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN ceiling_vectors cv
          ON cv.chunk_id = c.id AND cv.embedding_model = %s
        WHERE d.dataset = %s AND d.status = 'indexed'
        """,
        (embedding_model, dataset),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)
