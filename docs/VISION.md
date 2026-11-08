# Company Knowledge Agent — Project Brief

Nov 7, 2026 · @Nicholas Middelberg

## Concept

A permission-aware agent that answers questions about a company by searching its legacy docs, code, email and meeting transcripts, then citing exactly where each piece of the answer came from.

**The problem.** At small and mid-sized companies, answers are scattered across a code comment, a 2022 email thread and someone's meeting notes. Finding one is a game of telephone, and business rules often contradict each other.

**What the agent does:**

- **Cross-source answers:** stitches together evidence from several sources instead of hunting for one perfect document.
- **Provenance:** every claim cites its source, e.g. "per Dana's email (Mar 2023) and `billing/legacy.py` line 140."
- **Conflict detection:** flags when sources disagree and prefers the newer, more authoritative one.
- **Expert finding:** when it can't answer, it names who to ask, based on who wrote the relevant emails, code and notes.
- **Abstention:** says "I don't know" instead of guessing.
- **Access control:** users only get answers drawn from documents they're allowed to see.
- **Governed improvement:** learns from corrections an expert has reviewed and approved, never from its own answers.

## Key design choices

Build a general engine that works on any knowledge base, and treat each dataset as a swappable test case.

| Decision | Choice | Why |
| --- | --- | --- |
| Knowledge approach | RAG (index and retrieve), not training | Swapping companies means re-indexing, not retraining. Fine-tuning teaches behavior, not facts. |
| Structure | Engine + source adapters + datasets | Engine stays general; adapters handle email, code, transcripts and docs differently. |
| Dataset 1 | Apache Airflow, scoped to one subsystem (the scheduler and DAG-run behavior): code, dev mailing list, Airflow Improvement Proposals, GitHub issues and PRs | Real messiness, and all four source types present. Python codebase with rule-like behavior that genuinely changed across versions (`execution_date` → `logical_date`), which makes eval questions easy to write and conflicts real rather than staged. |
| Dataset 2 | Synthetic pharma-flavored company — a contract manufacturer — built from a ground-truth YAML file | Contradictory rules with a perfect answer key: batch release criteria, deviation and CAPA handling, SOP revisions under change control, region-specific labeling (FDA vs EMA), client-specific exceptions. Its SOPs cite real public regulatory guidance, so the agent has to reconcile internal rules with external regulation. |
| Storage | Postgres with pgvector — local Docker Compose first, the same database on RDS later | One store for chunks, embeddings and metadata, and keyword search comes with it. The schema and queries move to AWS unchanged. |
| Embeddings | Local model on the serving path, with a hosted reference run to establish the quality ceiling | Privacy and zero marginal cost where it counts, plus a known upper bound so a weak retrieval score is attributable rather than mysterious. The vector dimension follows the model, so the model is chosen before the first migration. |
| Model | Frontier API first (Claude), behind a provider interface | Debug the agent, not the model's limits. |
| Open models | Added later, compared on the same evals | Privacy story, cost, and learning quantization, serving and memory limits. |
| Real work data | Never used — no real company or client data | Public and synthetic sources only: not in the index, not in an eval case, not in a screenshot. Build on public data, then pitch it internally. |

**Synthetic company recipe:** write a YAML/JSON ground truth — people, rules, and rule history, e.g. "SOP-114 raises the dissolution spec from 80% to 85% in Mar 2024; EMA-market batches stay at 80%; one client is exempted under its own agreement." Generate emails, meeting transcripts, superseded SOP revisions and code from it, add noise, then derive test questions straight from the ground truth. Start with about 10 rules, 5 people and a few dozen documents.

The SOPs cite **real public regulatory guidance** — FDA guidance documents, ICH quality guidelines — which is ingested as its own source. That adds the case the internal-only corpus can't produce: an internal rule that has to be read against an external regulation, where the regulation is authoritative but the internal SOP is more specific.

## Architecture

Four parts do the work — async ingestion, permission-filtered retrieval, a hand-written agent loop, and auth tying a user's identity to what they can see — all over a single Postgres store.

### Async ingestion

- Raw files land in storage; an event puts a job on a queue; workers parse, chunk, embed and index.
- **Idempotent:** a content hash skips unchanged documents, so re-runs are safe.
- **Reliable:** retries with backoff and a dead-letter queue for documents that keep failing.
- **Incremental:** only new or changed documents get re-processed. Old versions are kept, which powers "what changed and when."
- **Status tracking:** each document is queued, processing, indexed or failed, visible through the API.

### Storage and embeddings

- **One store:** Postgres with pgvector holds chunks, embeddings, metadata and access lists. Local via Docker Compose, the same database on RDS later, so nothing is rewritten on the way to AWS.
- **Keyword search is Postgres full-text search,** so hybrid retrieval needs no second search system.
- **Embeddings come from a local model.** The vector column is fixed-width, so its dimension follows whichever model is configured — which means the model is picked before the first migration, not after.
- **A hosted reference run sets the ceiling.** The same corpus, the same evals, a hosted embedding model, scored once. Without it, a weak retrieval score has two suspects — the embedding model and the chunking — and no way to tell them apart.

### Retrieval

- **Source-aware chunking:** code by function or class, email by thread, transcripts by speaker turn, docs by heading.
- **Metadata on every chunk:** source, author, date, version and access list.
- **Hybrid search:** keyword (Postgres full-text search) plus vector search, then a reranker.

### Document-level authorization at retrieval time

- Each chunk carries its document's access list (allowed users and groups).
- The user's groups come from their verified login token, never from the prompt.
- The permission filter runs **inside the search query**, so forbidden chunks never reach the model. The LLM never decides permissions.
- Defense in depth: Postgres row-level security, so even a buggy query can't return forbidden rows.
- Permission changes propagate on re-sync. Leakage is tested in the eval harness and must stay at 0.

### Agent

- Hand-written loop first (model → tool call → result → model), frameworks later if ever.
- Tools: `search`, `read_document`, `grep_code`, `git_log`, `who_knows`.
- Outputs: answer with citations, conflicts flagged, or an abstention plus a suggested expert.
- **MCP server:** the same tools are exposed over MCP, so any MCP client — Claude Desktop, for instance — can query the knowledge base without a bespoke integration.
- **Prompt-injection defense:** document text is treated as data, never instructions. Tools are read-only.

## Query logging and audit trail

Every interaction is logged as a full trace: the user, the question, the retrieved document IDs and versions, the model, the prompt version, the answer, latency and cost. The schema lands in Milestone 1, so traces exist from the very first query rather than being retrofitted.

- **It doubles as an audit log.** In regulated industries that is the difference between a tool and a usable tool — 21 CFR Part 11 sets audit-trail expectations for electronic records, and "who was told what, and on what basis" is exactly what the trace answers.
- **The log has its own access controls and retention policy.** Being allowed to use the agent does not mean being allowed to read everyone's questions.
- **Logs are never retrieved as knowledge.** Only reviewed, verified answers enter the knowledge base. Indexing the agent's own output would let a single wrong answer become its own supporting evidence, and the error would then be self-reinforcing and very hard to find.

## AWS and infrastructure as code

Deploy on about ten AWS services, all defined in Terraform or CDK, so the whole stack can be created or destroyed with one command.

&#91;embedded content: AWS architecture · ingestion and query paths\]

Documents flow through the queue into Postgres. Every query is authenticated, then filtered by the user's groups inside the database search.

| Service | Role |
| --- | --- |
| S3 | Raw document storage and versions; upload events start ingestion |
| SQS | Ingestion job queue with a dead-letter queue |
| ECS Fargate or Lambda | Ingestion workers and the agent API |
| RDS Postgres + pgvector | Chunks, embeddings, access lists, row-level security |
| Cognito | Login, users and groups; issues the JWT |
| API Gateway | Public API that verifies the JWT |
| Bedrock (optional) | Claude and open models hosted inside AWS |
| Secrets Manager | API keys and database credentials |
| CloudWatch + X-Ray | Logs, metrics, traces of each agent step |
| Terraform or CDK | Infrastructure as code for all of the above |
| GitHub Actions (OIDC) | CI/CD, eval smoke tests, deploys with no long-lived AWS keys |

**Cost guardrails:** set an AWS Budgets alert before anything else. Use the smallest database instance, and avoid always-on extras early (NAT Gateways, OpenSearch Serverless), which carry fixed monthly costs. Tear the stack down when you're not using it.

## Eval harness

The eval harness is what turns this from a demo into engineering: every change to the model, prompts, chunking or retrieval gets a score.

**Test cases** are stored as data: question, asking user, gold answer, gold source documents, and a type (lookup, cross-source, conflict, outdated doc, unanswerable, forbidden). Real-project questions are written by hand (start with 10–15). Synthetic-company questions are generated from the ground-truth file.

| Metric | What it checks |
| --- | --- |
| Retrieval recall@k | Did the right documents come back? |
| Answer correctness | LLM-as-judge plus exact checks where possible |
| Citation accuracy | Do cited sources actually support the claim? |
| Conflict handling | Were contradictions flagged and the right version chosen? |
| Abstention | Does it say "I don't know" on unanswerable questions? |
| Permission leakage | Forbidden content in an answer. Must be 0. |
| Latency and cost | Seconds and dollars per query |

**How it runs:** one command runs the suite against any config (model, chunking, retriever) and writes a results table. A small smoke set runs in CI on every pull request, so regressions fail the build. The results table goes in the README.

## Governed feedback loop

A knowledge system that improves through governed human feedback. The review and approval flow deliberately mirrors pharma change control — which is also why Dataset 2 is pharma-flavored.

**Corrections.** A user explicitly corrects an answer. That becomes a candidate entry carrying its provenance, and it enters an SME review queue to be approved, edited or rejected. Approved, it is stored as a new source type — a **verified answer** — with author, approver, date, access list and review-by date.

**Verified answers are governed, not just stored.**

- They inherit the strictest access list of their source documents, so a correction can never widen who can see something.
- They link to the specific source document versions they were derived from, and get flagged for re-review when those sources change.
- A conflict with a newer document is surfaced, never silently resolved.

**Gap detection.** Flag low-confidence interactions using *observable* signals — abstention, low retrieval scores, unresolved conflicts, a thumbs-down, the same question rephrased repeatedly. Not the model's self-reported confidence, which is a fluent guess rather than evidence. Cluster similar questions by embedding, rank the clusters by frequency, and route each to a likely expert via expert finding.

**Every approved correction or filled gap automatically becomes an eval case,** so the system's improvement is measurable instead of asserted. This is the loop that makes the eval suite grow with use rather than only with hand-written work.

## Open-model phase

Once the agent works on a frontier API, swap in open models through the same provider interface and measure the gap. The target result is a line like "the local 8B model reaches X% of Claude's accuracy at zero marginal cost, with fully private data."

- **Local on the M1:** Ollama and Apple's MLX. Small models (3–4B on 8GB RAM, 7–8B at 4-bit on 16GB).
- **Quantization:** compare 4-bit and 8-bit on quality, speed and memory using the eval suite.
- **Memory limits:** context length versus RAM, KV cache size, why big models don't fit.
- **Serving:** run vLLM on a rented GPU (RunPod, Modal, or an EC2 GPU instance) and measure throughput and latency under concurrent load.
- **Hosted open models:** Amazon Bedrock or OpenRouter for larger open models without managing hardware.
- **Optional, eval-driven fine-tuning:** a LoRA run only to fix a specific failure the evals actually reveal — a small open model fumbling tool-call formats, say — with before and after scores reported. RAG handles knowledge; fine-tuning is never used to teach facts.

Rented GPUs and hosted APIs are pay-as-you-go. Check current prices before starting, and skip subscriptions.

## Engineering practices

Docs first, then code: VISION, DECISIONS, a per-milestone SPECS, and CLAUDE.md. A decision made in conversation is written down before it is implemented.

- **Before the first line of application code:** pre-commit (ruff, pyright), gitleaks, and pytest running in GitHub Actions. Cheap to add on an empty repo, miserable to retrofit onto a full one.
- **Added as each piece appears:** eval smoke tests (M1), a permission-leakage gate that must stay at 0 (M2), IaC scanning (M4), and AI PR review against DECISIONS.md and SPECS.md once there is real code to review.
- **All work goes through pull requests,** and every diff is reviewed before merging.

## Milestones

Each phase ends with something working and an eval score, so you can stop at any point and still have a solid project.

1. **Local MVP (first weekend):** ingest one Airflow subsystem. Postgres and pgvector in Docker, local embeddings, a hand-written agent loop on the Claude API, cited answers, the query-log schema, 10–15 hand-written eval questions, and CI basics.
2. **Auth and permissions:** users and groups, access lists on chunks, filtering inside the search query, row-level security, and leakage evals that must stay at 0.
3. **Synthetic pharma company:** ground-truth file, artifact generator with noise, auto-generated evals for conflicts and outdated docs.
4. **AWS + IaC:** storage, the queue, workers, the database and the API on AWS via Terraform or CDK. CI/CD deploys and IaC scanning.
5. **Hard features:** conflict detection, expert finding, rule history over time, prompt-injection tests, and the MCP server.
6. **Governed feedback loop:** corrections, verified answers, the review queue, a gap-detection dashboard, and auto-generated eval cases.
7. **Open models:** local and served comparisons, the quantization study, and optional eval-driven LoRA.
8. **Stretch:** MCP connectors for external sources (Google Drive, Notion, Slack), multi-tenant isolation, and a simple web UI or Slack bot.

## Resume skills checklist

Tick these off as you go. Each one is a concrete talking point for interviews.

- [ ] Agent loop and tool calling, written by hand
- [ ] RAG: source-aware chunking, hybrid search, reranking
- [ ] Document-level authorization enforced at retrieval time, with 0 leakage proven by tests
- [ ] Authentication with users and groups (Cognito, JWT)
- [ ] Async ingestion pipeline: queues, idempotency, retries, dead-letter queue
- [ ] Eval harness with metrics, a results table and CI regression checks
- [ ] Multi-service AWS deployment
- [ ] Infrastructure as code (Terraform or CDK)
- [ ] CI/CD with GitHub Actions
- [ ] Observability: agent traces, logs, cost per query
- [ ] Audit logging: full query traces as an audit trail, with access controls and a retention policy
- [ ] Synthetic benchmark dataset with ground truth
- [ ] Governed feedback loop: SME review queue, verified answers under change control
- [ ] Gap detection: clustering low-confidence interactions and routing them to experts
- [ ] MCP server exposing the agent's tools to any MCP client
- [ ] Open-model serving, quantization and memory tradeoffs, measured
- [ ] Eval-driven fine-tuning (LoRA), with before and after scores
- [ ] Prompt-injection defenses
- [ ] Stretch: MCP connectors for external sources, multi-tenancy

**Presentation:** clean README with an architecture diagram, a 60-second demo GIF and the eval results table. Resume bullets lead with numbers, e.g. "raised cross-source answer accuracy from 52% to 81% with hybrid search and reranking."
