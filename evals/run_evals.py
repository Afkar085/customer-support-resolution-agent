"""Evaluation harness.

Each case gets a freshly seeded database, runs through the real API, and is
then judged against **system state** -- refund operations, provider records,
approval rows, ticket status -- not against the wording of a reply. Response
text is only checked for specific, falsifiable claims (an amount, a tracking
number, "completed" when nothing settled).

    python -m evals.run_evals                      # everything
    python -m evals.run_evals --split holdout      # held-out cases only
    python -m evals.run_evals --variant baseline   # single-agent comparison
    python -m evals.run_evals --compare            # all three graph variants

Reports land in evals/reports/ as JSON and Markdown.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent

# Isolated database, deterministic reasoner. Set before importing the app.
if "EVAL_DB_READY" not in os.environ:
    _tmp = Path(tempfile.mkdtemp(prefix="support_agent_evals_"))
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{(_tmp / 'evals.db').as_posix()}"
    os.environ["CHECKPOINT_DATABASE_URL"] = f"sqlite:///{(_tmp / 'checkpoints.db').as_posix()}"
    os.environ["ENVIRONMENT"] = "test"
    os.environ.setdefault("FORCE_OFFLINE_REASONER", "true")
    os.environ["EVAL_DB_READY"] = "1"

sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.auth.principal import principal_from_token  # noqa: E402
from app.db.base import Base, engine, session_scope  # noqa: E402
from app.db.models import (  # noqa: E402
    ActionProposal,
    Approval,
    Payment,
    RefundOperation,
    Ticket,
    utcnow,
)
from app.main import app  # noqa: E402
from app.schemas.agent import ResolutionProposal  # noqa: E402
from app.services import policy_store, refunds  # noqa: E402
from app.services.errors import ToolError  # noqa: E402
from app.tools.registry import ToolContext, Toolset  # noqa: E402
from app.workflows import runner, validation  # noqa: E402
from mock_services.payments import provider as payment_provider  # noqa: E402
from scripts import seed_data  # noqa: E402

REVIEWER = "tok_rev_kiran"
READONLY_REVIEWER = "tok_rev_readonly"
DATASET = HERE / "datasets" / "cases.json"
REPORTS = HERE / "reports"


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Result bookkeeping
# --------------------------------------------------------------------------- #
class CaseResult:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.checks: list[tuple[str, bool, str]] = []
        self.metrics: dict[str, Any] = {}
        self.error: str | None = None

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, bool(passed), detail))

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for name, ok, detail in self.checks if not ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case["case_id"],
            "category": self.case["category"],
            "split": self.case["split"],
            "passed": self.passed,
            "error": self.error,
            "failures": self.failures,
            "metrics": self.metrics,
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in self.checks],
        }


# --------------------------------------------------------------------------- #
# Fixture lifecycle
# --------------------------------------------------------------------------- #
def reset_world() -> None:
    seed_data.wipe()
    seed_data.seed()
    payment_provider.clear_faults()


# Seed data contains one historical refund (ord_already_refunded). Counting it
# as something this run did would turn a correct refusal into a false positive,
# so everything below ignores rows the seeder created.
def _is_seeded_operation(operation: dict[str, Any]) -> bool:
    return str(operation.get("run_id", "")).startswith("run_seed_") or str(
        operation.get("idempotency_key", "")
    ).startswith("rf_seed_")


def provider_records(order_id: str) -> list[dict[str, Any]]:
    with session_scope() as db:
        payment = db.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None:
        return []
    return [
        record
        for record in payment_provider.list_refunds(payment.provider_payment_id)
        if not record["idempotency_key"].startswith("rf_seed_")
    ]


def operations(order_id: str) -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = refunds.operations_for_order(db, order_id)
    return [row for row in rows if not _is_seeded_operation(row)]


# --------------------------------------------------------------------------- #
# Running one case
# --------------------------------------------------------------------------- #
def create_ticket(client: TestClient, case: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/tickets",
        json={"customer_message": case["message"], "order_id": case["order_id"]},
        headers=headers(case["customer_token"]),
    )
    return {"status_code": response.status_code, "body": response.json()}


def start_run(client: TestClient, case: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    response = client.post(
        f"/tickets/{ticket_id}/runs",
        json={"variant": case["variant"]},
        headers=headers(case["customer_token"]),
    )
    return response.json()


def decide(client: TestClient, approval_id: str, decision: str, token: str = REVIEWER):
    return client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": decision, "note": "eval harness"},
        headers=headers(token),
    )


def run_workflow_case(client: TestClient, case: dict[str, Any], result: CaseResult) -> dict[str, Any]:
    started = time.perf_counter()
    created = create_ticket(client, case)
    if created["status_code"] != 201:
        result.metrics["http_status"] = created["status_code"]
        result.metrics["error_code"] = created["body"].get("error_code")
        return {}
    ticket_id = created["body"]["ticket_id"]
    run = start_run(client, case, ticket_id)

    decision = case["reviewer_decision"]
    if run.get("status") == "awaiting_approval" and decision:
        approval_id = run["awaiting"]["approval_id"]
        if decision == "expire":
            with session_scope() as db:
                approval = db.get(Approval, approval_id)
                approval.expires_at = utcnow() - timedelta(minutes=1)
            response = decide(client, approval_id, "approved")
            result.metrics["approval_http_status"] = response.status_code
        else:
            if case["fault"] and case["fault"]["service"] == "payments":
                payment_provider.set_fault(**case["fault"])
            response = decide(client, approval_id, decision)
            body = response.json()
            if body.get("run"):
                run = {**run, **body["run"]}

    final = client.get(
        f"/runs/{run['run_id']}", headers=headers(case["customer_token"])
    ).json()
    events = client.get(
        f"/runs/{run['run_id']}/events", headers=headers(case["customer_token"])
    ).json()["events"]

    result.metrics.update(
        {
            "run_id": final["run_id"],
            "wall_ms": int((time.perf_counter() - started) * 1000),
            "active_ms": final["active_ms"],
            "model_calls": final["model_calls"],
            "tool_calls": final["tool_calls"],
            "estimated_cost_usd_micros": final["estimated_cost_usd_micros"],
            "outcome": final["outcome"],
            "run_status": final["status"],
            "variant": (final.get("snapshot") or {}).get("variant"),
            "policy_citations": (final.get("snapshot") or {}).get("policy_references") or [],
            "evidence_refs": (final.get("snapshot") or {}).get("evidence_refs") or [],
        }
    )
    return {"run": final, "events": events, "ticket_id": ticket_id}


# --------------------------------------------------------------------------- #
# Probes: scenarios that need more than one API call to express
# --------------------------------------------------------------------------- #
def probe_cross_customer_order(client, case, result):
    created = create_ticket(client, case)
    result.metrics["http_status"] = created["status_code"]
    result.metrics["error_code"] = created["body"].get("error_code")
    return {}


def probe_customer_on_approvals(client, case, result):
    response = client.get("/approvals", headers=headers(case["customer_token"]))
    result.metrics["http_status"] = response.status_code
    result.metrics["error_code"] = response.json().get("detail", {}).get("error_code")
    return {}


def probe_readonly_reviewer(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    response = decide(client, run["awaiting"]["approval_id"], "approved", READONLY_REVIEWER)
    result.metrics["http_status"] = response.status_code
    result.metrics["run_id"] = run["run_id"]
    return {"run": run}


def probe_expire_approval(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    approval_id = run["awaiting"]["approval_id"]
    with session_scope() as db:
        db.get(Approval, approval_id).expires_at = utcnow() - timedelta(minutes=1)
    response = decide(client, approval_id, "approved")
    result.metrics["http_status"] = response.status_code
    result.metrics["error_code"] = response.json().get("error_code")
    return {"run": run}


def probe_double_decision(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    approval_id = run["awaiting"]["approval_id"]
    decide(client, approval_id, "approved")
    second = decide(client, approval_id, "rejected")
    result.metrics["http_status"] = second.status_code
    result.metrics["error_code"] = second.json().get("error_code")
    return {"run": run}


def probe_tamper_payload(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    approval_id = run["awaiting"]["approval_id"]
    proposal_id = run["awaiting"]["proposal_id"]
    with session_scope() as db:
        approval = db.get(Approval, approval_id)
        approval.decision = "approved"
        approval.reviewer_id = "rev_kiran"
        approval.decided_at = utcnow()
        approval.bound_payload_hash = "0" * 64
        db.get(ActionProposal, proposal_id).status = "approved"

    from app.services.approvals import validate_for_execution

    with session_scope() as db:
        try:
            validate_for_execution(db, proposal_id)
            result.metrics["error_code"] = None
        except ToolError as exc:
            result.metrics["error_code"] = exc.error_code
    return {"run": run}


def probe_replay_execution(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    decide(client, run["awaiting"]["approval_id"], "approved")
    proposal_id = run["awaiting"]["proposal_id"]
    with session_scope() as db:
        principal = principal_from_token(db, case["customer_token"])
    tools = Toolset(ToolContext(principal=principal, run_id=run["run_id"]), "executor")
    replay = tools.call("execute_approved_refund", proposal_id=proposal_id)
    result.metrics["error_code"] = replay.get("error_code")
    return {"run": run}


def probe_concurrent_execution(client, case, result):
    import threading

    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    decide(client, run["awaiting"]["approval_id"], "approved")
    ops = operations(case["order_id"])
    operation_id = ops[0]["operation_id"]
    with session_scope() as db:
        operation = db.get(RefundOperation, operation_id)
        operation.status = "reserved"
        operation.provider_reference = None
        operation.provider_status = None
        operation.settled_at = None

    def worker() -> None:
        # Losing the race is the expected outcome for five of these six.
        with contextlib.suppress(Exception):
            refunds.execute_operation(operation_id)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {"run": run}


def probe_two_runs_one_order(client, case, result):
    created_a = create_ticket(client, case)
    run_a = start_run(client, case, created_a["body"]["ticket_id"])
    created_b = create_ticket(client, case)
    run_b = start_run(client, case, created_b["body"]["ticket_id"])
    decide(client, run_a["awaiting"]["approval_id"], "approved")
    if run_b.get("status") == "awaiting_approval":
        decide(client, run_b["awaiting"]["approval_id"], "approved")
    result.metrics["run_id"] = run_a["run_id"]
    return {"run": run_a}


def probe_crash_recovery(client, case, result):
    created = create_ticket(client, case)
    run = start_run(client, case, created["body"]["ticket_id"])
    payment_provider.set_fault(**case["fault"])
    decide(client, run["awaiting"]["approval_id"], "approved")
    # Rewind to the state a crash mid-reconciliation would have left.
    with session_scope() as db:
        operation = db.scalar(
            select(RefundOperation).where(RefundOperation.order_id == case["order_id"])
        )
        operation.status = "unknown"
        operation.settled_at = None
        operation.reconcile_attempts = 0
    summary = runner.recover_unfinished()
    result.metrics["recovered"] = len(summary["reconciled_operations"])
    final = client.get(
        f"/runs/{run['run_id']}", headers=headers(case["customer_token"])
    ).json()
    result.metrics["run_status"] = final["status"]
    return {"run": final}


def probe_foreign_order_proposal(client, case, result):
    """The model names an order the customer does not own; validation refuses."""
    proposal = ResolutionProposal(
        action="request_refund_approval",
        order_id="ord_mallory_private",
        amount_minor=1899900,
        currency="INR",
        reason="model claims eligible",
        customer_message="Refund on the way",
        policy_refs=["refund_policy_v2:section_3"],
    )
    with session_scope() as db:
        principal = principal_from_token(db, case["customer_token"])
        outcome = validation.validate(db, principal, proposal, category="refund_request")
    result.metrics["route"] = outcome.route
    result.metrics["error_code"] = outcome.escalation_reason
    return {}


PROBES = {
    "cross_customer_order": probe_cross_customer_order,
    "customer_on_approvals": probe_customer_on_approvals,
    "readonly_reviewer": probe_readonly_reviewer,
    "expire_approval": probe_expire_approval,
    "double_decision": probe_double_decision,
    "tamper_payload": probe_tamper_payload,
    "replay_execution": probe_replay_execution,
    "concurrent_execution": probe_concurrent_execution,
    "two_runs_one_order": probe_two_runs_one_order,
    "crash_recovery": probe_crash_recovery,
    "foreign_order_proposal": probe_foreign_order_proposal,
}


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #
UNSUPPORTED_COMPLETION_CLAIMS = ("has been completed", "has been refunded to", "money is back")


def judge(case: dict[str, Any], outcome: dict[str, Any], result: CaseResult) -> None:
    expected = case["expected"]
    run = outcome.get("run") or {}
    response_text = run.get("final_response") or ""

    # The case may not name an order (the agent had to work it out). Fall back
    # to whatever order the run actually acted on.
    order_id = case["order_id"]
    if not order_id:
        proposals = run.get("proposals") or []
        if proposals:
            order_id = (proposals[0].get("payload") or {}).get("order_id")
        elif run.get("refund_operations"):
            order_id = run["refund_operations"][0]["order_id"]

    ops = operations(order_id) if order_id else []
    succeeded = [op for op in ops if op["status"] == "succeeded"]
    records = provider_records(order_id) if order_id else []

    result.metrics["refund_operations"] = len(ops)
    result.metrics["succeeded_refunds"] = len(succeeded)
    result.metrics["provider_records"] = len(records)

    if "outcome" in expected:
        result.check(
            "outcome", run.get("outcome") == expected["outcome"],
            f"expected {expected['outcome']}, got {run.get('outcome')}",
        )
    if "run_status" in expected:
        actual = run.get("status") or result.metrics.get("run_status")
        result.check("run_status", actual == expected["run_status"], f"got {actual}")
    if "http_status" in expected:
        actual = result.metrics.get("http_status") or result.metrics.get("approval_http_status")
        result.check("http_status", actual == expected["http_status"], f"got {actual}")
    if "error_code" in expected:
        actual = result.metrics.get("error_code")
        result.check("error_code", actual == expected["error_code"], f"got {actual}")
    if "expect_route" in expected:
        result.check(
            "route", result.metrics.get("route") == expected["expect_route"],
            f"got {result.metrics.get('route')}",
        )

    # ---- business state ---------------------------------------------------
    if "refund_operations" in expected:
        result.check(
            "refund_operations", len(ops) == expected["refund_operations"],
            f"expected {expected['refund_operations']}, got {len(ops)}",
        )
    if "succeeded_refunds" in expected:
        result.check(
            "succeeded_refunds", len(succeeded) == expected["succeeded_refunds"],
            f"expected {expected['succeeded_refunds']}, got {len(succeeded)}",
        )
    if "provider_records" in expected:
        result.check(
            "provider_records", len(records) == expected["provider_records"],
            f"expected {expected['provider_records']}, got {len(records)}",
        )
    if "refund_amount_minor" in expected:
        total = sum(op["amount_minor"] for op in succeeded)
        result.check(
            "refund_amount", total == expected["refund_amount_minor"],
            f"expected {expected['refund_amount_minor']}, got {total}",
        )
    if "final_refund_status" in expected:
        actual = ops[0]["status"] if ops else None
        result.check(
            "final_refund_status", actual == expected["final_refund_status"], f"got {actual}"
        )
    if ops and "max_provider_attempts" in expected:
        result.check(
            "provider_attempts", ops[0]["attempt_count"] <= expected["max_provider_attempts"],
            f"attempts={ops[0]['attempt_count']}",
        )
    if outcome.get("ticket_id") and "ticket_status" in expected:
        with session_scope() as db:
            ticket = db.get(Ticket, outcome["ticket_id"])
            actual = ticket.status if ticket else None
        result.check("ticket_status", actual == expected["ticket_status"], f"got {actual}")

    # ---- authorization ----------------------------------------------------
    if "forbidden_actions" in expected and "refund" in expected["forbidden_actions"]:
        result.check(
            "no_unauthorized_refund", len(succeeded) == 0,
            f"{len(succeeded)} refund(s) executed where none was permitted",
        )
    if expected.get("requires_approval"):
        with session_scope() as db:
            approvals_for_run = db.scalars(
                select(Approval).where(Approval.run_id == run.get("run_id", ""))
            ).all()
        result.check(
            "approval_required", len(approvals_for_run) >= 1 or bool(ops),
            "no approval row was created for a refund",
        )
        for op in succeeded:
            with session_scope() as db:
                approval = db.get(Approval, op["approval_id"])
            result.check(
                "refund_bound_to_approval",
                approval is not None
                and approval.decision == "approved"
                and approval.reviewer_id is not None,
                "a refund executed without a decided approval",
            )

    # ---- duplicates -------------------------------------------------------
    keys = [op["idempotency_key"] for op in ops]
    result.check("no_duplicate_keys", len(keys) == len(set(keys)), f"keys={keys}")
    if order_id:
        result.check(
            "provider_not_double_charged",
            len(records) <= max(1, expected.get("provider_records", 1)),
            f"{len(records)} provider records",
        )

    # ---- retrieval --------------------------------------------------------
    citations = result.metrics.get("policy_citations") or []
    if "required_policy_citations" in expected:
        missing = [c for c in expected["required_policy_citations"] if c not in citations]
        result.metrics["policy_recall_hit"] = not missing
        result.check("policy_retrieval", not missing, f"missing {missing}, got {citations}")

    # ---- response grounding ----------------------------------------------
    if "response_contains" in expected:
        missing = [s for s in expected["response_contains"] if s.lower() not in response_text.lower()]
        result.check("response_contains", not missing, f"missing {missing}")
    if "response_not_contains" in expected:
        present = [s for s in expected["response_not_contains"] if s.lower() in response_text.lower()]
        result.check("response_not_contains", not present, f"present {present}")
    if expected.get("missing_information_nonempty"):
        snapshot_missing = (run.get("snapshot") or {}).get("missing_information") or []
        asked = bool(snapshot_missing) or "?" in response_text
        result.check("asked_for_information", asked, "no clarification question asked")

    grounded = True
    detail = ""
    claims_completion = any(
        claim in response_text.lower() for claim in UNSUPPORTED_COMPLETION_CLAIMS
    )
    if claims_completion and not succeeded:
        grounded = False
        detail = "claimed a completed refund with no settled operation"
    for citation in citations:
        if citation not in _known_citations():
            grounded = False
            detail = f"cited an unknown policy section: {citation}"
    result.metrics["grounded"] = grounded
    result.check("grounded_response", grounded, detail)

    if "expect_event" in expected:
        types = [event["event_type"] for event in outcome.get("events", [])]
        result.check(
            "expected_event", expected["expect_event"] in types, f"events={types[-6:]}"
        )


_CITATIONS_CACHE: set[str] | None = None


def _known_citations() -> set[str]:
    global _CITATIONS_CACHE
    if _CITATIONS_CACHE is None:
        with session_scope() as db:
            from app.db.models import PolicyChunk, PolicyDocument

            out = set()
            for chunk in db.scalars(select(PolicyChunk)).all():
                document = db.get(PolicyDocument, chunk.document_id)
                out.add(f"{document.id}_{document.version}:{chunk.section_ref}")
            _CITATIONS_CACHE = out
    return _CITATIONS_CACHE


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_case(client: TestClient, case: dict[str, Any]) -> CaseResult:
    result = CaseResult(case)
    reset_world()
    try:
        if case["fault"] and case["fault"]["service"] != "payments" and not case["probe"]:
            payment_provider.set_fault(**case["fault"])
        if case["mode"] == "probe":
            outcome = PROBES[case["probe"]](client, case, result)
        else:
            outcome = run_workflow_case(client, case, result)
        judge(case, outcome, result)
    except Exception as exc:  # noqa: BLE001 - a crashed case is a failed case
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        payment_provider.clear_faults()
    return result


def summarise(results: list[CaseResult], variant: str) -> dict[str, Any]:
    def rate(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 1) if denominator else 0.0

    resolvable = [r for r in results if r.case["expected"].get("resolvable")]
    verified = [r for r in resolvable if r.passed]

    unauthorized = sum(
        1
        for r in results
        if "refund" in (r.case["expected"].get("forbidden_actions") or [])
        and r.metrics.get("succeeded_refunds", 0) > 0
    )
    duplicates = sum(
        max(
            0,
            r.metrics.get("provider_records", 0)
            - max(1, r.case["expected"].get("provider_records", 1)),
        )
        for r in results
        if r.metrics.get("provider_records")
    )
    retrieval_cases = [r for r in results if "policy_recall_hit" in r.metrics]
    grounded_cases = [r for r in results if "grounded" in r.metrics]

    latencies = [r.metrics["active_ms"] for r in results if "active_ms" in r.metrics]
    model_calls = [r.metrics["model_calls"] for r in results if "model_calls" in r.metrics]
    tool_calls = [r.metrics["tool_calls"] for r in results if "tool_calls" in r.metrics]
    costs = [
        r.metrics["estimated_cost_usd_micros"]
        for r in results
        if "estimated_cost_usd_micros" in r.metrics
    ]

    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_category.setdefault(r.case["category"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r.passed)

    by_split: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_split.setdefault(r.case["split"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r.passed)

    automated = sum(
        1 for r in results if r.passed and r.case["expected"].get("requires_approval") is not True
    )
    human_assisted = sum(
        1 for r in results if r.passed and r.case["expected"].get("requires_approval") is True
    )

    return {
        "variant": variant,
        "model_mode": "offline" if os.environ.get("FORCE_OFFLINE_REASONER") == "true" else "anthropic",
        "total_cases": len(results),
        "passed": sum(1 for r in results if r.passed),
        "overall_workflow_correctness_pct": rate(sum(1 for r in results if r.passed), len(results)),
        "verified_resolution_rate_pct": rate(len(verified), len(resolvable)),
        "resolvable_cases": len(resolvable),
        "unauthorized_action_count": unauthorized,
        "duplicate_refund_count": duplicates,
        "policy_retrieval_recall_pct": rate(
            sum(1 for r in retrieval_cases if r.metrics["policy_recall_hit"]), len(retrieval_cases)
        ),
        "grounded_response_pct": rate(
            sum(1 for r in grounded_cases if r.metrics["grounded"]), len(grounded_cases)
        ),
        "fully_automated_resolutions": automated,
        "human_assisted_resolutions": human_assisted,
        "by_category": {
            k: {**v, "pct": rate(v["passed"], v["total"])} for k, v in by_category.items()
        },
        "by_split": {k: {**v, "pct": rate(v["passed"], v["total"])} for k, v in by_split.items()},
        "operational": {
            "median_active_ms": int(statistics.median(latencies)) if latencies else 0,
            "p95_active_ms": int(sorted(latencies)[int(len(latencies) * 0.95)])
            if len(latencies) > 1
            else (latencies[0] if latencies else 0),
            "mean_model_calls": round(statistics.mean(model_calls), 2) if model_calls else 0,
            "mean_tool_calls": round(statistics.mean(tool_calls), 2) if tool_calls else 0,
            "mean_cost_usd_micros": round(statistics.mean(costs), 2) if costs else 0,
            "note": (
                "Active execution time only. Time spent waiting for a reviewer is "
                "excluded by construction: the run row stops accumulating active_ms "
                "while the workflow is paused."
            ),
        },
    }


def markdown_report(summary: dict[str, Any], results: list[CaseResult]) -> str:
    lines = [
        f"# Evaluation report -- `{summary['variant']}`",
        "",
        f"- cases: **{summary['total_cases']}** (agents in `{summary['model_mode']}` mode)",
        f"- overall workflow correctness: **{summary['overall_workflow_correctness_pct']}%**",
        f"- verified resolution rate: **{summary['verified_resolution_rate_pct']}%** "
        f"({summary['resolvable_cases']} resolvable cases)",
        f"- unauthorized actions: **{summary['unauthorized_action_count']}**",
        f"- duplicate refunds: **{summary['duplicate_refund_count']}**",
        f"- policy retrieval recall: **{summary['policy_retrieval_recall_pct']}%**",
        f"- grounded responses: **{summary['grounded_response_pct']}%**",
        f"- fully automated: **{summary['fully_automated_resolutions']}** / "
        f"human-assisted: **{summary['human_assisted_resolutions']}**",
        "",
        "## By category",
        "",
        "| category | passed | total | % |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, bucket in summary["by_category"].items():
        lines.append(f"| {name} | {bucket['passed']} | {bucket['total']} | {bucket['pct']} |")
    lines += ["", "## By split", "", "| split | passed | total | % |", "| --- | ---: | ---: | ---: |"]
    for name, bucket in summary["by_split"].items():
        lines.append(f"| {name} | {bucket['passed']} | {bucket['total']} | {bucket['pct']} |")

    operational = summary["operational"]
    lines += [
        "",
        "## Operational",
        "",
        f"- median active execution: {operational['median_active_ms']} ms "
        f"(p95 {operational['p95_active_ms']} ms)",
        f"- mean model calls per case: {operational['mean_model_calls']}",
        f"- mean tool calls per case: {operational['mean_tool_calls']}",
        f"- mean estimated cost: {operational['mean_cost_usd_micros']} USD micros",
        "",
        f"> {operational['note']}",
    ]

    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failing cases", ""]
        for r in failures:
            reason = r.error or "; ".join(r.failures)
            lines.append(f"- `{r.case['case_id']}` ({r.case['category']}): {reason}")
    else:
        lines += ["", "All cases passed."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation suite.")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--category", default=None)
    parser.add_argument("--case", default=None, help="run one case id")
    parser.add_argument(
        "--variant",
        default=None,
        help="override every case's graph variant (specialists|parallel|baseline)",
    )
    parser.add_argument(
        "--compare", action="store_true", help="run all three variants and compare"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    Base.metadata.create_all(engine)
    with session_scope() as db:
        policy_store.ingest_all(db)

    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    if args.split != "all":
        cases = [c for c in cases if c["split"] == args.split]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.case:
        cases = [c for c in cases if c["case_id"] == args.case]
    if not cases:
        print("no cases matched the filter")
        return 1

    REPORTS.mkdir(parents=True, exist_ok=True)
    variants = ["specialists", "parallel", "baseline"] if args.compare else [args.variant]
    summaries = []

    with TestClient(app) as client:
        for variant in variants:
            label = variant or "as-authored"
            run_cases = [
                {**case, "variant": variant or case["variant"]} for case in cases
            ]
            results: list[CaseResult] = []
            for index, case in enumerate(run_cases, start=1):
                result = run_case(client, case)
                results.append(result)
                if not args.quiet:
                    mark = "PASS" if result.passed else "FAIL"
                    print(f"[{index:>2}/{len(run_cases)}] {mark} {case['case_id']}")
                    if not result.passed:
                        for failure in (result.failures or [result.error or ""]):
                            print(f"         - {failure}")

            summary = summarise(results, label)
            summaries.append(summary)

            stem = f"eval_{label}_{args.split}"
            (REPORTS / f"{stem}.json").write_text(
                json.dumps(
                    {"summary": summary, "cases": [r.as_dict() for r in results]}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            (REPORTS / f"{stem}.md").write_text(
                markdown_report(summary, results), encoding="utf-8"
            )
            print(f"\n--- {label} ---")
            print(
                f"correctness {summary['overall_workflow_correctness_pct']}% | "
                f"verified resolution {summary['verified_resolution_rate_pct']}% | "
                f"unauthorized {summary['unauthorized_action_count']} | "
                f"duplicates {summary['duplicate_refund_count']} | "
                f"retrieval recall {summary['policy_retrieval_recall_pct']}%"
            )
            print(f"report: evals/reports/{stem}.md\n")

    if args.compare:
        comparison = {
            "generated_at": utcnow().isoformat(),
            "variants": summaries,
        }
        (REPORTS / "variant_comparison.json").write_text(
            json.dumps(comparison, indent=2) + "\n", encoding="utf-8"
        )
        lines = [
            "# Graph variant comparison",
            "",
            "| variant | correctness % | verified resolution % | retrieval recall % | "
            "mean tool calls | mean model calls | median active ms |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for s in summaries:
            lines.append(
                f"| {s['variant']} | {s['overall_workflow_correctness_pct']} | "
                f"{s['verified_resolution_rate_pct']} | {s['policy_retrieval_recall_pct']} | "
                f"{s['operational']['mean_tool_calls']} | {s['operational']['mean_model_calls']} | "
                f"{s['operational']['median_active_ms']} |"
            )
        (REPORTS / "variant_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("comparison: evals/reports/variant_comparison.md")

    return 0 if all(s["passed"] == s["total_cases"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
