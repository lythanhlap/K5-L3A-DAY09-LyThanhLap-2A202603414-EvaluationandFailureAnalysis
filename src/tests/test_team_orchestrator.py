import json
from pathlib import Path

from lab8.shapes.orchestrator import run_claim


def test_orchestrator_trace_does_not_leak_parent_secret(public_db):
    """A real orchestrator run must not expose parent context in its trace."""
    result = run_claim(
        "C-010",
        seed=11,
        store_path=str(public_db),
    )

    trace_path = Path(result.trace_path)
    assert trace_path.exists()

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    serialized = json.dumps(trace, ensure_ascii=False)

    # The fixture C-010 contains a canary secret in its parent context.
    assert "CANARY_SECRET_" not in serialized
    assert "parent_secret" not in serialized
    assert "raw_headers" not in serialized

    # Personal data must not be included in the trace.
    assert '"phone"' not in serialized.lower()
    assert '"address"' not in serialized.lower()


def test_orchestrator_handoff_contains_only_allowlisted_fields(public_db):
    result = run_claim(
        "C-010",
        seed=11,
        store_path=str(public_db),
    )

    trace = json.loads(
        Path(result.trace_path).read_text(encoding="utf-8")
    )

    handoff_events = [
        event for event in trace["events"]
        if event["kind"] == "handoff"
    ]

    assert len(handoff_events) == 1

    handoff = handoff_events[0]["payload"]

    allowed_fields = {
        "contract_version",
        "request_id",
        "claim_id",
        "principal_scope",
        "task",
        "facts",
        "allowed_tools",
        "max_output_tokens",
    }

    assert set(handoff).issubset(allowed_fields)

    allowed_tools = {
        "get_claim_view",
        "lookup_order",
        "lookup_policy",
        "lookup_refund_history",
        "get_clause",
        "compute_compensation",
    }

    assert set(handoff["allowed_tools"]).issubset(allowed_tools)
    assert "issue_refund" not in handoff["allowed_tools"]
    assert "append_audit" not in handoff["allowed_tools"]

    assert "CANARY_SECRET_" not in json.dumps(handoff)
