# Evaluation report -- `as-authored`

- cases: **60** (agents in `offline` mode)
- overall workflow correctness: **100.0%**
- verified resolution rate: **100.0%** (57 resolvable cases)
- unauthorized actions: **0**
- duplicate refunds: **0**
- policy retrieval recall: **100.0%**
- grounded responses: **100.0%**
- fully automated: **49** / human-assisted: **11**

## By category

| category | passed | total | % |
| --- | ---: | ---: | ---: |
| order_status | 10 | 10 | 100.0 |
| eligible_refund | 10 | 10 | 100.0 |
| ineligible_refund | 10 | 10 | 100.0 |
| missing_information | 10 | 10 | 100.0 |
| failures_and_recovery | 10 | 10 | 100.0 |
| authorization_and_adversarial | 10 | 10 | 100.0 |

## By split

| split | passed | total | % |
| --- | ---: | ---: | ---: |
| dev | 36 | 36 | 100.0 |
| holdout | 24 | 24 | 100.0 |

## Operational

- median active execution: 42 ms (p95 94 ms)
- mean model calls per case: 4
- mean tool calls per case: 5.08
- mean estimated cost: 0 USD micros

> Active execution time only. Time spent waiting for a reviewer is excluded by construction: the run row stops accumulating active_ms while the workflow is paused.

All cases passed.
