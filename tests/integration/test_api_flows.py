"""End-to-end flows through the HTTP API."""
from __future__ import annotations

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import ActionProposal, RefundOperation, Ticket
from tests.conftest import CUSTOMER_TOKEN, READONLY_REVIEWER_TOKEN, REVIEWER_TOKEN


def _operations_for(order_id: str) -> list[RefundOperation]:
    """Scoped to one order: the seed data contains a historical refund."""
    with session_scope() as db:
        return db.scalars(
            select(RefundOperation).where(RefundOperation.order_id == order_id)
        ).all()



def _submit(client, auth, token, message, order_id=None, variant="specialists"):
    ticket = client.post(
        "/tickets",
        json={"customer_message": message, "order_id": order_id},
        headers=auth(token),
    )
    assert ticket.status_code == 201, ticket.text
    ticket_id = ticket.json()["ticket_id"]
    run = client.post(
        f"/tickets/{ticket_id}/runs",
        json={"variant": variant},
        headers=auth(token),
    )
    assert run.status_code == 202, run.text
    return ticket_id, run.json()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_eligible_refund_pauses_for_approval_then_executes(client, auth):
    _, run = _submit(
        client,
        auth,
        CUSTOMER_TOKEN,
        "I returned my headphones last week, but I still haven't received my refund.",
        "ord_eligible_basic",
    )
    assert run["status"] == "awaiting_approval"
    assert run["awaiting"]["amount_minor"] == 249900
    assert run["awaiting"]["policy_refs"]

    # Nothing has been sent to the provider yet.
    assert _operations_for("ord_eligible_basic") == []

    queue = client.get("/approvals", headers=auth(REVIEWER_TOKEN)).json()["approvals"]
    assert len(queue) == 1
    card = queue[0]
    assert card["proposal"]["evidence_refs"]
    assert card["amount_display"].endswith("2,499.00")

    decision = client.post(
        f"/approvals/{card['approval_id']}/decision",
        json={"decision": "approved", "note": "Return verified."},
        headers=auth(REVIEWER_TOKEN),
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["resumed"] is True
    assert body["run"]["outcome"] == "refund_succeeded"

    final = client.get(f"/runs/{body['run']['run_id']}", headers=auth(CUSTOMER_TOKEN)).json()
    assert final["status"] == "completed"
    assert "completed by your payment provider" in final["final_response"]
    assert len(final["refund_operations"]) == 1
    assert final["refund_operations"][0]["status"] == "succeeded"

    with session_scope() as db:
        ticket = db.scalar(select(Ticket).where(Ticket.id == final["ticket_id"]))
        assert ticket.status == "resolved"


def test_rejected_approval_issues_no_refund(client, auth):
    _, run = _submit(
        client, auth, CUSTOMER_TOKEN, "Refund my returned headphones please.", "ord_eligible_basic"
    )
    approval_id = run["awaiting"]["approval_id"]
    body = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "rejected", "note": "Serial number does not match."},
        headers=auth(REVIEWER_TOKEN),
    ).json()
    assert body["run"]["outcome"] == "rejected"
    assert "not able to approve" in body["run"]["final_response"]
    assert _operations_for("ord_eligible_basic") == []
    with session_scope() as db:
        proposal = db.scalar(
            select(ActionProposal).where(ActionProposal.run_id == body["run_id"])
        )
        assert proposal.status == "rejected"


# --------------------------------------------------------------------------- #
# Blocked, pending and escalated paths
# --------------------------------------------------------------------------- #
def test_pending_inspection_is_explained_not_refused(client, auth):
    _, run = _submit(
        client,
        auth,
        CUSTOMER_TOKEN,
        "I sent the earbuds back and they arrived. Where is my refund?",
        "ord_return_inspection_pending",
    )
    assert run["status"] == "completed"
    assert run["outcome"] == "answered"
    assert "inspection" in run["final_response"].lower()
    assert _operations_for("ord_return_inspection_pending") == []


def test_expired_window_is_refused_with_policy(client, auth):
    _, run = _submit(
        client,
        auth,
        "tok_cust_dev",
        "I want a refund for these earbuds, I returned them a while ago.",
        "ord_window_expired",
    )
    assert run["outcome"] == "answered"
    assert "return window" in run["final_response"]
    assert _operations_for("ord_window_expired") == []


def test_failed_inspection_escalates_to_a_human(client, auth):
    _, run = _submit(
        client,
        auth,
        "tok_cust_dev",
        "I returned the soundbar because it was faulty. I want my money back.",
        "ord_inspection_failed",
    )
    assert run["status"] == "escalated"
    assert run["outcome"] == "escalated"


def test_order_status_answers_from_the_carrier_record(client, auth):
    _, run = _submit(
        client, auth, CUSTOMER_TOKEN, "Where is my order? Has it shipped?", "ord_in_transit"
    )
    assert run["outcome"] == "answered"
    assert "BD4411902311" in run["final_response"]


def test_missing_tracking_is_not_reported_as_lost(client, auth):
    _, run = _submit(
        client, auth, "tok_cust_dev", "Where is my order? Has it shipped?", "ord_no_tracking"
    )
    assert "lost" not in run["final_response"].lower()
    assert "tracking" in run["final_response"].lower()


def test_lost_shipment_escalates(client, auth):
    _, run = _submit(
        client,
        auth,
        "tok_cust_meera",
        "The tracking hasn't moved in two weeks. Where is my power bank?",
        "ord_lost",
    )
    assert run["outcome"] == "escalated"


def test_incomplete_damage_report_asks_for_what_is_missing(client, auth):
    _, run = _submit(
        client,
        auth,
        "tok_cust_meera",
        "The earbuds arrived with a cracked case.",
        "ord_delivered_recent",
    )
    assert run["outcome"] == "clarification_requested"
    assert any("photo" in item for item in run["state"]["missing_information"])


def test_complete_damage_report_goes_to_a_reviewer(client, auth):
    _, run = _submit(
        client,
        auth,
        "tok_cust_meera",
        "The earbuds arrived with a cracked case and a dented box. Photo attached.",
        "ord_delivered_recent",
    )
    assert run["outcome"] == "escalated"


def test_vague_complaint_asks_a_targeted_question(client, auth):
    _, run = _submit(client, auth, CUSTOMER_TOKEN, "This is unacceptable, sort it out.")
    assert run["outcome"] == "clarification_requested"
    assert "?" in run["final_response"]


def test_followup_message_starts_a_fresh_run_with_full_context(client, auth):
    ticket_id, run = _submit(
        client,
        auth,
        "tok_cust_meera",
        "The earbuds arrived with a cracked case.",
        "ord_delivered_recent",
    )
    assert run["outcome"] == "clarification_requested"
    follow_up = client.post(
        f"/runs/{run['run_id']}/messages",
        json={"message": "Here is the photo of the cracked case and the box.", "restart": True},
        headers=auth("tok_cust_meera"),
    ).json()
    assert follow_up["restarted"] is True
    assert follow_up["outcome"] == "escalated"
    assert follow_up["run_id"] != run["run_id"]


def test_out_of_scope_request_is_handed_over(client, auth):
    _, run = _submit(
        client,
        auth,
        CUSTOMER_TOKEN,
        "I am raising a chargeback with my bank over this order.",
        "ord_eligible_basic",
    )
    assert run["outcome"] == "escalated"


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
def test_event_timeline_is_ordered_and_redacted(client, auth):
    _, run = _submit(
        client, auth, CUSTOMER_TOKEN, "Where is my order?", "ord_in_transit"
    )
    events = client.get(f"/runs/{run['run_id']}/events", headers=auth(CUSTOMER_TOKEN)).json()[
        "events"
    ]
    types = [event["event_type"] for event in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "run.policy_retrieved" in types
    assert "@example.com" not in str(events)


def test_run_records_cost_and_call_counters(client, auth):
    _, run = _submit(client, auth, CUSTOMER_TOKEN, "Where is my order?", "ord_in_transit")
    view = client.get(f"/runs/{run['run_id']}", headers=auth(CUSTOMER_TOKEN)).json()
    assert view["model_calls"] >= 1
    assert view["tool_calls"] >= 1
    assert view["active_ms"] >= 0
    assert view["snapshot"]["model_mode"] == "offline"


def test_single_agent_baseline_reaches_the_same_decision(client, auth):
    _, run = _submit(
        client,
        auth,
        CUSTOMER_TOKEN,
        "I returned my headphones last week, but I still haven't received my refund.",
        "ord_eligible_basic",
        variant="baseline",
    )
    assert run["status"] == "awaiting_approval"
    assert run["awaiting"]["amount_minor"] == 249900


def test_parallel_variant_reaches_the_same_decision(client, auth):
    _, run = _submit(
        client,
        auth,
        CUSTOMER_TOKEN,
        "I returned my headphones last week, but I still haven't received my refund.",
        "ord_eligible_basic",
        variant="parallel",
    )
    assert run["status"] == "awaiting_approval"
    assert run["awaiting"]["amount_minor"] == 249900


def test_health_and_meta_endpoints(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["policy_documents"] == 6

    tools = client.get("/meta/tools").json()["tools"]
    investigation = next(t for t in tools if t["tool"] == "get_order")
    assert "investigation" in investigation["toolsets"]
    execute = next(t for t in tools if t["tool"] == "execute_approved_refund")
    assert execute["toolsets"] == ["executor"]


def test_readonly_reviewer_cannot_decide(client, auth):
    _, run = _submit(
        client, auth, CUSTOMER_TOKEN, "Refund my returned headphones.", "ord_eligible_basic"
    )
    approval_id = run["awaiting"]["approval_id"]
    assert client.get("/approvals", headers=auth(READONLY_REVIEWER_TOKEN)).status_code == 200
    denied = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers=auth(READONLY_REVIEWER_TOKEN),
    )
    assert denied.status_code == 403
    assert _operations_for("ord_eligible_basic") == []
