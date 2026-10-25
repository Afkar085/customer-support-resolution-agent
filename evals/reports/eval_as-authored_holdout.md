# Evaluation report -- `as-authored`

- cases: **24** (agents in `offline` mode)
- overall workflow correctness: **100.0%**
- verified resolution rate: **100.0%** (24 resolvable cases)
- unauthorized actions: **0**
- duplicate refunds: **0**
- policy retrieval recall: **100.0%**
- grounded responses: **100.0%**
- fully automated: **20** / human-assisted: **4**

## By category

| category | passed | total | % |
| --- | ---: | ---: | ---: |
| order_status | 5 | 5 | 100.0 |
| eligible_refund | 4 | 4 | 100.0 |
| ineligible_refund | 4 | 4 | 100.0 |
| missing_information | 4 | 4 | 100.0 |
| failures_and_recovery | 3 | 3 | 100.0 |
| authorization_and_adversarial | 4 | 4 | 100.0 |

## By split

| split | passed | total | % |
| --- | ---: | ---: | ---: |
| holdout | 24 | 24 | 100.0 |

## Operational

- median active execution: 54 ms (p95 112 ms)
- mean model calls per case: 4
- mean tool calls per case: 4.33
- mean estimated cost: 0 USD micros

> Active execution time only. Time spent waiting for a reviewer is excluded by construction: the run row stops accumulating active_ms while the workflow is paused.

All cases passed.
