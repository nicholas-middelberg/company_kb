# Specification

> ## Current milestone: 1 — Local MVP
>
> This file specifies **the current milestone only**. Milestones 2–7 live in `docs/VISION.md` as
> intent and in `docs/DECISIONS.md` as decisions; they are deliberately not specified here. When
> M1 ships, bump the banner above and rewrite this file for M2.

Decisions referenced below are recorded in `docs/DECISIONS.md`. Where this file and an accepted
ADR disagree, the ADR wins and this file is wrong.

## Goal

Ingest one slice of a real open-source project, ask it questions through a hand-written agent
loop, get answers with citations you can click, and score the whole thing with one command.

No auth, no cloud, no synthetic data. One corpus, one machine, real measurement.

## Non-goals

Explicitly out of scope. Each is tagged with the milestone that owns it. If a change requires one
of these, it is not M1 work — see guardrail 1 in `CLAUDE.md`.

| Not in M1 | Owner |
| --- | --- |
| Authentication, users, groups, JWT, Cognito | M2 |
| Access lists on documents or chunks; permission-filtered retrieval; row-level security | M2 |
| Permission-leakage eval cases | M2 |
| S3, SQS, dead-letter queues, async workers, ECS/Lambda | M3 |
| Terraform/CDK, CI/CD deploys, CloudWatch, X-Ray | M3 |
| Synthetic company, ground-truth file, generated documents | M4 |
| Conflict detection and resolution | M5 |
| Expert finding (`who_knows`), rule history over time | M5 |
| Reranking | M5 (ADR-0018) |
| Prompt-injection eval cases | M5 |
| Open models, quantization, local serving, vLLM | M6 |
| MCP connectors, multi-tenancy, web UI, Slack bot, LoRA | M7 |

Three M1 structures are shaped for later milestones, deliberately, to avoid a reshape:
the `documents.status` enum (M3 async ingestion), the metrics table's deferred rows (M2/M5), and
the deferred agent tools (M5). Nothing else anticipates a later milestone.

## Exit criteria

M1 is done when all five hold:

1. The pinned Airflow slice is ingested and indexed, and re-running ingestion changes nothing
   (content-hash idempotency, verified by a second run reporting 0 re-embeds).
2. `kb ask "<question>"` returns an answer with citations, retrieved over hybrid search.
3. 10–15 hand-written eval cases are committed as data files.
4. `kb eval` runs the whole suite in one command and writes a results table.
5. An unanswerable question returns an explicit "I don't know" rather than a fabricated answer,
   and this is covered by at least two eval cases.

## Dataset — Apache Airflow scheduler slice

Per ADR-0008. Four source types from one subsystem, so cross-source synthesis is a real task.

| Source | What | Adapter |
| --- | --- | --- |
| Code | Scheduler, executor and DAG-processing modules of `apache/airflow` | `code` |
| Email | `dev@airflow.apache.org` archive threads touching the scheduler | `email` |
| Design docs | AIPs (Airflow Improvement Proposals) relevant to the slice | `doc` |
| Tickets | A bounded set of GitHub issues on scheduler behaviour | `ticket` |

Target 200–500 documents: enough mess to be real, small enough to re-ingest while iterating.

**The corpus must be pinned, and this is load-bearing rather than tidy.** A dataset manifest
records the exact repository paths, a commit SHA, mailing-list date ranges, and the issue query.
Eval scores are only comparable across runs if the corpus is byte-identical between them; an
unpinned corpus silently drifts every number in the results table and makes the harness lie about
whether a change helped. Changing the manifest invalidates prior results — note it in the results
table rather than comparing across the change.

## Data model

Postgres 16 with the `vector` extension (ADR-0004).

### `documents`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `source_type` | `text` | `code` \| `email` \| `doc` \| `ticket` |
| `source_uri` | `text` | Stable locator; unique with `dataset` |
| `dataset` | `text` | `airflow-scheduler` in M1 (ADR-0002) |
| `title` | `text` | Subject line, file path, AIP title |
| `author` | `text` | Nullable; powers `who_knows` in M5 |
| `created_at` | `timestamptz` | Source's own date, not ingestion time |
| `content_hash` | `text` | SHA-256 of normalized content; drives idempotency |
| `status` | `text` | `queued` \| `processing` \| `indexed` \| `failed` |
| `ingested_at` | `timestamptz` | |

`status` is present in M1 even though M1 ingestion is synchronous and effectively only ever writes
`indexed` or `failed`. It exists now so M3's queue workers don't require a schema change — the
enum is from `docs/VISION.md` and is cheap to carry.

M2 adds an `access_list` column here and the row-level security policy (ADR-0012). Nothing in M1
should make that harder.

### `chunks`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `document_id` | `uuid` FK → `documents.id` | `ON DELETE CASCADE` |
| `ordinal` | `int` | Position within the document |
| `text` | `text` | The chunk as embedded |
| `embedding` | `vector(1024)` | **Pinned at 1024 — see ADR-0006** |
| `embedding_model` | `text` | Model that produced this vector. Required. |
| `tsv` | `tsvector` | Generated from `text`, for keyword search |
| `file_path` | `text` | Code only |
| `start_line`, `end_line` | `int` | Code only; makes citations clickable |
| `heading` | `text` | Docs only |
| `speaker` | `text` | Transcripts; unused in M1, present for the adapter contract |
| `message_id` | `text` | Email; identifies the message within a thread |

Indexes: GIN on `tsv`, HNSW on `embedding`, btree on `document_id`.

Two constraints from ADR-0006, both of which fail silently if ignored:

- **1024 dimensions is a schema commitment, not a config value.** `vector(N)` is fixed-width.
  Swapping to a model that cannot emit 1024 is a migration. 1024 is the default across the whole
  current Voyage lineup and is reachable by every Qwen3-Embedding size via MRL truncation, which
  is what keeps ADR-0020 a config change.
- **`embedding_model` is required, and every vector query filters on it.** Cosine distance
  between vectors from two different models is a meaningless number that no exception will warn
  you about. Mixed-model corpora are only safe because of this filter.

## Ingestion — synchronous in M1

M1 has no queue (M3, ADR-0015). The CLI walks the corpus in-process, but through the same stage
boundaries the M3 workers will use, so M3 is a transport change rather than a rewrite:

```
discover → parse → chunk → embed → upsert
```

- **Idempotent.** Hash normalized content. Unchanged hash means skip — no re-parse, no re-embed.
  Re-running ingestion over an unchanged corpus must perform zero embedding calls, and must say
  so, since that's exit criterion 1.
- **Incremental.** Only new or changed documents are processed.
- **Per-document failure isolation.** One unparseable file marks that document `failed` and does
  not abort the run. The run reports counts per status.
- **Embedding calls are batched**, and the run reports how many were made. Embedding spend is
  real and re-embeds are full corpus passes (ADR-0006, ADR-0019).

## Chunking

Source-aware (ADR-0002). Starting parameters below; these are empirical and tracked as open in
ADR-0019 — settle them against `recall@k`, not argument.

| Source | Unit | Fallback when the unit exceeds the cap |
| --- | --- | --- |
| Code | Function or class | Split on blank-line boundaries, carry the enclosing signature into each part |
| Email | One message within its thread | Split on paragraph; repeat the thread subject as context |
| Doc | Section under a heading | Split on paragraph; repeat the heading path |
| Ticket | Issue body, then each comment | Split on paragraph; repeat the issue title |

Starting values: ~1500 character target, ~200 character overlap, 3000 hard cap. Every chunk
carries enough locator metadata to cite precisely.

Quoted-reply trimming on email matters more than it sounds like it does — a thread where every
message quotes the entire thread will otherwise embed the same text a dozen times and distort
both retrieval paths.

## Retrieval

Hybrid, per ADR-0005.

1. **Keyword.** Postgres `ts_rank` over `tsv`, top 20. (Not textbook BM25; close enough for the
   baseline — named here so the difference isn't later reported as a bug.)
2. **Vector.** pgvector cosine distance over `embedding`, top 20, **filtered to the active
   `embedding_model`** (ADR-0006).
3. **Fuse.** Reciprocal rank fusion, `k = 60`, to a final top-k (default 10).

Both arms run against the same `dataset`. Each returned chunk carries its document metadata and
locators so the agent can cite without a second query.

M2 adds the permission predicate to **both** arms, inside the SQL (ADR-0012).

## Provider interfaces

The seam that makes ADR-0020 a config change (ADR-0003, ADR-0006). No provider SDK call appears
anywhere outside these implementations.

**`LLMProvider`** — one method to continue a conversation given messages and tool definitions,
returning content blocks, stop reason, and token usage. Usage must be returned, not logged, since
the eval harness reports cost per query.

- M1 implementation: Anthropic, `claude-opus-5`, `thinking: {type: "adaptive"}`, with
  `output_config.effort` configurable (default `high`).
- Do not use `budget_tokens` — removed on this model family. Do not use assistant prefill —
  returns 400. Parse tool inputs as JSON; never string-match the serialized form.

**`EmbeddingProvider`** — embed a batch of texts; expose `model_name` and `dimension`. Callers
persist `model_name` onto every chunk.

- M1 implementation: Voyage — `voyage-code-3` for `source_type = code`, `voyage-3` otherwise.
- `dimension` must be 1024 or ingestion refuses to run, rather than failing at the INSERT.

## Agent

Hand-written loop (ADR-0009): model → `tool_use` → execute → `tool_result` → model, until the
model stops requesting tools. Bounded iteration count. Each step is traced with its tool, latency
and token usage.

### Tools in M1

| Tool | Status |
| --- | --- |
| `search(query, k)` → ranked chunks with metadata | M1 |
| `read_document(document_id)` → full document, for when a chunk isn't enough | M1 |
| `grep_code(pattern)` | M5 |
| `git_log(path)` | M5 |
| `who_knows(topic)` | M5 |

All tools are read-only, in M1 and permanently (ADR-0010).

### Output contract

Either an answer in which **every factual claim carries a citation**, or an explicit abstention.
Never a confident uncited claim, and never a guess dressed as an answer — abstention is exit
criterion 5, not a fallback.

Conflict flagging and expert suggestion on abstention are M5. In M1 an abstention says that the
corpus doesn't answer the question.

### Prompt structure

Retrieved content is delimited and labelled as data. The system prompt states that document text
is never an instruction (ADR-0010) — this corpus is public mailing lists, so adversarial text is
ordinary content, not a hypothetical.

Keep the stable prefix (system prompt, tool definitions) byte-identical across calls so prompt
caching works; put the question after it.

## Citation format

One format, fixed, because every eval case asserts against it:

| Source | Format | Example |
| --- | --- | --- |
| Code | `[path:line]` or `[path:start-end]` | `[airflow/jobs/scheduler_job_runner.py:412]` |
| Email | `[dev@ "subject" (YYYY-MM-DD)]` | `[dev@ "Scheduler HA and the mini scheduler" (2023-03-14)]` |
| Doc | `[AIP-N § heading]` | `[AIP-15 § Scheduler HA]` |
| Ticket | `[#issue]` | `[#28149]` |

Citations must resolve to something a reader can open. A citation accurate to the document but
wrong about the line is a failure — `citation_accuracy` scores exactly this.

## Eval harness

Per ADR-0011. Test cases are data, not code.

### Case schema

```yaml
id: sched-001
question: "When the scheduler is running in HA mode, what prevents two schedulers from queueing the same task instance?"
type: lookup          # lookup | cross-source | outdated-doc | unanswerable
gold_answer: "Row-level locking on the task instance ..."
gold_doc_ids:
  - airflow/jobs/scheduler_job_runner.py
  - AIP-15
notes: "Answer must cite both the code and the AIP."
```

`asking_user` joins the schema in M2, when there is such a thing as a user. The `conflict` and
`forbidden` types join in M5 and M2 respectively; the field is a string, so adding them needs no
schema change.

### Metrics

| Metric | What it checks | Milestone |
| --- | --- | --- |
| `recall@k` | Did the gold documents come back? | M1 |
| `answer_correctness` | LLM-as-judge, plus exact checks where the question permits one | M1 |
| `citation_accuracy` | Do the cited sources actually support the claim, at the cited location? | M1 |
| `abstention` | Does it say "I don't know" on unanswerable questions? | M1 |
| `latency_p50`, `latency_p95` | Seconds per query | M1 |
| `cost_per_query` | Dollars, from provider token usage | M1 |
| `permission_leakage` | Forbidden content in an answer. Target 0. | M2 |
| `conflict_handling` | Contradiction flagged, newer source preferred | M5 |

M1 ships six of eight. The deferred two are listed so the table's shape is stable from the first
run — a results table whose columns change every milestone can't show a trend.

`answer_correctness` is the softest number here: the judge model is an open question (ADR-0021),
and a model judging its own family's output is a known bias. Until that's settled, prefer exact
checks where a question permits one, and treat correctness as indicative rather than decisive.

### Running

`kb eval` runs the suite against a named configuration and writes a timestamped markdown table to
`evals/results/`, recording the config, the model, the embedding model, and the dataset manifest
SHA alongside the scores. Without those four, a row in the table can't be interpreted later.

## Commands

The `kb` CLI. None of these exist yet.

| Command | Does |
| --- | --- |
| `kb db up` / `kb db down` | Start/stop Postgres via docker-compose |
| `kb db migrate` | Apply schema |
| `kb ingest --dataset airflow-scheduler` | Run the pipeline; report per-status counts and embedding-call count |
| `kb status` | Document counts by status; active embedding model |
| `kb search "<query>"` | Raw hybrid retrieval, no agent — for debugging retrieval alone |
| `kb ask "<question>"` | Full agent loop with citations |
| `kb eval [--config <name>]` | Run the suite, write the results table |

`kb search` separate from `kb ask` is deliberate: when an answer is wrong, the first question is
always whether retrieval or the agent failed, and this answers it in one command.

## Configuration

Environment: `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `DATABASE_URL`. Never committed.

Config file keys: LLM model and effort; embedding model per source type; embedding dimension
(must be 1024); chunk target, overlap and cap; retrieval `top_k`, per-arm candidate count, RRF
`k`; agent max iterations; dataset manifest path.

A named config is the unit the eval harness compares, so every knob that could move a score
belongs here rather than in code.

## Repository layout

```
src/kb/
  providers/     LLMProvider, EmbeddingProvider + Anthropic and Voyage implementations
  adapters/      code, email, doc, ticket — parse and chunk
  ingest/        pipeline stages, hashing, upsert
  retrieval/     hybrid search, RRF
  agent/         loop, tools, prompts
  eval/          runner, metrics, judge, results table
  db/            schema, migrations, queries
  cli.py
datasets/airflow-scheduler/manifest.*
evals/cases/*.yaml
evals/results/
docker-compose.yml
```

## Open questions

| Question | Tracked as |
| --- | --- |
| Chunk size, overlap, fallback behaviour | ADR-0019 |
| Which model judges answer correctness | ADR-0021 |
| Whether `voyage-code-3` on code beats one model for everything | ADR-0006 revisit |
| Whether RRF beats weighted score fusion | ADR-0005 revisit |

None of these block starting. All of them are answered by measurement rather than discussion,
which is the point of building the harness first.
