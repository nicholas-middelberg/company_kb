-- 002_ceiling_vectors.sql
--
-- Storage for the hosted reference run that establishes the quality ceiling
-- (ADR-0006). Forward-only and never edited once applied (ADR-0029).

BEGIN;

-- Separate table, not another column on chunks, for two reasons. The hosted
-- model's dimension differs from the serving model's and a vector column holds
-- exactly one dimension; and vectors from different models must never meet in
-- one similarity search, which a second column on the same row invites.
CREATE TABLE ceiling_vectors (
    chunk_id        uuid         NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    embedding_model text         NOT NULL,
    embedding       vector(1024) NOT NULL,
    created_at      timestamptz  NOT NULL DEFAULT now(),

    -- Composite, so two hosted models can be compared without a migration.
    -- Both must emit 1024 to share this table: the whole current Voyage lineup
    -- defaults to it and every Qwen3-Embedding size can truncate to it
    -- (ADR-0006), but a candidate that cannot needs its own table.
    PRIMARY KEY (chunk_id, embedding_model)
);

COMMENT ON TABLE ceiling_vectors IS
    'Hosted embeddings of the serving corpus, used only to measure a quality '
    'ceiling. Never a retrieval source for the agent.';

COMMENT ON COLUMN ceiling_vectors.chunk_id IS
    'Keyed to chunks, not documents, and deliberately. The ceiling isolates '
    'the embedding model only if chunk boundaries are identical on both sides; '
    're-chunking and re-embedding at once would measure the two together and '
    'attribute the result to neither. ON DELETE CASCADE because a replaced '
    'chunk''s ceiling vector describes text that no longer exists.';

CREATE INDEX ceiling_vectors_model_idx ON ceiling_vectors (embedding_model);

-- Cosine, matching the serving-side index, so the comparison differs in the
-- embedding model and nothing else.
CREATE INDEX ceiling_vectors_embedding_idx
    ON ceiling_vectors USING hnsw (embedding vector_cosine_ops);

COMMIT;
