"""Regression tests born from docs/trace_case.md (trace -> first wrong step -> test).

Case 1: seeded_regression C-002@11. First wrong step = event 3 `model_override {"delay_days": 5}`:
the ticket's "trễ 5 ngày" overwrote the DB fact (on time), so money moved on a claim that must be
denied (FM-03). Case 2: seeded_regression C-008@11, reply states 35.000đ while the ledger holds the
approval-capped 20.000đ (FM-08). Each must FAIL on the buggy variant and PASS on `fixed`.
"""
import pytest

pytestmark = pytest.mark.student


def _grade(shape: str, claim: str, seed: int = 11) -> dict[str, bool]:
    from pydantic_evals.evaluators import EvaluatorContext

    from lab9.graders import DbFactKept, RefundAmountCorrect, ReplyMatchesLedger, TrialPass
    from lab9.trace_case import investigate

    case = investigate(shape, claim, seed)
    ctx = EvaluatorContext(name="t", inputs={}, metadata=None, expected_output=case["label"],
                           output=case["trial"], duration=0.0, _span_tree=None, attributes={}, metrics={})
    return {g.__name__: bool(g().evaluate(ctx).value)
            for g in (DbFactKept, RefundAmountCorrect, ReplyMatchesLedger, TrialPass)}


def test_lab9_trace_case_ticket_override_is_caught():
    bad = _grade("seeded_regression", "C-002")
    assert bad["DbFactKept"] is False          # event 3 model_override: the first wrong step
    assert bad["RefundAmountCorrect"] is False  # consequence: 20.000đ in the ledger, label 0
    assert bad["TrialPass"] is False


def test_lab9_trace_case_ticket_override_fixed_passes():
    ok = _grade("fixed", "C-002")
    assert ok == {"DbFactKept": True, "RefundAmountCorrect": True, "ReplyMatchesLedger": True, "TrialPass": True}


def test_lab9_trace_case_misreport_is_caught():
    assert _grade("seeded_regression", "C-008")["ReplyMatchesLedger"] is False
    assert _grade("fixed", "C-008")["ReplyMatchesLedger"] is True
