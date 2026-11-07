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

## Key design choices

Build a general engine that works on any knowledge base, and treat each dataset as a swappable test case.

| Decision | Choice | Why |
| --- | --- | --- |
| Knowledge approach | RAG (index and retrieve), not training | Swapping companies means re-indexing, not retraining. Fine-tuning teaches behavior, not facts. |
| Structure | Engine + source adapters + datasets | Engine stays general; adapters handle email, code, transcripts and docs differently. |
| Dataset 1 | One slice of a real open-source project (e.g. an Apache project: code, mailing lists, tickets, design docs) | Real messiness. All sources come from the same people and decisions, so synthesis makes sense. |
| Dataset 2 | Synthetic company built from a ground-truth file | Mirrors your company's contradictory business rules, with a perfect answer key for evals. |
| Model | Frontier API first (Claude), behind a provider interface | Debug the agent, not the model's limits. |
| Open models | Added later, compared on the same evals | Privacy story, cost, and learning quantization, serving and memory limits. |
| Real work data | Not used without company permission | Build on public data, then pitch it internally. |

**Synthetic company recipe:** write a YAML/JSON ground truth (people, rules, rule history, e.g. "refund window 30 → 14 days in Mar 2024, EU stays at 30"). Generate emails, transcripts, outdated wiki pages and code from it, add noise, then derive test questions straight from the ground truth. Start with about 10 rules, 5 people and a few dozen documents.

## Architecture

Four parts do the work: async ingestion, permission-filtered retrieval, a hand-written agent loop, and auth tying a user's identity to what they can see.

### Async ingestion

- Raw files land in storage; an event puts a job on a queue; workers parse, chunk, embed and index.
- **Idempotent:** a content hash skips unchanged documents, so re-runs are safe.
- **Reliable:** retries with backoff and a dead-letter queue for documents that keep failing.
- **Incremental:** only new or changed documents get re-processed. Old versions are kept, which powers "what changed and when."
- **Status tracking:** each document is queued, processing, indexed or failed, visible through the API.

### Retrieval

- **Source-aware chunking:** code by function or class, email by thread, transcripts by speaker turn, docs by heading.
- **Metadata on every chunk:** source, author, date, version and access list.
- **Hybrid search:** keyword (BM25) plus vector search, then a reranker.

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
- **Prompt-injection defense:** document text is treated as data, never instructions. Tools are read-only.

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

## Open-model phase

Once the agent works on a frontier API, swap in open models through the same provider interface and measure the gap. The target result is a line like "the local 8B model reaches X% of Claude's accuracy at zero marginal cost, with fully private data."

- **Local on the M1:** Ollama and Apple's MLX. Small models (3–4B on 8GB RAM, 7–8B at 4-bit on 16GB).
- **Quantization:** compare 4-bit and 8-bit on quality, speed and memory using the eval suite.
- **Memory limits:** context length versus RAM, KV cache size, why big models don't fit.
- **Serving:** run vLLM on a rented GPU (RunPod, Modal, or an EC2 GPU instance) and measure throughput and latency under concurrent load.
- **Hosted open models:** Amazon Bedrock or OpenRouter for larger open models without managing hardware.
- **Optional fine-tuning:** a LoRA run to improve tool-call formatting or company terminology, scored against the base model.

Rented GPUs and hosted APIs are pay-as-you-go. Check current prices before starting, and skip subscriptions.

## Milestones

Each phase ends with something working and an eval score, so you can stop at any point and still have a solid project.

1. **Local MVP (first weekend):** ingest one subsystem of a real open-source project. Hand-written agent loop on the Claude API. Cited answers. 10–15 hand-written eval questions.
2. **Auth and permissions:** users and groups, access lists on chunks, filtered retrieval, leakage tests in the eval suite.
3. **AWS + IaC:** move storage, the queue, workers, the database and the API to AWS, all defined in code. CI/CD deploys.
4. **Synthetic company:** ground-truth file, artifact generator, auto-generated evals for conflicts and outdated docs.
5. **Hard features:** conflict detection, expert finding, rule history over time, prompt-injection tests.
6. **Open models:** local and served comparisons, the quantization study, the results table.
7. **Stretch:** MCP servers as connectors (Google Drive, Notion, Slack), multi-tenant isolation, a simple web UI or Slack bot, LoRA fine-tuning.

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
- [ ] Synthetic benchmark dataset with ground truth
- [ ] Open-model serving, quantization and memory tradeoffs, measured
- [ ] Prompt-injection defenses
- [ ] Stretch: MCP, multi-tenancy, LoRA fine-tuning

**Presentation:** clean README with an architecture diagram, a 60-second demo GIF and the eval results table. Resume bullets lead with numbers, e.g. "raised cross-source answer accuracy from 52% to 81% with hybrid search and reranking."
