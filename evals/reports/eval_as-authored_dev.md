# Evaluation report -- `as-authored`

- cases: **36** (agents in `offline` mode)
- overall workflow correctness: **100.0%**
- verified resolution rate: **100.0%** (33 resolvable cases)
- unauthorized actions: **0**
- duplicate refunds: **0**
- policy retrieval recall: **100.0%**
- grounded responses: **100.0%**
- fully automated: **29** / human-assisted: **7**

## By category

| category | passed | total | % |
| --- | ---: | ---: | ---: |
| order_status | 5 | 5 | 100.0 |
| eligible_refund | 6 | 6 | 100.0 |
| ineligible_refund | 6 | 6 | 100.0 |
| missing_information | 6 | 6 | 100.0 |
| failures_and_recovery | 7 | 7 | 100.0 |
| authorization_and_adversarial | 6 | 6 | 100.0 |

## By split

| split | passed | total | % |
| --- | ---: | ---: | ---: |
| dev | 36 | 36 | 100.0 |

## Operational

- median active execution: 44 ms (p95 96 ms)
- mean model calls per case: 0.65
- mean tool calls per case: 3.35
- mean estimated cost: 0 USD micros

> Active execution time only. Time spent waiting for a reviewer is excluded by construction: the run row stops accumulating active_ms while the workflow is paused.

All cases passed.
