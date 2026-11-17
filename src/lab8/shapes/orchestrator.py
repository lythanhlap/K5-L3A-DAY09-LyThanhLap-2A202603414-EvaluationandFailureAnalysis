
"""Shape C — orchestrator with isolated read-only investigators."""

from __future__ import annotations

import sqlite3

from lab8 import durability, tools
from lab8.db import connect
from lab8.handoff import make_handoff
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.trace import Trace

SHAPE = "orchestrator"


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))

    def done(
        status: str,
        proposal: int,
        reason: str | None,
    ) -> RunResult:
        trace.add(
            "decision",
            {
                "status": status,
                "proposal_amount": int(proposal),
                "reason": reason,
            },
        )
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # 1. Read the claim through the allowlisted tool.
    # Never trace the full claim: it contains ticket and parent context.
    budget.add_tool(tools.tool_cost())
    claim = tools.get_claim_view(conn, claim_id)

    if not claim["found"]:
        trace.add("claim", {"found": False})
        return done("failed", 0, "unknown_claim")

    trace.add(
        "claim",
        {
            "found": True,
            "has_order_id": bool(claim.get("order_id")),
        },
    )

    if not claim.get("order_id"):
        return done("needs_info", 0, "missing_order_id")

    # 2. Build a strictly allowlisted handoff.
    # No ticket, parent_secret, raw headers, or full claim row.
    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task="verify_order_and_calculate_compensation",
        facts={},
        allowed_tools=[
            "lookup_order",
            "lookup_policy",
            "compute_compensation",
        ],
    )

    trace.add("handoff", safe_dump(handoff))

    # 3. Run investigators with read-only tools only.
    investigated = run_readonly_investigators(
        conn,
        budget,
        handoff,
        principal_id=claim["principal_id"],
    )

    order_view = investigated["tools"].get("lookup_order", {})

    if not order_view.get("visible"):
        trace.add(
            "investigator",
            {
                "lookup_order": {
                    "visible": False,
                    "reason": order_view.get("reason"),
                },
                "policy_count": len(
                    (investigated["tools"].get("lookup_policy") or {}).get(
                        "policies"
                    ) or []
                ),
            },
        )
        return done(
            "escalated",
            0,
            order_view.get("reason", "order_not_visible"),
        )

    # lookup_order returns an allowlisted order view.
    order = order_view["order"]

    trace.add(
        "investigator",
        {
            "lookup_order": {
                "visible": True,
                "order": {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "delay_days": order["delay_days"],
                    "shipping_fee_vnd": order["shipping_fee_vnd"],
                    "paid_vnd": order["paid_vnd"],
                    "damage_verified": order["damage_verified"],
                    "carrier": order["carrier"],
                },
            },
            "policy_count": len(
                (investigated["tools"].get("lookup_policy") or {}).get(
                    "policies"
                ) or []
            ),
        },
    )

    # 4. The orchestrator owns the business decision.
    # Investigators cannot issue refunds.
    quote = investigated["tools"].get("compute_compensation")

    if not quote:
        return done("failed", 0, "missing_compensation_quote")

    proposal = int(quote["amount_vnd"])

    trace.add(
        "quote",
        {
            "amount_vnd": proposal,
            "reason": quote["reason"],
            "clause_ids": list(quote["clause_ids"]),
            "parts": dict(quote.get("parts") or {}),
        },
    )

    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    # 5. Only the orchestrator/host can perform the write.
    # Pass the FULL quote. The host handles approval, order cap,
    # previous refunds, and idempotency.
    try:
        outcome = durability.issue_refund(
            conn,
            claim_id,
            proposal,
            crash_at=crash_at,
        )
    except durability.InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise

    trace.add(
        "issue_refund",
        {
            "ok": outcome["ok"],
            "reason": outcome["reason"],
            "conflict": outcome["conflict"],
            "amount_vnd": (outcome["effect"] or {}).get("amount_vnd"),
        },
    )

    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return done(status, proposal, outcome["reason"])

    return done("resolved", proposal, None)
