-- 001_initial_schema.sql
--
-- Milestone 1 schema: documents, chunks, and the query log.
-- Forward-only and never edited once applied (ADR-0029).
-- Applied by scripts/migrate.sh.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ----------------------------------------------------------------- documents
CREATE TABLE documents (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset      text        NOT NULL,
    source_type  text        NOT NULL,
    source_uri   text        NOT NULL,
    title        text,
    author       text,
    created_at   timestamptz,
    content_hash text        NOT NULL,
    status       text        NOT NULL DEFAULT 'queued',
    ingested_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT documents_source_type_valid
        CHECK (source_type IN ('code', 'email', 'doc', 'ticket')),
    CONSTRAINT documents_status_valid
        CHECK (status IN ('queued', 'processing', 'indexed', 'failed')),
    CONSTRAINT documents_dataset_source_uri_key
        UNIQUE (dataset, source_uri)
);

COMMENT ON COLUMN documents.created_at IS
    'The source''s own date (commit, email, ticket) -- not when we ingested it.';
COMMENT ON COLUMN documents.content_hash IS
    'SHA-256 of normalised content. Drives ingestion idempotency: an unchanged '
    'hash means skip, with no re-parse and no re-embed.';
COMMENT ON COLUMN documents.status IS
    'queued | processing | indexed | failed. Milestone 1 ingestion is '
    'synchronous and effectively only writes indexed or failed; the column '
    'exists now so the Milestone 4 queue workers need no schema change.';

-- Milestone 2 adds an access_list column here plus the row-level security
-- policy (ADR-0012). Nothing in this migration should make that harder.

-- -------------------------------------------------------------------- chunks
CREATE TABLE chunks (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid        NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    ordinal         int         NOT NULL,
    content         text        NOT NULL,
    embedding       vector(768) NOT NULL,
    embedding_model text        NOT NULL,
    tsv             tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Source-specific locators, so a citation points at something openable.
    file_path       text,
    start_line      int,
    end_line        int,
    heading         text,
    speaker         text,
    message_id      text,

    CONSTRAINT chunks_document_ordinal_key UNIQUE (document_id, ordinal)
);

COMMENT ON COLUMN chunks.embedding IS
    'Dimension 768 is derived from the configured embedding model '
    '(gte-modernbert-base, ADR-0022), not pinned by preference (ADR-0006). '
    'vector(N) is fixed-width, so ingestion refuses to start when the '
    'provider''s reported dimension disagrees with this column.';
COMMENT ON COLUMN chunks.embedding_model IS
    'Required. Every vector query filters on it, because cosine distance '
    'between vectors from two different models is a meaningless number that '
    'raises no error (ADR-0006).';

CREATE INDEX chunks_document_id_idx ON chunks (document_id);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX chunks_embedding_model_idx ON chunks (embedding_model);

-- Cosine, matching the retrieval spec. HNSW cannot be composite, so the
-- embedding_model filter is applied as a post-filter; with a single active
-- model that costs nothing. If two models ever co-reside, the right shape is
-- one partial HNSW index per model rather than a wider index here.
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

-- -------------------------------------------------------------- interactions
-- Query log and audit trail (ADR-0023).
--
-- This table deliberately has no chunk, embedding or tsvector column, and no
-- index supporting similarity search. The rule that the log is never a
-- retrieval source is enforced by the table's shape, not by remembering it:
-- indexing the agent's own answers would let one wrong answer become its own
-- supporting evidence, and that error is self-reinforcing and nearly
-- impossible to trace back.
CREATE TABLE interactions (
    id              uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz    NOT NULL DEFAULT now(),
    user_id         text,
    question        text           NOT NULL,
    answer          text,
    abstained       boolean        NOT NULL DEFAULT false,
    retrieved       jsonb          NOT NULL DEFAULT '[]'::jsonb,
    llm_model       text,
    embedding_model text,
    prompt_version  text,
    effort          text,
    latency_ms      int,
    input_tokens    int,
    output_tokens   int,
    cost_usd        numeric(12, 6),
    error           text
);

COMMENT ON COLUMN interactions.user_id IS
    'A local CLI identity in Milestone 1; a verified subject from Milestone 2.';
COMMENT ON COLUMN interactions.retrieved IS
    'Per retrieved chunk: document_id, chunk_id, content_hash, per-arm rank '
    'and fused score. content_hash is the version identity -- it answers '
    '"which version of this document produced that answer", which is the '
    'audit question.';
COMMENT ON COLUMN interactions.abstained IS
    'Separates "I do not know" from a failure. Both are written: a log that '
    'records only successes hides the interactions worth reviewing, and those '
    'are the raw material for gap detection in Milestone 6.';

CREATE INDEX interactions_created_at_idx ON interactions (created_at DESC);

COMMIT;
