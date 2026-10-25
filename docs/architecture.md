# Architecture

## The one decision everything follows from

A language model **proposes**. The application **decides and executes**.

Every design choice below is downstream of that. If you only read one diagram,
read this one:

```
customer message ──► [ agents ] ──► proposal (validated schema)
                                         │
                                         ▼
                            ┌────────────────────────┐
                            │  ordinary Python code  │
                            │  ownership check       │
                            │  eligibility rules     │
                            │  amount arithmetic     │
                            │  approval validation   │
                            └────────────┬───────────┘
                                         ▼
                              reviewer approval row
                                         │
                                         ▼
                             idempotent refund execution
```

Nothing a model emits reaches a payment API. It reaches
`app/workflows/validation.py`, which recomputes the amount, re-checks
ownership and eligibility, and can overrule the proposed action entirely.

## Components

| Component | Lives in | Responsibility |
| --- | --- | --- |
| FastAPI app | `app/api`, `app/main.py` | HTTP surface, authn/authz, request and response validation |
| LangGraph workflow | `app/workflows` | orchestration, the approval pause, routing, recovery |
| Specialist agents | `app/agents` | classification, evidence summary, policy interpretation, proposal |
| Tools | `app/tools/registry.py` | narrow typed operations with structured failures |
| Domain services | `app/services` | ownership, eligibility, proposals, approvals, refunds, audit |
| Policy retrieval | `app/services/policy_store.py` | chunking, embedding, versioned retrieval, citations |
| Persistence | `app/db`, `migrations` | application tables + a separate checkpoint store |
| Mock externals | `mock_services/` | payment, carrier and ticketing APIs that can fail realistically |

## The workflow graph

```
authenticate ─► classify ─► investigate ─► retrieve_policy ─► propose ─► validate_rules
                                                                              │
          ┌───────────────────┬───────────────────┬──────────────────────────┘
          ▼                   ▼                   ▼                          ▼
     answer_only      request_information     escalate            request_approval
          │                   │                   │                          │
          │                   │                   │                   [ interrupt() ]
          │                   │                   │                          ▼
          │                   │                   │                     revalidate
          │                   │                   │                    ╱     │     ╲
          │                   │                   │             rejected  execute   escalate
          │                   │                   │                          ▼
          │                   │                   │                       verify
          │                   │                   │                          ▼
          │                   │                   │                    update_ticket
          └───────────────────┴───────────────────┴──────────────────────────┤
                                                                              ▼
                                                                          finalise
```

Three graph shapes are built from the same nodes (`app/workflows/graph.py`):

- **specialists** (default) — investigation runs first, then policy retrieval
  with a query expanded using the established order facts.
- **parallel** — both specialists in one LangGraph superstep. Faster in
  principle, but policy retrieval cannot see order facts that do not exist yet.
- **baseline** — one generalist node holding every read tool.

`evals/reports/variant_comparison.md` has the measured difference.

## Why some steps are a model and others are not

| Step | Implementation | Why |
| --- | --- | --- |
| Classification | model (offline: scored keywords) | language judgement, cheap, recoverable if wrong |
| Which order is meant | code + model | code narrows by ownership and status; the model handles ambiguous wording |
| Reading records | code | a fixed sequence of reads needs no reasoning |
| Eligibility | **code only** | money depends on it; it must be auditable and testable |
| Policy interpretation | model over retrieved passages | genuinely a reading-comprehension task |
| Amount | **code only** | arithmetic, and a wrong number is a real loss |
| Customer wording | model | the part a model is actually good at |
| Approval validation | **code only** | authorization |
| Execution and retries | **code only** | exactly-once semantics are not a prompting problem |

## Identity and access

`Principal` (`app/auth/principal.py`) is built from a bearer token by the
server and is the only source of customer identity. No tool in
`app/tools/registry.py` takes a `customer_id` parameter — there is nothing for
a model to override. `load_owned_order` is the single chokepoint for order
access, and it returns the **same** failure for a non-existent order and for
someone else's order, so responses cannot be used to enumerate order ids. The
real reason is written to the audit trail instead.

Toolsets are least-privilege per specialist:

| Toolset | Can do | Cannot do |
| --- | --- | --- |
| `investigation` | read orders, returns, payments, shipments, eligibility | propose, execute, write tickets |
| `policy` | search policy | everything else |
| `resolution` | eligibility, policy, create a proposal | execute a refund |
| `executor` | execute an approved refund, update a ticket, reconcile | nothing a customer-facing agent calls |

## Data model

Money is an integer in minor units everywhere. There is no float in the
refund path.

Key constraints doing real work:

- `refund_operations.idempotency_key` — unique. This is the durable guarantee
  against a duplicate refund, not a code path.
- `refund_operations.proposal_id` — unique. One proposal, one refund.
- `approvals.proposal_id` — unique. One approval per proposal.
- `action_proposals (run_id, payload_hash)` — unique. Re-running an
  interrupted node cannot create a second proposal for the same action.
- `CHECK amount_minor > 0`, `CHECK paid_amount_minor >= 0`, status enums on
  every lifecycle column.

**The LangGraph checkpoint is not the ledger.** Checkpoints live in their own
SQLite file (or their own Postgres tables) and can be replayed. Refund
accounting lives in application tables with constraints and transactions.

## Refund execution

```
reserve (short txn, order row locked)
   │  balance check: captured − committed − reserved
   ▼
COMMIT                      ◄── the row now says "about to move money with key K"
   │
   ▼
call provider with key K    ◄── no transaction open during the network call
   │
   ├── 2xx            ──► persist status
   ├── 503            ──► back to `reserved`; safe to retry with the same key
   ├── timeout        ──► `unknown`  (never `failed`, never a blind retry)
   └── payload clash  ──► `failed` + escalate
   │
   ▼
reconcile by key K          ◄── only the provider can say whether money moved
```

On Postgres the reservation holds the order row with `SELECT ... FOR UPDATE`.
SQLite has no row locks, so an in-process mutex narrows the window and the
unique index does the actual work; the concurrency test asserts the outcome,
not the mechanism.

`recover_unfinished()` runs at startup and from the dashboard: reconcile money
first, then resume workflows whose approval was decided while the process was
down.

## Retrieval

Policy documents are versioned markdown with front matter and
`## [section_n] Heading` sections. Ingestion chunks by section, embeds with a
deterministic local hashing embedding, and flags instruction-like text.

Retrieval is hybrid: 0.65 × cosine + 0.35 × lexical overlap, filtered to the
policy version in force at the time of the request, then cut by a relevance gap
(a hit must score at least 55% of the best hit) so a clear single-section
answer is not padded with loosely related sections.

Retrieved text is data. A passage containing instruction-like phrasing is
excluded from what the resolution agent sees and reported as untrusted content.
A similarity score is never evidence that a refund is valid.

### pgvector

Embeddings are stored as JSON so the project runs on plain Postgres or SQLite.
On Postgres with pgvector the upgrade is contained: change
`PolicyChunk.embedding` to a `Vector(512)` column, add an ivfflat index, and
replace the Python scoring loop in `policy_store.search` with an
`ORDER BY embedding <=> :query` clause. The hybrid lexical term stays in SQL as
a `ts_rank` component. Nothing above that function changes.

## What is deliberately out of scope

Chargebacks, fraud adjudication, partial line-item refunds, multiple stores,
multiple currencies, goodwill credits, and outbound customer email. The
escalation policy hands these to a human untouched.
