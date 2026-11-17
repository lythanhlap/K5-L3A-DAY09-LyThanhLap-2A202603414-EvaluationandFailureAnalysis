"""Shape B — one ReAct agent. Student work.

Use `lab8.runtime.run_react` with max_steps=8. `decide` may pick a read tool.
It must not call `issue_refund`. After the loop, the host validates the quote
and is the only writer.

A decide() that trusts ticket text over the order row will fail hidden claims
where the customer overstates the delay or cites a superseded clause.

Per-order cap: an order may already have been refunded by an earlier claim.
The read tool `lookup_refund_history` shows how much. The host still enforces
the cap, so pass the full quote to `durability.issue_refund`, not a net amount.

Caps: stop at 8 steps. Count model calls in the Budget so cost stays comparable.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.

How this shape splits the work between model and code:

- Agent (`_Agent.decide`, run by run_react with worker=True, so only read tools
  get through): each step costs one MockLLM call, then the policy names the next
  read tool or stops. run_react hands back only the latest tool result, so the
  agent keeps its own memory in `seen`.
- Code binds every tool argument from trusted values (the assigned claim_id, then
  the claim view). The model never writes an id, so it cannot aim a lookup at
  another claim, order or principal.
- Code decides after the loop: status from the evidence, not from the agent's
  final answer; quote from compute_compensation on the order view the host
  returned; FULL quote into durability.issue_refund with crash_at; InjectedCrash
  is not caught.
- Same three read tools and same draft call as the workflow, so the benchmark gap
  between A and B is the ReAct loop itself.
- The trace keeps step numbers, tool names and allowlisted order fields. Ticket
  text, claim rows and order rows never reach the trace, the result or stdout.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lab8 import durability
from lab8.db import connect
from lab8.mock_llm import complete
from lab8.runtime import dispatch, run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.trace import Trace

SHAPE = "single"
MAX_STEPS = 8
STOP = "stop"
# Order fields the trace may show. lookup_order already drops phone, address, note and item.
TRACED_ORDER_FIELDS = ("order_id", "status", "delay_days", "shipping_fee_vnd", "damage_verified")


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


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
        # Same draft call as the workflow: reply text only, never the amount.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status} proposal={int(proposal)}")
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

    # 1. Bounded ReAct loop. The agent only gathers evidence with read tools.
    agent = _Agent(claim_id, seed=seed, budget=budget, trace=trace)
    loop = run_react(
        conn,
        budget,
        observation={"claim": {"claim_id": claim_id}},
        decide=agent.decide,
        max_steps=MAX_STEPS,
        worker=True,
    )
    if loop["stopped"] != "final":
        return done("failed", 0, "step_cap")

    # 2. Code decides from the evidence. The agent's final answer is not trusted.
    claim = agent.seen.get("get_claim_view") or {}
    if not claim.get("found"):
        return done("failed", 0, "unknown_claim")
    if not claim.get("order_id"):
        return done("needs_info", 0, "missing_order_id")
    order_view = agent.seen.get("lookup_order")
    if order_view is None:
        return done("failed", 0, "order_not_checked")  # fail closed: no refund without evidence
    if not order_view["visible"]:
        return done("escalated", 0, order_view["reason"])

    # 3. Quote in code, from the order view the host returned, never from model-written numbers.
    quote = dispatch(conn, budget, "compute_compensation", {"order": order_view["order"]}, worker=False)
    proposal = int(quote["amount_vnd"])
    trace.add(
        "quote",
        {"amount_vnd": proposal, "reason": quote["reason"], "clause_ids": list(quote["clause_ids"])},
    )
    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    # 4. Host gate: FULL quote in; the host checks approval, per-order cap and idempotency.
    #    InjectedCrash is not caught: flush the trace and let it propagate.
    try:
        outcome = durability.issue_refund(conn, claim_id, proposal, crash_at=crash_at)
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
        return done("failed" if outcome["conflict"] else "denied", proposal, outcome["reason"])
    return done("resolved", proposal, None)


class _Agent:
    """Bounded ReAct policy: names the next read tool or stops. Never sees parent_secret or headers."""

    def __init__(self, claim_id: str, *, seed: int, budget: Budget, trace: Trace) -> None:
        self.claim_id = claim_id
        self.seed = seed
        self.budget = budget
        self.trace = trace
        self.seen: dict[str, dict[str, Any]] = {}  # run_react keeps only the latest result
        self.step = 0

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        last_tool = observation.get("last_tool")
        if last_tool:
            self.seen[last_tool] = observation["last_result"]
        self.step += 1
        # One model call per step: the price of the loop. MockLLM text never drives a decision.
        complete(self.budget, seed=self.seed, purpose="react", prompt=self._prompt(observation))
        choice = self._policy()
        self.trace.add(
            "react_step",
            {"step": self.step, "saw": _summary(last_tool, observation.get("last_result")), "next": choice},
        )
        if choice == STOP:
            return {"final": {"stop_after_step": self.step}}
        return {"tool": choice, "args": self._bind(choice)}

    def _policy(self) -> str:
        """Next read the evidence still needs. Ticket text is never consulted."""
        claim = self.seen.get("get_claim_view")
        if claim is None:
            return "get_claim_view"
        if not claim.get("found") or not claim.get("order_id"):
            return STOP  # nothing to look up; code returns failed / needs_info
        if "lookup_order" not in self.seen:
            return "lookup_order"
        return STOP

    def _bind(self, tool: str) -> dict[str, Any]:
        """Arguments come from trusted values, never from model text or the ticket."""
        if tool == "get_claim_view":
            return {"claim_id": self.claim_id}
        claim = self.seen["get_claim_view"]
        return {"order_id": claim["order_id"], "principal_id": claim["principal_id"]}

    def _prompt(self, observation: dict[str, Any]) -> str:
        # The agent reads the ticket like a real model would. This text only goes to MockLLM.
        ticket = (self.seen.get("get_claim_view") or {}).get("ticket", "")
        return json.dumps(
            {
                "ticket": ticket,
                "last_tool": observation.get("last_tool"),
                "last_result": observation.get("last_result"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _summary(tool: str | None, result: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the trace may say about a tool result: flags and allowlisted order fields only."""
    if not tool or result is None:
        return None
    if tool == "get_claim_view":
        return {"tool": tool, "found": bool(result.get("found")), "has_order_id": bool(result.get("order_id"))}
    order = result.get("order") or {}
    return {
        "tool": tool,
        "visible": bool(result.get("visible")),
        "reason": result.get("reason"),
        **{key: order[key] for key in TRACED_ORDER_FIELDS if key in order},
    }
