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
| Synthetic pharma company, ground-truth file, generated documents | M3 |
| S3, SQS, dead-letter queues, async workers, ECS/Lambda | M4 |
| Terraform/CDK, CI/CD deploys, IaC scanning, CloudWatch, X-Ray | M4 |
| Conflict detection and resolution | M5 |
| Expert finding (`who_knows`), rule history over time | M5 |
| MCP server exposing the agent's tools | M5 |
| Reranking | M5 (ADR-0018) |
| Prompt-injection eval cases | M5 |
| Corrections, verified answers, SME review queue, gap detection | M6 |
| Open models, served comparisons, quantization study, eval-driven LoRA | M7 |
| MCP connectors for external sources, multi-tenancy, web UI, Slack bot | M8 |

Three M1 structures are shaped for later milestones, deliberately, to avoid a reshape:
the `documents.status` enum (M4 async ingestion), the metrics table's deferred rows (M2/M5), and
the deferred agent tools (M5). Nothing else anticipates a later milestone.

## Exit criteria

M1 is done when all seven hold:

1. The pinned Airflow slice is ingested and indexed, and re-running ingestion changes nothing
   (content-hash idempotency, verified by a second run reporting 0 re-embeds).
2. `kb ask "<question>"` returns an answer with citations, retrieved over hybrid search.
3. 10–15 hand-written eval cases are committed as data files.
4. `kb eval` runs the whole suite in one command and writes a results table.
5. An unanswerable question returns an explicit "I don't know" rather than a fabricated answer,
   and this is covered by at least two eval cases.
6. Every interaction is written to the query log as a full trace — including abstentions and
   errors, which are the ones worth reviewing.
7. CI is green on every pull request: pre-commit (ruff, pyright), gitleaks, pytest, and the eval
   smoke set.

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
| `content` | `text` | The chunk as embedded. Named `content`, not `text`: a bare `text` in a Postgres expression collides with typed-literal syntax (`text 'abc'`), which would force quoting it as `"text"` in the generated `tsv` column and every query touching it |
| `embedding` | `vector(768)` | 768 because the configured model emits it; derived, not pinned — ADR-0006, ADR-0022 |
| `embedding_model` | `text` | Model that produced this vector. Required. |
| `tsv` | `tsvector` | Generated from `text`, for keyword search |
| `file_path` | `text` | Code only |
| `start_line`, `end_line` | `int` | Code only; makes citations clickable |
| `heading` | `text` | Docs only |
| `speaker` | `text` | Transcripts; unused in M1, present for the adapter contract |
| `message_id` | `text` | Email; identifies the message within a thread |

Indexes: GIN on `tsv`, HNSW on `embedding` with `vector_cosine_ops`, btree on
`document_id` and on `embedding_model`. HNSW cannot be composite, so the mandatory
`embedding_model` filter is a post-filter — free with one active model. Two
co-resident models would want one partial HNSW index per model instead.

The schema is applied by numbered forward-only migrations in
`src/kb/db/migrations/`, run by `scripts/migrate.sh` (ADR-0029). An applied
migration is never edited; the runner checksums them and refuses to proceed on a
mismatch.

Three constraints from ADR-0006, the first two of which fail silently if ignored:

- **`embedding_model` is required, and every vector query filters on it.** Cosine distance
  between vectors from two different models is a meaningless number that no exception will warn
  you about. Mixed-model corpora are only safe because of this filter.
- **The dimension is validated against the column, not a constant.** `vector(N)` is fixed-width,
  and N is read from the configured provider's reported dimension when the migration runs. It is
  768 today because `gte-modernbert-base` emits 768 (ADR-0022) — a derived value, not a new pin.
  Replacing the model moves the column with it.
- **The Voyage ceiling run stores its vectors separately.** Its dimension will differ from the
  local model's, and a column holds one dimension. See *Quality ceiling* below.

## Ingestion — synchronous in M1

M1 has no queue (M3, ADR-0015). The CLI walks the corpus in-process, but through the same stage
boundaries the M4 workers will use, so M4 is a transport change rather than a rewrite:

```
discover → parse → chunk → embed → upsert
```

- **Idempotent.** Hash normalized content. Unchanged hash means skip — no re-parse, no re-embed.
  Re-running ingestion over an unchanged corpus must perform zero embedding calls, and must say
  so, since that's exit criterion 1.
- **Incremental.** Only new or changed documents are processed.
- **Per-document failure isolation.** One unparseable file marks that document `failed` and does
  not abort the run. The run reports counts per status.
- **Embedding calls are batched within a document**, and the run reports how many were made.
  Batching deliberately does not span documents: each is embedded, written and committed on its
  own so one failure cannot discard another's work. The cost is one call per small document
  rather than filled batches across them — negligible locally, worth knowing before pointing a
  metered provider at a few hundred documents, as the ceiling run will.
- **Discovery prunes excluded directories** (`config.exclude_dirs`). Not hygiene: pointing
  `kb ingest` at a repository root walks into the virtualenv, where this project alone has 11,867
  vendored Python files against 17 of its own.
- **A failed document keeps its row and loses its chunks.** The row must outlive the failure or
  `kb status` shows nothing wrong; the chunks must go or the agent cites text no longer in the
  source through a citation that looks valid.
- **Skip requires unchanged *and* indexed.** Matching on the hash alone would strand a document
  that failed last run, since its hash is recorded even though it was never indexed.

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
persist `model_name` onto every chunk. `embed` also takes `purpose` (`"document"` or `"query"`),
because providers are not symmetric: Voyage prepends a different instruction per type, while the
local model takes raw text for both. Indexing always uses `"document"`.

- M1 default: **`Alibaba-NLP/gte-modernbert-base`** via sentence-transformers, loaded in-process
  — 149M parameters, 768 dimensions, 8192-token limit, Apache 2.0 (ADR-0022). The 8192 limit is
  roughly 8x the chunk cap, so no chunk truncates.
- **Loading is constrained by ADR-0022's supply-chain rules,** which are requirements rather than
  suggestions: pinned `revision="e7f32e3c00f91d699e8c43b53106206bcc72bb22"`,
  `use_safetensors=True` (the repo also ships a `.bin` pickle), never `trust_remote_code=True`,
  and `HF_HUB_OFFLINE=1` at runtime. The pin is an eval-reproducibility control as much as a
  security one — unpinned weights silently invalidate the results table.
- M1 also ships a **Voyage** implementation for the ceiling run below — not on the serving path.
  `voyageai` is an optional extra (`uv sync --extra ceiling`), since a serving install should not
  carry a client it never calls. The provider accepts an injected client, so its tests need
  neither the package nor a network call.
- Ingestion refuses to start when the provider's `dimension` disagrees with the column it is
  about to write, rather than failing at the INSERT or truncating silently.

### Quality ceiling

Per ADR-0006, a Voyage reference run over the identical pinned corpus, scored on the identical
eval suite. This is a measurement, not a serving path, and it exists for attribution: without it,
a weak `recall@k` has two suspects — the local embedding model and the chunk parameters
(ADR-0019) — and no way to separate them.

Because its dimension differs from the local model's, the ceiling run materialises vectors in a
separate table with its own `vector(N)`, keyed to the same documents. Its scores go in the
results table as a distinct configuration, not as a replacement row.

Default `voyage-3-large` at 1024 dimensions, sent explicitly rather than relying on the model
default so the reported dimension cannot drift from the produced one. Voyage caps a request at
1000 texts and bills per **token**, so request count is not the cost — the provider accumulates
`total_tokens` for the run.

`ceiling_vectors` (migration 002) holds them, keyed to **`chunks.id`** rather than to documents
and deliberately so: the ceiling isolates the embedding model only if chunk boundaries are
identical on both sides, and re-chunking and re-embedding at once would measure the two together
and attribute the result to neither. `ON DELETE CASCADE`, because a replaced chunk's ceiling
vector describes text that no longer exists. The primary key is
`(chunk_id, embedding_model)`, so two hosted models can be compared without a migration — both
must emit 1024 to share the table.

The runner (`kb ceiling`) has two properties that follow from billing per token:

- **Resumable.** An anti-join fetches only chunks lacking a vector for this model, and each batch
  commits, so a run that dies partway through does not re-pay for what succeeded.
- **Estimable, and estimating is the default.** `--execute` is required to call the API at all. A
  metered command whose default action is to spend is a bad default.

Batching here spans the whole corpus, unlike ingestion. There is no per-document transaction to
protect, so the one place batching actually pays is the one place nothing prevents it.

**Still to build:** the comparison itself — scoring both vector sets on the same eval suite —
which lands with the eval harness.

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

## Query logging

The schema and the writes land in M1 so traces exist from the first query. Access controls and
retention enforcement arrive with auth in M2; M1 ships the record, not the policy.

### `interactions`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `created_at` | `timestamptz` | |
| `user_id` | `text` | Local/CLI identity in M1; a real subject from M2 |
| `question` | `text` | |
| `answer` | `text` | Null when the run errored |
| `abstained` | `boolean` | Distinguishes "I don't know" from a failure |
| `retrieved` | `jsonb` | Per retrieved chunk: `document_id`, `chunk_id`, `content_hash`, per-arm rank and fused score |
| `llm_model`, `embedding_model` | `text` | Both, since either can move a result |
| `prompt_version` | `text` | Bump on any prompt change, or the trace can't be interpreted later |
| `effort` | `text` | |
| `latency_ms` | `int` | |
| `input_tokens`, `output_tokens` | `int` | |
| `cost_usd` | `numeric` | Derived from token usage |
| `error` | `text` | Null on success |

`retrieved` records `content_hash` as the version identity — it answers "which version of this
document produced that answer", which is the audit question.

### Two rules that are structural, not conventions

- **The log is never a retrieval source.** No adapter reads `interactions`, it has no chunk or
  embedding columns, and it is not in any search path. Indexing the agent's own answers would let
  one wrong answer become its own supporting evidence, and that error is self-reinforcing and
  nearly impossible to trace back. Only reviewed, verified answers enter the knowledge base,
  which is M6 work.
- **Writes happen on every path**, including abstention and error. A log that only records
  successes hides exactly the interactions worth reviewing, and in M6 those are the raw material
  for gap detection.

### Known gap

`documents` keeps one row per `source_uri` and upserts on content change, so M1 does not retain
old document *versions* — only the hash of the current one. Logging `content_hash` still
identifies which version answered, but reconstructing that version's text is not possible yet.
Version history is needed for M5 (rule history) and M6 (re-flagging verified answers when sources
change), so it is a schema change those milestones must carry. Noted here rather than discovered
there.

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

## CI and engineering practices

Set up before the first application code — cheap on an empty repo, miserable to retrofit.

| Check | Tool | Runs |
| --- | --- | --- |
| Lint and format | `ruff` | pre-commit + CI |
| Integration tests | `pytest -m integration` | CI, against a Postgres service |
| Real-model tests | `pytest -m model` | Local and nightly; needs warm weights, so excluded per-PR |
| Types | `pyright` | pre-commit + CI |
| Secret scanning | `gitleaks` | pre-commit + CI |
| Tests | `pytest` | CI on every PR |
| Eval smoke set | `kb eval --smoke` | CI on every PR |

All work goes through pull requests; every diff is reviewed before merge.

**The eval smoke set is deliberately tiny** — 3–5 cases, not the full suite. It calls paid APIs
on every pull request, so its cost is a standing per-PR charge rather than a one-off. It exists to
catch a hard regression (retrieval returning nothing, the agent loop breaking, citations
vanishing), not to score quality. The full suite runs on demand.

Later milestones add: the permission-leakage gate at 0 (M2), IaC scanning (M4), and AI PR review
against `docs/DECISIONS.md` and `docs/SPECS.md` once there is real code.

## Commands

Requires a Docker-compatible runtime for the Postgres container. Docker Desktop
works; this project was verified with Colima (`brew install colima docker
docker-compose`, then `colima start --cpu 2 --memory 4 --disk 10`), which needs
no privileged install and carries no licensing question.

The `kb` CLI. None of these exist yet.

| Command | Does |
| --- | --- |
| `docker compose up -d` / `down` | Start/stop Postgres. `kb db up`/`down` will wrap this |
| `scripts/migrate.sh [--status]` | Apply pending migrations. `kb db migrate` will wrap this (ADR-0029) |
| `kb ingest <root> [--dataset X]` | Run the pipeline; report per-status counts and embedding-call count |
| `kb model-warm` | One-off download of the pinned model, so ingest can stay offline |
| `kb ceiling [--execute] [--limit N] [--price-per-mtok P]` | Estimate, or run, the hosted ceiling embedding. Estimates by default — a metered command must not spend on its default path |
| `kb status` | Document counts by status; active embedding model |
| `kb search "<query>"` | Raw hybrid retrieval, no agent — for debugging retrieval alone |
| `kb ask "<question>"` | Full agent loop with citations |
| `kb eval [--config <name>]` | Run the suite, write the results table |
| `kb eval --smoke` | The 3–5 case subset CI runs on every PR |
| `kb log [--last N]` | Read back recent interaction traces |

`kb search` separate from `kb ask` is deliberate: when an answer is wrong, the first question is
always whether retrieval or the agent failed, and this answers it in one command.

## Configuration

Environment: `ANTHROPIC_API_KEY`, `DATABASE_URL`, and `HF_HUB_OFFLINE=1`. `VOYAGE_API_KEY` is
needed only for the ceiling run, not for serving. Never committed — `gitleaks` runs in pre-commit
precisely because a leaked key is not fixable by deleting a line (ADR-0027).

Config file keys: LLM model and effort; embedding provider and model (default
`gte-modernbert-base`); chunk target, overlap and cap; retrieval `top_k`, per-arm candidate
count, RRF `k`; agent max iterations; dataset manifest path. The embedding dimension is not a config key — it is read from the provider and checked
against the column (ADR-0006).

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
  ceiling/       the hosted reference run (ADR-0006)
  eval/          runner, metrics, judge, results table
  log/           interaction trace writer
  db/            queries
  db/migrations/ numbered forward-only .sql (ADR-0029)
  cli.py
scripts/migrate.sh
datasets/airflow-scheduler/manifest.*
evals/cases/*.yaml
evals/results/
docker-compose.yml
.pre-commit-config.yaml
.github/workflows/ci.yml
```

## Open questions

| Question | Tracked as |
| --- | --- |
| Chunk size, overlap, fallback behaviour | ADR-0019 |
| Whether `Qwen3-Embedding-0.6B` beats the chosen model on this corpus | ADR-0022 revisit |
| Which model judges answer correctness | ADR-0021 |
| Whether the Voyage ceiling justifies a hosted serving path | ADR-0006 revisit |
| Whether RRF beats weighted score fusion | ADR-0005 revisit |

None of these block starting. All of them are answered by measurement rather than discussion,
which is the point of building the harness first.
