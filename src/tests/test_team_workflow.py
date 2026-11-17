"""Team tests for Shape A — deterministic workflow (lab8/shapes/workflow.py).

Runs in the default `pytest` (no `student` marker), unlike tests/test_public_matrix.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lab8.db import connect, init_store
from lab8.durability import InjectedCrash
from lab8.metrics import check_trace, trial_pass
from lab8.shapes.workflow import SHAPE, run_claim
from tests.conftest import BY_ID, CLAIMS, WORLD

SEEDS = [11, 22, 33, 44, 55]
SINGLE_RUN_CLAIMS = [f"C-{i:03d}" for i in range(1, 11)] + ["C-013", "C-014"]


def _store(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    init_store(path, WORLD, CLAIMS).close()
    return path


def _trace(result) -> dict:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


def _kinds(result) -> list[str]:
    return [event["kind"] for event in _trace(result)["events"]]


def _ledger_count(store: Path, claim_id: str) -> int:
    conn = connect(store)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM refund_ledger WHERE claim_id = ?", (claim_id,)
        ).fetchone()["n"]
    finally:
        conn.close()


# --- Public matrix T01–T10, T13, T14 on every seed --------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("claim_id", SINGLE_RUN_CLAIMS)
def test_public_cases_pass_on_every_seed(tmp_path, claim_id, seed):
    store = _store(tmp_path, f"{claim_id}-{seed}.sqlite")
    started_at = time.time()
    result = run_claim(claim_id, seed=seed, store_path=str(store))
    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert passed, reason
    assert result.shape == SHAPE


@pytest.mark.parametrize("claim_id", SINGLE_RUN_CLAIMS)
def test_workflow_is_deterministic_across_seeds(tmp_path, claim_id):
    outcomes = set()
    for seed in SEEDS:
        store = _store(tmp_path, f"det-{claim_id}-{seed}.sqlite")
        result = run_claim(claim_id, seed=seed, store_path=str(store))
        outcomes.add((result.status, result.proposal_amount, result.error_reason))
    assert len(outcomes) == 1, outcomes


# --- T11 retry and T12 crash -------------------------------------------------------


def test_t11_retry_is_idempotent(tmp_path):
    store = _store(tmp_path, "t11.sqlite")
    first = run_claim("C-011", seed=11, store_path=str(store))
    second = run_claim("C-011", seed=11, store_path=str(store))
    assert first.status == second.status == "resolved"
    assert first.proposal_amount == second.proposal_amount == 35000
    assert _ledger_count(store, "C-011") == 1
    assert second.committed_audit_count == 1


def test_t12_crash_propagates_and_rerun_does_not_double_pay(tmp_path):
    store = _store(tmp_path, "t12.sqlite")
    with pytest.raises(InjectedCrash):
        run_claim("C-012", seed=11, store_path=str(store), crash_at="after_effect_before_checkpoint")
    started_at = time.time()
    result = run_claim("C-012", seed=11, store_path=str(store))
    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID["C-012"]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert passed, reason
    assert _ledger_count(store, "C-012") == 1


def test_crash_still_flushes_trace(tmp_path):
    store = _store(tmp_path, "t12-trace.sqlite")
    with pytest.raises(InjectedCrash):
        run_claim("C-012", seed=11, store_path=str(store), crash_at="after_effect_before_checkpoint")
    from lab8.schemas import make_run_id
    from lab8.trace import Trace

    path = Trace(make_run_id(SHAPE, "C-012", 11)).path
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["events"][-1] == {"kind": "crash", "payload": {"at": "after_effect_before_checkpoint"}}


# --- Branch-specific behaviour ------------------------------------------------------


def test_unknown_claim_fails_without_ledger(tmp_path):
    store = _store(tmp_path, "unknown.sqlite")
    result = run_claim("C-999", seed=11, store_path=str(store))
    assert result.status == "failed"
    assert result.error_reason == "unknown_claim"
    assert result.proposal_amount == 0
    assert _ledger_count(store, "C-999") == 0
    assert check_trace(result.as_dict())[0]


def test_missing_order_id_stops_before_lookup(tmp_path):
    store = _store(tmp_path, "c004.sqlite")
    result = run_claim("C-004", seed=11, store_path=str(store))
    assert result.status == "needs_info"
    assert result.error_reason == "missing_order_id"
    assert "lookup_order" not in _kinds(result)
    assert "issue_refund" not in _kinds(result)


def test_order_outside_scope_is_escalated_without_leaking_pii(tmp_path):
    store = _store(tmp_path, "c005.sqlite")
    result = run_claim("C-005", seed=11, store_path=str(store))
    assert result.status == "escalated"
    assert "quote" not in _kinds(result)
    blob = json.dumps(result.as_dict(), ensure_ascii=False) + Path(result.trace_path).read_text(encoding="utf-8")
    for secret in BY_ID["C-005"]["expected"]["forbid_substrings"]:
        assert secret not in blob


def test_zero_quote_denied_without_host_call(tmp_path):
    store = _store(tmp_path, "c002.sqlite")
    result = run_claim("C-002", seed=55, store_path=str(store))
    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert "issue_refund" not in _kinds(result)


def test_policy_denied_keeps_full_quote(tmp_path):
    store = _store(tmp_path, "c013.sqlite")
    result = run_claim("C-013", seed=11, store_path=str(store))
    assert result.status == "denied"
    assert result.proposal_amount == 35000
    assert _ledger_count(store, "C-013") == 0


def test_t14_passes_full_quote_and_host_pays_difference(tmp_path):
    store = _store(tmp_path, "c014.sqlite")
    result = run_claim("C-014", seed=11, store_path=str(store))
    assert result.status == "resolved"
    assert result.proposal_amount == 150000
    refund = [e for e in _trace(result)["events"] if e["kind"] == "issue_refund"][0]["payload"]
    assert refund["ok"] is True
    assert refund["amount_vnd"] == 120000


# --- Trace contract and model usage ------------------------------------------------


@pytest.mark.parametrize("claim_id", ["C-001", "C-004", "C-005", "C-007"])
def test_trace_contract_and_no_ticket_text(tmp_path, claim_id):
    store = _store(tmp_path, f"trace-{claim_id}.sqlite")
    started_at = time.time()
    result = run_claim(claim_id, seed=11, store_path=str(store))
    ok, reason = check_trace(result.as_dict(), started_at=started_at)
    assert ok, reason
    body = _trace(result)
    assert body["run_id"] == result.run_id
    assert _kinds(result)[0] == "claim"
    assert _kinds(result)[-1] == "decision"
    raw = Path(result.trace_path).read_text(encoding="utf-8")
    assert BY_ID[claim_id]["ticket"] not in raw
    assert BY_ID[claim_id]["parent_secret"] not in raw


@pytest.mark.parametrize("claim_id", ["C-001", "C-004", "C-007"])
def test_model_only_drafts_once(tmp_path, claim_id):
    store = _store(tmp_path, f"llm-{claim_id}.sqlite")
    result = run_claim(claim_id, seed=11, store_path=str(store))
    assert result.model_calls == 1
    decision = _trace(result)["events"][-1]["payload"]
    assert decision["status"] == result.status
    assert decision["proposal_amount"] == result.proposal_amount
