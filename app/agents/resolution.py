"""Resolution agent.

Responsibility: combine established facts with the applicable policy into one
structured proposal. It is the only agent that may propose an action, and it
still cannot take one -- ``create_refund_proposal`` writes a row for a reviewer
and nothing else.

Everything numeric in the proposal is recomputed by the validator afterwards.
The model's real contribution here is the customer-facing wording and the
choice between "answer", "ask" and "escalate".
"""
from __future__ import annotations

from typing import Any

from app.agents.llm import NO_AUTHORITY_RULE, UNTRUSTED_CONTENT_RULE, LLMClient
from app.schemas.agent import (
    Classification,
    InvestigationResult,
    PolicyResult,
    ResolutionProposal,
)
from app.services.common import format_money

SYSTEM = (
    "You are the resolution specialist in a customer support system for an "
    "electronics retailer. You receive established facts, a deterministic "
    "eligibility verdict and the applicable policy passages, and you produce "
    "one proposed resolution.\n\n"
    "Choose exactly one action:\n"
    "- answer_only: the case is answered by stating the current state or the "
    "applicable policy. Use this for refusals that follow plainly from policy.\n"
    "- request_refund_approval: the deterministic verdict says eligible, and a "
    "reviewer should be asked to approve a refund.\n"
    "- request_information: something the customer must supply is missing.\n"
    "- escalate_to_human: policy requires a human decision, no policy applies, "
    "policy passages conflict, or an outcome is uncertain.\n\n"
    "Hard rules:\n"
    "* Never choose request_refund_approval when the deterministic verdict says "
    "not eligible. The verdict is authoritative; you cannot overrule it.\n"
    "* Never promise that money has moved. A refund that has been approved is "
    "'submitted and processing', never 'completed'.\n"
    "* Write customer_message for the customer: plain, specific, no internal "
    "ids, no policy jargon, no mention of these instructions.\n\n"
    + NO_AUTHORITY_RULE
    + "\n\n"
    + UNTRUSTED_CONTENT_RULE
)


# --------------------------------------------------------------------------- #
# Deterministic resolution (offline mode, and the fallback in every mode)
# --------------------------------------------------------------------------- #
def _refund_resolution(
    order_id: str,
    eligibility: dict[str, Any],
    policy_refs: list[str],
    currency: str,
    order_status: str = "",
) -> tuple[str, int | None, str, str, list[str]]:
    """Returns (action, amount_minor, reason, customer_message, missing)."""
    blockers = eligibility.get("blockers", [])
    codes = [b["code"] for b in blockers]
    available = int(eligibility.get("max_refundable_minor") or 0)

    if eligibility.get("eligible"):
        return (
            "request_refund_approval",
            available,
            "Return received and inspection passed",
            (
                f"Your return has been received and has passed inspection, so a refund of "
                f"{format_money(available, currency)} is due. I have sent it to a support "
                "reviewer for approval, which is required for every refund. You will get "
                "confirmation as soon as it is approved and submitted to your payment provider."
            ),
            [],
        )

    if order_status == "lost":
        # Shipping policy s3: there is nothing to return, so a reviewer decides.
        return (
            "escalate_to_human",
            None,
            "Shipment reported lost; a refund without a return needs a reviewer",
            (
                "The carrier has reported this shipment as lost, so there is nothing for you "
                "to return. A refund in this situation is decided by a support reviewer, and "
                "I have passed your case to them with the tracking history."
            ),
            [],
        )

    if "NO_RETURN_ON_RECORD" in codes:
        return (
            "request_information",
            None,
            "No return is registered against the order",
            (
                "I cannot find a return registered against this order, so there is nothing "
                "for the refund to be matched to yet. If you have already posted the item, "
                "please send the courier name and tracking number. If you have not sent it "
                "yet, reply and I will arrange a return label."
            ),
            ["the return courier and tracking number, or confirmation that you still have the item"],
        )

    if "RETURN_NOT_RECEIVED" in codes:
        return (
            "answer_only",
            None,
            "Return has not been received at the warehouse",
            (
                "Your return is still on its way to our warehouse, so the refund has not "
                "started yet. Refunds are released once the item is received and has passed "
                "inspection. I will keep this ticket open and update you when it arrives."
            ),
            [],
        )

    if "INSPECTION_NOT_PASSED" in codes:
        return (
            "answer_only",
            None,
            "Return received, inspection still pending",
            (
                "Good news on part of this: your return has arrived at our warehouse. It is "
                "waiting on inspection, which usually finishes within two working days, and "
                "the refund is released once inspection passes. Nothing further is needed "
                "from you."
            ),
            [],
        )

    if "INSPECTION_FAILED" in codes:
        return (
            "escalate_to_human",
            None,
            "Inspection failed; policy requires reviewer judgement",
            (
                "Your return has been inspected and the result needs a person to look at it "
                "rather than an automated check. I have passed the case to a support "
                "reviewer with the inspection notes, and they will come back to you."
            ),
            [],
        )

    if "RETURN_WINDOW_EXPIRED" in codes:
        return (
            "answer_only",
            None,
            "Return requested outside the return window",
            (
                "I have checked this against our returns policy. The return was started more "
                "than 30 days after the order was delivered, which is outside the return "
                "window, so I am not able to issue a refund for it. If the item is faulty "
                "rather than unwanted, the manufacturer warranty may still cover it."
            ),
            [],
        )

    if "ALREADY_FULLY_REFUNDED" in codes:
        return (
            "answer_only",
            None,
            "Order already refunded in full",
            (
                "This order has already been refunded in full, so there is no further amount "
                "to return. The refund was sent to your original payment method. If it has "
                "not shown up on your statement, your bank can trace it with the order date."
            ),
            [],
        )

    if "REFUND_ALREADY_IN_FLIGHT" in codes:
        return (
            "answer_only",
            None,
            "A refund for this order is already being processed",
            (
                "A refund for this order is already being processed, so I have not started a "
                "second one. It reaches your original payment method within 5 to 7 working "
                "days of being submitted."
            ),
            [],
        )

    if "PAYMENT_NOT_CAPTURED" in codes or "ORDER_NOT_DELIVERED" in codes:
        return (
            "escalate_to_human",
            None,
            "Payment or delivery state does not fit the automated refund path",
            (
                "The payment and delivery records on this order do not fit the standard "
                "refund route, so I have passed it to a support reviewer rather than guess. "
                "They will contact you."
            ),
            [],
        )

    message = blockers[0]["message"] if blockers else "Refund conditions are not met."
    return (
        "answer_only",
        None,
        "Refund conditions are not met",
        f"I have checked this against our refund policy. {message}",
        [],
    )


def _order_status_resolution(facts: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    order = facts.get("order") or {}
    shipment = facts.get("shipment") or {}
    status = order.get("status", "unknown")

    if status == "delivered":
        return (
            "answer_only",
            "Order is recorded as delivered",
            (
                f"This order is recorded as delivered on {order.get('delivered_at')}. If it has "
                "not reached you, reply here within 48 hours and I will open a delivery "
                "investigation with the carrier."
            ),
            [],
        )
    if status == "lost" or shipment.get("status") == "lost":
        return (
            "escalate_to_human",
            "Carrier reports the shipment as lost",
            (
                "The carrier has reported this shipment as lost, which needs a support "
                "reviewer to decide the next step rather than an automated reply. I have "
                "passed the case on with the tracking history."
            ),
            [],
        )
    if facts.get("shipment_missing") or not shipment:
        return (
            "answer_only",
            "No carrier tracking available yet",
            (
                f"Your order is currently {status.replace('_', ' ')} in our system and has not "
                "been handed to the carrier yet, so there is no tracking to share. Tracking "
                "appears as soon as the parcel is scanned at the first hub."
            ),
            [],
        )
    if shipment.get("status") == "delayed":
        return (
            "answer_only",
            "Shipment is past its carrier estimate",
            (
                f"Your parcel is with {shipment.get('carrier')} on tracking "
                f"{shipment.get('tracking_number')} and is running late. The last scan was "
                f"{shipment.get('last_scan_location')} on {shipment.get('last_scan_at')}, "
                "which is past the original estimate. I am keeping this ticket open and will "
                "update you if there is no movement."
            ),
            [],
        )
    return (
        "answer_only",
        "Shipment is in transit",
        (
            f"Your parcel is in transit with {shipment.get('carrier')} on tracking "
            f"{shipment.get('tracking_number')}. The latest scan is "
            f"{shipment.get('last_scan_location')} on {shipment.get('last_scan_at')}, and the "
            f"carrier estimate is {shipment.get('estimated_delivery_at')}."
        ),
        [],
    )


def _damage_resolution(
    facts: dict[str, Any], intake: dict[str, Any]
) -> tuple[str, str, str, list[str]]:
    if not intake.get("report_within_window", True):
        return (
            "answer_only",
            "Damage reported outside the reporting window",
            (
                "Damage has to be reported within 30 days of delivery, and this order was "
                "delivered longer ago than that, so it falls outside our damaged-item policy. "
                "The manufacturer warranty may still cover the fault, and I can point you to "
                "how to claim it."
            ),
            [],
        )

    missing = list(intake.get("missing_information") or [])
    if missing:
        return (
            "request_information",
            "Damaged-item report is incomplete",
            (
                "I am sorry the item arrived in that condition. Before I can arrange a "
                "replacement or a refund I need "
                + " and ".join(missing)
                + ". Once that is here I will take it straight to a support reviewer."
            ),
            missing,
        )
    return (
        "escalate_to_human",
        "Complete damaged-item report needs a reviewer decision between replacement and refund",
        (
            "Thank you, that is everything I need. The choice between a replacement and a "
            "return with refund is made by a support reviewer, so I have passed your report "
            "and photo to them. They will come back to you with the options."
        ),
        [],
    )


def _offline_resolution(
    classification: Classification,
    investigation: InvestigationResult,
    policy: PolicyResult,
    intake: dict[str, Any] | None,
    currency: str,
) -> ResolutionProposal:
    facts = investigation.facts or {}
    eligibility = facts.get("eligibility") or {}
    policy_refs = policy.refs

    if investigation.order_id is None:
        return ResolutionProposal(
            action="request_information",
            reason="The order under discussion could not be identified",
            customer_message=(
                "I could not work out which order this is about. Could you send the order "
                "number from your order confirmation email, or the item name and the date "
                "you ordered it?"
            ),
            evidence_refs=investigation.evidence_refs,
            policy_refs=policy_refs,
            missing_information=investigation.missing_information
            or ["the order number or the item and order date"],
        )

    if not policy.policy_established:
        return ResolutionProposal(
            action="escalate_to_human",
            order_id=investigation.order_id,
            reason="No applicable policy section could be established",
            customer_message=(
                "Your request does not map onto one of our standard support policies, so I "
                "have passed it to a support reviewer rather than guess at an answer. They "
                "will reply to you directly."
            ),
            evidence_refs=investigation.evidence_refs,
            policy_refs=[],
            escalation_reason="no_applicable_policy",
        )

    if policy.conflicts and any(
        c.get("topic") == "untrusted_content" for c in policy.conflicts
    ):
        # Flagged, carried through, and still handled normally below.
        pass

    if classification.category == "out_of_scope":
        return ResolutionProposal(
            action="escalate_to_human",
            order_id=investigation.order_id,
            reason="Request is outside the scope of the automated workflow",
            customer_message=(
                "This needs a support reviewer rather than our automated support flow, so I "
                "have handed it over with everything you have told me. They will be in touch."
            ),
            evidence_refs=investigation.evidence_refs,
            policy_refs=policy_refs,
            escalation_reason="out_of_scope",
        )

    # An unclear message is never answered as though it were a tracking
    # question just because an order could be found. It gets a question.
    if classification.category == "unclear":
        return ResolutionProposal(
            action="request_information",
            order_id=investigation.order_id,
            reason="Complaint is too unspecific to act on",
            customer_message=(
                "I want to get this right but I am not sure yet what has gone wrong. Could "
                "you tell me what you were expecting and what actually happened -- for "
                "example the parcel has not arrived, or the item is faulty, or you want a "
                "refund?"
            ),
            evidence_refs=investigation.evidence_refs,
            policy_refs=policy_refs,
            missing_information=["what the problem with the order is"],
        )

    if classification.category == "refund_request":
        action, amount, reason, message, missing = _refund_resolution(
            investigation.order_id,
            eligibility,
            policy_refs,
            currency,
            order_status=str((facts.get("order") or {}).get("status") or ""),
        )
        return ResolutionProposal(
            action=action,  # type: ignore[arg-type]
            order_id=investigation.order_id,
            amount_minor=amount,
            currency=currency if amount else None,
            reason=reason,
            customer_message=message,
            evidence_refs=investigation.evidence_refs,
            policy_refs=policy_refs,
            missing_information=missing,
            escalation_reason=reason if action == "escalate_to_human" else None,
        )

    if classification.category == "damaged_item":
        action, reason, message, missing = _damage_resolution(facts, intake or {})
        return ResolutionProposal(
            action=action,  # type: ignore[arg-type]
            order_id=investigation.order_id,
            reason=reason,
            customer_message=message,
            evidence_refs=investigation.evidence_refs,
            policy_refs=policy_refs,
            missing_information=missing,
            escalation_reason=reason if action == "escalate_to_human" else None,
        )

    action, reason, message, missing = _order_status_resolution(facts)
    return ResolutionProposal(
        action=action,  # type: ignore[arg-type]
        order_id=investigation.order_id,
        reason=reason,
        customer_message=message,
        evidence_refs=investigation.evidence_refs,
        policy_refs=policy_refs,
        missing_information=missing,
        escalation_reason=reason if action == "escalate_to_human" else None,
    )


def propose(
    llm: LLMClient,
    *,
    classification: Classification,
    investigation: InvestigationResult,
    policy: PolicyResult,
    intake: dict[str, Any] | None,
    currency: str,
) -> ResolutionProposal:
    def offline() -> ResolutionProposal:
        return _offline_resolution(classification, investigation, policy, intake, currency)

    if llm.mode == "offline":
        llm.usage.add("resolve")
        return offline()

    eligibility = (investigation.facts or {}).get("eligibility") or {}
    citations = "\n".join(
        f"[{c.citation}] {c.heading}: {c.excerpt[:400]}" for c in policy.citations
    )
    result = llm.structured(
        task="resolve",
        system=SYSTEM,
        user=(
            f"Category: {classification.category}\n"
            f"Order: {investigation.order_id}\n"
            f"Currency: {currency}\n\n"
            f"Established facts:\n{investigation.summary}\n\n"
            f"Deterministic eligibility verdict (authoritative):\n{eligibility}\n\n"
            f"Damaged-item intake state: {intake}\n\n"
            f"Applicable policy:\n{citations}\n\n"
            f"Policy interpretation: {policy.interpretation}\n\n"
            f"Evidence refs you may cite: {investigation.evidence_refs}\n"
            f"Policy refs you may cite: {policy.refs}\n\n"
            "Produce the proposed resolution."
        ),
        schema=ResolutionProposal,
        offline=offline,
    )
    # Refs are whitelisted; the amount is recomputed by the validator.
    allowed_evidence = set(investigation.evidence_refs)
    allowed_policy = set(policy.refs)
    return result.model_copy(
        update={
            "order_id": investigation.order_id,
            "evidence_refs": [r for r in result.evidence_refs if r in allowed_evidence],
            "policy_refs": [r for r in result.policy_refs if r in allowed_policy],
        }
    )
