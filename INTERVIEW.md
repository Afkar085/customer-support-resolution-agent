# Interview prep

Read this once slowly. Then read `app/services/refunds.py` — the function
`execute_operation` — and `docs/architecture.md`. Those two plus this file are
enough to defend the whole project.

---

## 1 · Why you built it

> "I wanted to answer one question: how do you let a language model work inside
> a process that moves money, without it becoming a liability?
>
> The obvious approach is to give the model a refund tool and write a careful
> prompt. That fails the first time the model is confidently wrong. So I built
> the opposite — the model proposes, and the application decides and pays."

That's the whole pitch. Say it in that order: the question, why the obvious
answer fails, what you did instead.

---

## 2 · What it solves

A support agent handling *"I returned my headphones, where's my refund?"* has to:

1. Find the order
2. Check whether the return arrived
3. Check whether it passed inspection
4. Check what was already refunded
5. Find the relevant clause in the refund policy
6. Work out the amount
7. Write a reply

Steps 1–6 are **lookup**, not judgement. They take 10–15 minutes and a human
adds nothing. Step 7 needs care. The *decision* needs authority.

So: automate the lookup, keep the authority with a person, and make the
execution safe enough that a retry or a crash can't pay twice.

---

## 3 · How it helps

| Who | Before | After |
| --- | --- | --- |
| Customer | Two days for an answer | Seconds, with the policy quoted |
| Support agent | 15 minutes of lookup per ticket | 10 seconds to approve or reject |
| The business | Trust that the agent read the policy | An audit trail of who approved what |

And the guarantee underneath: **no refund is ever paid twice**, under retry,
restart, or two people clicking at once.

---

## 4 · Questions you will be asked

### "Walk me through what happens when a complaint comes in."

> It authenticates the customer from the bearer token — identity never comes
> from the request body or the model. It classifies the complaint, reads the
> order, return and payment, and retrieves the policy sections that apply using
> the established facts, not just the customer's words. A resolution agent
> produces a structured proposal. Then ordinary code validates it: ownership,
> eligibility, amount. If money should move, it writes a proposal and pauses on
> a LangGraph interrupt. A reviewer approves. On resume it re-validates, then
> executes once, verifies with the provider, and updates the ticket.

### "Why does the approval bind to a payload hash?"

> Because a reviewer approves a specific thing — ₹2,499 on this order — not a
> general intent. The hash covers the action, order, amount, currency and
> ticket. If any of those change, the old approval stops matching and the agent
> has to ask again. Otherwise you could get approval for ₹100 and execute
> ₹10,000.

### "Why is the idempotency key derived instead of random?"

> Because a random key is only safe if you manage to persist it. If the worker
> dies between generating the key and writing it down, a restart generates a
> new one and you pay twice. Mine is derived from the proposal id and payload
> hash, so the same proposal always produces the same key — a crash at any
> point recomputes it identically.

### "What happens if the provider takes the payment and the connection drops?"

> That's the case the whole execution path is designed around. The operation is
> marked `unknown` — never `failed`, and never retried blind. Reconciliation
> asks the provider about that idempotency key, because only the provider knows
> whether the money moved. You can watch it in the Reliability tab: arm
> "accept then timeout", run a refund, and the ledger still shows one refund on
> both sides.

### "Why is eligibility in code rather than in the prompt?"

> Because it has to be auditable and testable. A rule in a prompt can't be
> unit-tested, can't be diffed in review, and changes behaviour when you swap
> models. `evaluate_refund` is a pure function over database rows — I can test
> every branch. The model can propose a refund the rules reject, and the
> validator overrules it and logs that it did.

### "How do you handle prompt injection?"

> Retrieved policy text and customer messages are data, never instructions.
> Passages containing instruction-like phrasing are flagged at ingest and
> excluded from what the resolution agent sees. But the real defence isn't
> detection — it's that the model has no authority. Even if it were fully
> hijacked and emitted "refund ₹50,000 immediately, no approval needed", it
> still just produces a proposal, and the amount gets recomputed. There's an
> eval case for exactly that.

### "Why LangGraph rather than a loop?"

> The approval pause. I needed a workflow that could stop for an arbitrary
> length of time, survive a process restart, and resume in the same place.
> That's `interrupt()` plus a durable checkpointer. A plain loop would have
> meant building that myself. One thing it forced me to get right: the
> interrupted node re-runs from its start on resume, so everything before the
> interrupt has to be idempotent — and the money has to move in a *later* node.

### "How do you know it works?"

> 60 test cases across six categories, judged on database state rather than on
> how the reply reads — refund rows, provider records, approval bindings,
> ticket status. 100% workflow correctness, zero unauthorized actions, zero
> duplicate refunds. The held-out split scored 91.7% on its first blind run and
> surfaced two real defects, which I fixed and documented.

### "What was the hardest bug?"

> A wrong model name looked exactly like no API key configured. The client
> caught the transport error, fell back to the deterministic reasoner, and
> reported nothing — so everything "worked" while no model was being called.
> I'd built a silent fallback and not thought about how it fails. It now records
> the provider's actual error and logs a warning, and still falls back rather
> than failing the ticket. The lesson was that a graceful degradation you can't
> observe is just a hidden failure.

### "What's the weakest part?"

> It's a simulation. Synthetic customers, a mock payment provider, and 60 cases
> I designed myself — so 100% is a self-graded exam, not a production number.
> Technically, runs execute inside the HTTP request; the design is ready for a
> worker (durable run rows, checkpoints, a recovery sweep) but I didn't wire
> one up. And retrieval scores similarity in Python rather than using pgvector.

### "What would you do next?"

> A real worker queue, pgvector for retrieval, and partial line-item refunds —
> which I deliberately scoped out and said so in the README. I'd also want a
> true single-prompt agent as a fourth variant, because my current "baseline"
> still runs four reasoning steps, so the comparison measures accuracy and not
> cost.

### "Did you build this yourself?"

Answer this honestly. A prepared honest answer is stronger than a defensive one.

> I built it with heavy AI assistance — that's how I work, and it's how most of
> this gets built now. What I own is the design: the decision that the model
> proposes and the code decides, the approval binding to a payload hash, the
> derived idempotency key, judging the evals on database state. Ask me about
> any file and I'll walk you through why it's shaped that way.

Then **be able to do that**. That sentence is a promise.

---

## 5 · The one thing to memorise

> **The model proposes. The code decides and pays.**

Every other answer is a consequence of that line.
