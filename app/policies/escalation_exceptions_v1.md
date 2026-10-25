---
id: escalation_exceptions
title: Exceptions Requiring Human Review
version: v1
effective_from: 2025-06-01
source_uri: app/policies/escalation_exceptions_v1.md
topics: escalation, approval
---

## [section_1] Cases that must be referred to a reviewer

The following must be referred to a support reviewer rather than resolved by an
agent:

* inspection of a returned item has failed;
* the item is hazardous to ship and a return waiver is being considered;
* the shipment is lost and a refund without a return is being considered;
* the refund outcome reported by the payment provider is uncertain;
* no policy section can be established that applies to the request;
* two policy sections give conflicting answers for the same facts.

## [section_2] Cases that must be refused, not escalated

A request that is clearly outside policy with no discretion attached -- a return
started after the return window, a damage report outside the reporting window,
an order already refunded in full -- is answered with the applicable policy and
closed. Escalating these wastes reviewer time.

## [section_3] Out of scope for this workflow

Chargebacks, fraud adjudication, partial refunds of individual line items,
multi-currency orders and goodwill credits are out of scope for the automated
workflow and are handed to a reviewer untouched.

## [section_4] Identity and access

An agent answers only about orders belonging to the authenticated customer. If a
customer asks about an order that is not on their account, the agent says no such
order is available on the account and reveals nothing about whether it exists.
