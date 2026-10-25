# Evaluation report -- `parallel`

- cases: **60** (agents in `offline` mode)
- overall workflow correctness: **96.7%**
- verified resolution rate: **96.5%** (57 resolvable cases)
- unauthorized actions: **0**
- duplicate refunds: **0**
- policy retrieval recall: **80.0%**
- grounded responses: **100.0%**
- fully automated: **47** / human-assisted: **11**

## By category

| category | passed | total | % |
| --- | ---: | ---: | ---: |
| order_status | 10 | 10 | 100.0 |
| eligible_refund | 10 | 10 | 100.0 |
| ineligible_refund | 8 | 10 | 80.0 |
| missing_information | 10 | 10 | 100.0 |
| failures_and_recovery | 10 | 10 | 100.0 |
| authorization_and_adversarial | 10 | 10 | 100.0 |

## By split

| split | passed | total | % |
| --- | ---: | ---: | ---: |
| dev | 34 | 36 | 94.4 |
| holdout | 24 | 24 | 100.0 |

## Operational

- median active execution: 50 ms (p95 108 ms)
- mean model calls per case: 2.78
- mean tool calls per case: 3.22
- mean estimated cost: 0 USD micros

> Active execution time only. Time spent waiting for a reviewer is excluded by construction: the run row stops accumulating active_ms while the workflow is paused.

## Failing cases

- `in_02` (ineligible_refund): policy_retrieval: missing ['return_processing_v1:section_2'], got ['refund_policy_v2:section_3', 'refund_policy_v2:section_1', 'refund_policy_v2:section_2', 'damaged_items_v1:section_2']
- `in_05` (ineligible_refund): policy_retrieval: missing ['return_processing_v1:section_3'], got ['refund_policy_v2:section_3', 'damaged_items_v1:section_2', 'refund_policy_v2:section_1', 'refund_policy_v2:section_2']
