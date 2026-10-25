"""Classification, retrieval and schema validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.classifier import _offline_classify
from app.agents.llm import LLMClient, ModelUsage
from app.agents.policy import build_query
from app.schemas.agent import Classification, ResolutionProposal
from app.services import policy_store
from app.services.common import redact
from app.services.embedding import cosine, embed, lexical_overlap


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("I returned my headphones last week, but I still haven't received my refund.", "refund_request"),
        ("Where is my order? Has it shipped yet?", "order_status"),
        ("My parcel is really late, it was supposed to arrive days ago.", "order_status"),
        ("The earbuds arrived with a cracked case.", "damaged_item"),
        ("I am raising a chargeback with my bank over this order.", "out_of_scope"),
        ("This is unacceptable, sort it out.", "unclear"),
    ],
)
def test_offline_classifier(message, expected):
    assert _offline_classify(message).category == expected


def test_damage_plus_return_is_a_refund_chase():
    result = _offline_classify("I returned the broken speaker, where is my money?")
    assert result.category == "refund_request"


def test_classifier_extracts_an_order_reference():
    result = _offline_classify("refund for ord_eligible_basic please")
    assert result.order_reference_mentioned == "ord_eligible_basic"


# --------------------------------------------------------------------------- #
# Embeddings and retrieval
# --------------------------------------------------------------------------- #
def test_embedding_is_deterministic_and_normalised():
    a = embed("return received but inspection pending")
    b = embed("return received but inspection pending")
    assert a == b
    assert cosine(a, b) == pytest.approx(1.0, abs=1e-9)


def test_unrelated_text_scores_lower_than_related_text():
    query = embed("refund for a returned item after inspection")
    close = embed("refunds for returned items become eligible after inspection passes")
    far = embed("the parcel is with the carrier and has not been scanned")
    assert cosine(query, close) > cosine(query, far)


def test_lexical_overlap_handles_synonyms():
    assert lexical_overlap("my earbuds are broken", "the headphones are damaged") > 0


@pytest.mark.parametrize(
    ("query", "expected_citation"),
    [
        ("refund eligibility returned item received inspection passed", "refund_policy_v2:section_3"),
        ("return requested outside the 30 day return window", "refund_policy_v2:section_2"),
        ("parcel is late past the carrier estimate", "shipping_delays_v1:section_2"),
        ("no tracking available for the order yet", "shipping_delays_v1:section_4"),
        ("damaged item photo required to report", "damaged_items_v1:section_1"),
        ("inspection failed refer to a reviewer", "return_processing_v1:section_3"),
    ],
)
def test_retrieval_finds_the_governing_section(session, query, expected_citation):
    hits = policy_store.search(session, query, top_k=4)
    assert expected_citation in [hit.citation for hit in hits]


def test_retrieval_excludes_superseded_policy_versions(session):
    """The v1 policy expired; it must never be cited for a current case."""
    hits = policy_store.search(session, "refund approval threshold return window", top_k=6)
    assert all(hit.document_id != "refund_policy_legacy" for hit in hits)


def test_retrieval_returns_nothing_for_an_unrelated_question(session):
    hits = policy_store.search(session, "quantum chromodynamics lattice spacing", top_k=4)
    assert hits == []


def test_fact_aware_query_expansion_mentions_the_blocker():
    facts = {
        "eligibility": {
            "facts": {"return_status": "received", "inspection_status": "pending"},
            "blockers": [{"code": "INSPECTION_NOT_PASSED", "message": "..."}],
        }
    }
    query = build_query("refund_request", "where is my refund", facts)
    assert "inspection" in query
    assert "received" in query


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
def test_resolution_proposal_rejects_a_negative_amount():
    with pytest.raises(ValidationError):
        ResolutionProposal(
            action="request_refund_approval",
            amount_minor=-1,
            reason="x",
            customer_message="y",
        )


def test_schemas_reject_unknown_fields():
    """A model cannot smuggle in a field the application does not expect."""
    with pytest.raises(ValidationError):
        Classification(category="refund_request", confidence=0.9, reviewer_id="rev_kiran")


def test_confidence_must_be_a_probability():
    with pytest.raises(ValidationError):
        Classification(category="refund_request", confidence=1.4)


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #
def test_client_runs_offline_without_a_key():
    client = LLMClient(ModelUsage())
    assert client.mode == "offline"
    result = client.structured(
        task="t",
        system="s",
        user="u",
        schema=Classification,
        offline=lambda: Classification(category="unclear", confidence=0.5),
    )
    assert result.category == "unclear"
    assert client.usage.calls == 1


def test_malformed_model_output_falls_back_rather_than_guessing():
    client = LLMClient(ModelUsage())
    client.mode = "anthropic"
    client._call = lambda *_: "not json at all"  # noqa: SLF001
    result = client.structured(
        task="t",
        system="s",
        user="u",
        schema=Classification,
        offline=lambda: Classification(category="unclear", confidence=0.1),
    )
    assert result.category == "unclear"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_audit_payloads_are_redacted():
    cleaned = redact(
        {
            "email": "priya@example.com",
            "note": "contact me at priya@example.com or 9876543210",
            "nested": {"api_key": "sk-secret", "fine": "ok"},
        }
    )
    assert cleaned["email"] == "<redacted>"
    assert "priya@example.com" not in cleaned["note"]
    assert "9876543210" not in cleaned["note"]
    assert cleaned["nested"]["api_key"] == "<redacted>"
    assert cleaned["nested"]["fine"] == "ok"
