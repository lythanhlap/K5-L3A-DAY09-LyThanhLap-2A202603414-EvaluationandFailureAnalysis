import json
from pathlib import Path

import pytest

from lab8.db import connect
from lab8.durability import committed_audit_count, get_refund
from lab8.shapes.workflow import run_claim as run_workflow
from lab8.shapes.single import run_claim as run_single
from lab8.shapes.orchestrator import run_claim as run_orchestrator


SHAPES = [
    ("workflow", run_workflow),
    ("single", run_single),
    ("orchestrator", run_orchestrator),
]


@pytest.mark.parametrize("shape_name,runner", SHAPES)
@pytest.mark.parametrize(
    "claim_id,approval_id,expected_reason",
    [
        ("C-NOAPPROVAL-AUDIT", None, "missing_approval"),
        ("C-BORROW-AUDIT", "A-001", "approval_claim_mismatch"),
    ],
)
def test_all_shapes_reject_invalid_approval(
    public_db,
    shape_name,
    runner,
    claim_id,
    approval_id,
    expected_reason,
):
    # A-001 belongs to C-001, not to the new claim.
    conn = connect(public_db)
    conn.execute(
        """
        INSERT INTO claims (
            claim_id, principal_id, order_id, ticket,
            parent_secret, raw_headers_json, approval_id
        )
        VALUES (?, 'P-100', 'O-107', 'security audit',
                'CANARY_SECRET_AUDIT', '{}', ?)
        """,
        (claim_id, approval_id),
    )
    conn.commit()
    conn.close()

    result = runner(
        claim_id,
        seed=11,
        store_path=str(public_db),
    ).as_dict()

    assert result["status"] == "denied", (
        f"{shape_name} accepted invalid approval: {result}"
    )

    conn = connect(public_db)
    try:
        assert get_refund(conn, claim_id) is None
        assert committed_audit_count(conn, claim_id) == 0

        denied_events = conn.execute(
            """
            SELECT details_json
            FROM audit_events
            WHERE claim_id = ? AND event_type = 'policy_denied'
            """,
            (claim_id,),
        ).fetchall()

        assert any(
            expected_reason in row["details_json"]
            for row in denied_events
        ), f"{shape_name} did not record {expected_reason}"
    finally:
        conn.close()


@pytest.mark.parametrize("shape_name,runner", SHAPES)
def test_all_shapes_keep_secrets_and_personal_data_out_of_trace(
    public_db,
    shape_name,
    runner,
):
    result = runner(
        "C-010",
        seed=11,
        store_path=str(public_db),
    )

    trace_path = Path(result.trace_path)
    assert trace_path.exists(), f"{shape_name} did not write a trace"

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    serialized = json.dumps(trace, ensure_ascii=False).lower()

    forbidden = [
        "canary_secret_",
        "parent_secret",
        "raw_headers",
        '"phone"',
        '"address"',
        "internal_note",
        "item_name",
    ]

    for value in forbidden:
        assert value.lower() not in serialized, (
            f"{shape_name} trace leaked forbidden data: {value}"
        )
