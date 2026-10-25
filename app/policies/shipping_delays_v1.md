---
id: shipping_delays
title: Shipping Delays and Lost Shipments
version: v1
effective_from: 2025-06-01
source_uri: app/policies/shipping_delays_v1.md
topics: shipping_delay, order_status
---

## [section_1] Reporting the current state

For any question about where an order is, the agent reports the order status and
the latest carrier scan available, with the scan timestamp. The agent does not
predict a delivery date beyond the carrier's own estimate and does not describe
an order as delivered unless a delivery date is recorded.

## [section_2] Delayed shipments

A shipment is *delayed* when it has passed its estimated delivery date without a
delivery scan. For a delay of up to 7 days past the estimate, the resolution is
to explain the current state and keep the ticket open. No refund is prepared for
a delay alone.

## [section_3] Lost shipments

A shipment with no carrier scan for 10 consecutive days, or marked lost by the
carrier, is treated as lost. A lost shipment is referred to a support reviewer,
who may approve a refund without a return because there is nothing to return.

## [section_4] No tracking available

Where the carrier API returns no tracking for an order, the agent reports the
order status from the order record and says tracking is not yet available. The
agent must not state that the parcel is lost on the strength of missing
tracking data.
