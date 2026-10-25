# Evaluation

## What is being measured

Response text is easy to grade and almost worthless on its own. A reply that
says "your refund is on its way" is correct only if a refund operation exists,
is bound to a decided approval, and the provider agrees. So every case is
judged against **system state**:

- rows in `refund_operations` for the order, and their statuses;
- records in the mock provider's own ledger;
- whether each executed refund is bound to an approval that a reviewer decided;
- the ticket's final status;
- the policy citations the run actually produced.

Response text is checked only for specific falsifiable claims: an amount, a
tracking number, the word "completed" when nothing has settled.

An LLM judge is not used to decide whether money moved. It could reasonably
assist with tone or helpfulness; it has no business grading a ledger.

## The dataset

60 synthetic cases in `evals/datasets/cases.json`, authored in
`evals/datasets/build_dataset.py`:

| Category | Cases |
| --- | ---: |
| Order status and shipment questions | 10 |
| Eligible refund requests | 10 |
| Ineligible refund requests | 10 |
| Missing information and ambiguous complaints | 10 |
| API failures, retries and crashes | 10 |
| Authorization, concurrency and adversarial inputs | 10 |

Each case stores the customer, the order, the applicable policy sections, the
allowed and forbidden actions, whether approval is required, the expected final
business state, and the expected escalation or clarification behaviour.

Cases run in one of two modes. `workflow` cases go through the HTTP API end to
end. `probe` cases need more than a linear request sequence — concurrent
execution, a replayed approval, a tampered payload hash, a crash rewind — and
are expressed as named probes in `evals/run_evals.py`.

Each case runs against a freshly seeded database with the mock provider's fault
switch cleared, so no case can be influenced by the one before it.

## Splits, and an honest note about the held-out set

36 cases are `dev`, 24 are `holdout`. The holdout cases were written before any
tuning and were not looked at while rules, prompts or retrieval were being
adjusted.

**The holdout was run three times, not once.** The first blind run scored
**91.7%** and surfaced two genuine defects:

1. An `unclear` complaint that happened to have an order attached fell through
   to the order-status branch and was answered as though it were a tracking
   question. It should ask a question.
2. Classification missed two ordinary phrasings — naming the inspection when
   chasing a refund ("inspection passed, I'd like the 2499 back"), and "it
   broke" for a damaged item.

Both were fixed as behaviour, not as test expectations. A third run surfaced a
third phrasing gap ("when will my headphones get here") and was fixed the same
way. After those fixes the holdout scores 100%.

That number is therefore **not a blind result**, and should not be quoted as
one. The blind number is 91.7%. If you want a genuinely blind figure from this
project, write new cases and run them once.

## Metrics

| Metric | Definition |
| --- | --- |
| Verified resolution rate | cases whose expected business outcome is confirmed in the database ÷ cases marked resolvable |
| Overall workflow correctness | cases handled exactly as expected — including correct refusal, clarification and escalation — ÷ all cases |
| Unauthorized action count | refunds executed where the case forbids one, or without a decided approval |
| Duplicate refund count | provider records beyond the expected one, under retries, restarts and concurrency |
| Policy retrieval recall | cases where the required policy section appears in the citations produced |
| Grounded response rate | no completion claim without a settled operation; no citation outside the retrieved set |
| Operational | active execution ms, model calls, tool calls, estimated cost per case |

Latency is **active execution time only**. Time spent waiting for a reviewer is
excluded by construction: `workflow_runs.active_ms` only accumulates while the
graph is actually running, and stops while it is paused on `interrupt()`.

Automated and human-assisted resolutions are reported separately. Every
successful refund in this system is human-assisted by design, so a headline
"resolution rate" that hid approvals would be misleading.

## Results

Run `make evals` to reproduce. Reports are written to `evals/reports/`.

See the checked-in reports for the current numbers:

- `evals/reports/eval_specialists_all.md` — all 60 cases
- `evals/reports/eval_as-authored_holdout.md` — held-out subset
- `evals/reports/variant_comparison.md` — the three graph variants

With the deterministic reasoner the suite is reproducible: the same input
produces the same output on any machine, which is why the numbers can be
checked rather than taken on trust.

## The multi-agent comparison

Three graph shapes, same nodes, same validation, same execution path:

| variant | what differs |
| --- | --- |
| `specialists` | investigation first, then policy retrieval with a query expanded by the established order facts; three narrow toolsets |
| `parallel` | both specialists in one superstep; policy retrieval has no facts to use |
| `baseline` | one generalist node holding every read tool; policy query from the customer's words alone |

The measured difference is retrieval recall, and it propagates into
correctness: both the parallel and single-agent variants fail the same two
cases, and for the same reason. Asked "I sent the earbuds back and they
arrived, where is my refund?", a query built from the customer's words alone
retrieves the general refund sections. Only a query that also knows
*the return is received and inspection is pending* retrieves
`return_processing_v1:section_2`, the section that actually governs the answer.

That is the whole argument for the decomposition, and it is worth being precise
about what it is **not**: in offline mode all three variants run the same number
of reasoning steps, so this is a quality result, not a cost result. A true
single-prompt tool-use agent would be the next variant to add, and would be the
honest way to measure the cost side.

## Reproducing

```bash
make evals                 # all 60 cases, specialists variant
make evals-holdout         # held-out subset only
make compare               # all three variants, writes variant_comparison.md

python -m evals.run_evals --category failures_and_recovery
python -m evals.run_evals --case fr_01          # one case, verbose
```

The harness uses its own temporary SQLite database and never touches
`support.db`, so it is safe to run while the dashboard is open.
