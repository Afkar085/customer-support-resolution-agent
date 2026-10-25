"""Single-agent baseline.

One agent, one toolset containing every read tool, one pass: classify, gather,
retrieve policy, propose. It exists to be measured against the three-specialist
design rather than to be argued about.

What it genuinely does differently -- and what the eval measures:

* it builds the policy query from the customer's words alone, because there is
  no separate investigation step whose facts a later step could consume;
* it holds one broad toolset instead of three narrow ones, so least privilege
  is lost: the same caller can read orders, read policy and write a proposal.

It is *not* a single-prompt agent: it still runs the same four reasoning steps,
so this comparison measures what the specialist decomposition buys in accuracy
and retrieval quality, not what it costs in model calls. A true single-prompt
tool-use loop would be the next variant to add.

Everything below the proposal (validation, approval, execution) is identical,
because that part is ordinary code in both designs.
"""
from __future__ import annotations

from typing import Any

from app.agents import classifier
from app.agents import investigation as investigation_agent
from app.agents import policy as policy_agent
from app.agents import resolution as resolution_agent
from app.agents.llm import LLMClient
from app.schemas.agent import (
    Classification,
    InvestigationResult,
    PolicyResult,
    ResolutionProposal,
)
from app.tools.registry import Toolset


def run(
    llm: LLMClient,
    tools: Toolset,
    *,
    customer_message: str,
    ticket_order_id: str | None,
    intake_fn,
    currency: str,
) -> tuple[Classification, InvestigationResult, PolicyResult, ResolutionProposal, dict[str, Any]]:
    classification = classifier.classify(llm, customer_message, ticket_order_id)

    investigation = investigation_agent.investigate(
        llm,
        tools,
        classification=classification,
        customer_message=customer_message,
        ticket_order_id=ticket_order_id,
    )

    intake: dict[str, Any] = {}
    if classification.category == "damaged_item" and investigation.order_id:
        intake = intake_fn(investigation.order_id)

    # The baseline's handicap, stated plainly: no fact-aware query expansion.
    policy = policy_agent.retrieve(
        llm,
        tools,
        category=classification.category,
        customer_message=customer_message,
        facts={},
    )

    proposal = resolution_agent.propose(
        llm,
        classification=classification,
        investigation=investigation,
        policy=policy,
        intake=intake,
        currency=currency,
    )
    return classification, investigation, policy, proposal, intake
