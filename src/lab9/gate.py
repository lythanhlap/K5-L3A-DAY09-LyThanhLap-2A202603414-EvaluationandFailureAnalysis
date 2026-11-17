"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems = [f"PR {p}" for p in _report_problems(pr, spec)]
    problems += [f"baseline {p}" for p in _report_problems(base, spec)]
    problems += _compare_with_baseline(pr, base)
    if base_spec is not None:
        problems += ratchet_check(spec, base_spec)
    return problems


# ---------------------------------------------------------------- M7 helpers
_SAFETY_SUMS = ("secret_leak", "approval_bypass")   # lab9.eval.aggregate counts these, others are means


def _bad_number(v: object) -> bool:
    return isinstance(v, float) and not math.isfinite(v)


def _graded(spec: dict, rows: list[dict]) -> list[str]:
    """Spec metrics a grader writes into every row (pass_5, cost... are derived, not graded)."""
    from lab9.graders import GRADERS
    keys = set().union(*(r.keys() for r in rows)) if rows else set()
    return [m["id"] for m in spec.get("metrics", []) if m["id"] in GRADERS or m["id"] in keys]


def _recompute(rows: list[dict], metric: str) -> float | int | None:
    vals = [r.get(metric) for r in rows if r.get(metric) is not None]
    if metric in _SAFETY_SUMS:
        return sum(bool(v) for v in vals)
    return round(sum(float(v) for v in vals) / len(vals), 4) if vals else None


def _derived(rows: list[dict], name: str) -> float | None:
    """pass_5 / pass_at_k / pass_hat_k recomputed from the rows (same rules as lab9.eval.aggregate)."""
    by_item: dict[str, list[bool]] = {}
    for r in rows:
        by_item.setdefault(r["item_id"], []).append(bool(r.get("pass_rate")))
    if not by_item:
        return None
    if name == "pass_5":
        return round(sum(all(v) for v in by_item.values()) / len(by_item), 4)
    kind, _, k = name.rpartition("_")
    fn = {"pass_at": stats.pass_at_k, "pass_hat": stats.pass_hat_k}.get(kind)
    if fn is None or not k.isdigit() or min(len(v) for v in by_item.values()) < int(k):
        return None
    try:
        est = [fn(len(v), sum(v), int(k)) for v in by_item.values()]
    except (NotImplementedError, ValueError):
        return None
    return round(sum(est) / len(est), 4)


def _report_problems(rep: dict, spec: dict) -> list[str]:
    """Problems a single report has on its own: broken numbers, broken graders, bad row set,
    and headline metrics that do not follow from its own rows (edited or partly recomputed)."""
    out: list[str] = []
    rows = rep.get("rows") or []
    metrics = rep.get("metrics") or {}
    manifest = rep.get("manifest") or {}
    for k, v in metrics.items():
        if _bad_number(v):
            out.append(f"metric {k} is {v} (NaN/inf never passes a gate)")
    if rep.get("evaluator_failures"):
        out.append(f"{len(rep['evaluator_failures'])} evaluator failure(s), first: {rep['evaluator_failures'][0]}")
    if not rows:
        out.append("has no rows")
        return out
    seeds = manifest.get("seeds") or []
    n_items = manifest.get("items")
    if not isinstance(n_items, int) or n_items * len(seeds) != len(rows):
        out.append(f"has {len(rows)} rows, manifest says {n_items} items x {len(seeds)} seeds")
    pairs = [(r.get("item_id"), r.get("seed")) for r in rows]
    dup = len(pairs) - len(set(pairs))
    if dup:
        out.append(f"has {dup} duplicated (item_id, seed) row(s)")
    if seeds and {p[1] for p in pairs} != set(seeds):
        out.append(f"row seeds {sorted({p[1] for p in pairs})} differ from manifest seeds {sorted(seeds)}")
    items = {p[0] for p in pairs}
    if isinstance(n_items, int) and len(items) != n_items:
        out.append(f"rows cover {len(items)} items, manifest says {n_items}")
    if spec.get("seeds") and list(seeds) != list(spec["seeds"]):
        out.append(f"seeds {seeds} differ from the spec's {spec['seeds']}")
    for mid in _graded(spec, rows):
        missing = sum(1 for r in rows if r.get(mid) is None or _bad_number(r.get(mid)))
        if missing:
            out.append(f"{missing} row(s) have no value for graded metric {mid} (crashed grader?)")
        elif mid not in metrics or metrics[mid] is None:
            out.append(f"graded metric {mid} is in the rows but missing from metrics")
    for mid, v in metrics.items():
        if v is None or _bad_number(v) or not isinstance(v, (int, float)):
            continue
        if mid in _graded(spec, rows):
            want = _recompute(rows, mid)
        elif mid == "pass_5" or mid.startswith(("pass_at_", "pass_hat_")):
            want = _derived(rows, mid)
        else:
            continue
        if want is not None and not math.isclose(float(v), float(want), abs_tol=1e-3):
            out.append(f"metric {mid}={v} does not match its rows ({want})")
    for k in spec.get("pass_k") or []:
        for name in (f"pass_at_{k}", f"pass_hat_{k}"):
            if name in {m["id"] for m in spec.get("metrics", [])} and metrics.get(name) is None:
                out.append(f"metric {name} is missing")
    return out


def _compare_with_baseline(pr: dict, base: dict) -> list[str]:
    out: list[str] = []
    pm, bm = pr.get("manifest") or {}, base.get("manifest") or {}
    for key in ("eval_set", "eval_set_sha", "items", "seeds"):
        if key in bm and pm.get(key) != bm.get(key):
            out.append(f"manifest {key} {pm.get(key)!r} differs from the baseline's {bm.get(key)!r}")
    pp = {(r.get("item_id"), r.get("seed")) for r in pr.get("rows") or []}
    bp = {(r.get("item_id"), r.get("seed")) for r in base.get("rows") or []}
    if pp != bp:
        out.append(f"(item, seed) set differs from the baseline's: {len(bp - pp)} missing, {len(pp - bp)} extra")
    pmid = {k for r in pr.get("rows") or [] for k in r}
    bmid = {k for r in base.get("rows") or [] for k in r}
    if bmid - pmid:
        out.append(f"rows lost columns the baseline has: {sorted(bmid - pmid)}")
    tags_b: dict[str, set] = {}
    for r in base.get("rows") or []:
        tags_b.setdefault(r.get("item_id"), set()).add(tuple(sorted(r.get("slices") or [])))
    tags_p: dict[str, set] = {}
    for r in pr.get("rows") or []:
        tags_p.setdefault(r.get("item_id"), set()).add(tuple(sorted(r.get("slices") or [])))
    for item in sorted(set(tags_b) & set(tags_p)):
        if tags_b[item] != tags_p[item]:
            out.append(f"{item} slice tags {sorted(tags_p[item])} differ from the baseline's {sorted(tags_b[item])}")
    return out


def _loosened(where: str, th: dict, bth: dict) -> list[str]:
    out = []
    for key, old in bth.items():
        new = th.get(key)
        if key == "no_significant_drop":
            if old and not new:
                out.append(f"{where}: no_significant_drop removed")
        elif new is None:
            out.append(f"{where}: threshold {key} removed")
        elif key in ("max", "max_increase") and float(new) > float(old):
            out.append(f"{where}: {key} raised {old} -> {new}")
        elif key == "min" and float(new) < float(old):
            out.append(f"{where}: min lowered {old} -> {new}")
    return out


def ratchet_check(spec: dict, base_spec: dict) -> list[str]:
    """Same eval-set version => thresholds may only get stricter. A new eval-set version starts over."""
    if spec.get("eval_set_version") != base_spec.get("eval_set_version"):
        return []
    out: list[str] = []
    for key in ("eval_set", "labels", "adversarial_pack"):
        if base_spec.get(key) and spec.get(key) != base_spec.get(key):
            out.append(f"RATCHET {key} changed on the same eval-set version")
    if base_spec.get("seeds") and set(spec.get("seeds") or []) < set(base_spec["seeds"]):
        out.append("RATCHET seeds dropped on the same eval-set version")
    if set(base_spec.get("pass_k") or []) - set(spec.get("pass_k") or []):
        out.append("RATCHET pass_k values dropped")
    bconf = (base_spec.get("bootstrap") or {}).get("confidence")
    conf = (spec.get("bootstrap") or {}).get("confidence")
    if bconf is not None and (conf is None or float(conf) > float(bconf)):
        out.append(f"RATCHET bootstrap confidence {bconf} -> {conf} (wider CI blocks less)")
    now = {m["id"]: m for m in spec.get("metrics", [])}
    for bm in base_spec.get("metrics", []):
        m = now.get(bm["id"])
        if m is None:
            out.append(f"RATCHET metric {bm['id']} removed")
            continue
        if bm.get("blocking") and not m.get("blocking"):
            out.append(f"RATCHET metric {bm['id']} no longer blocking")
        out += [f"RATCHET {x}" for x in _loosened(bm["id"], m.get("threshold") or {}, bm.get("threshold") or {})]
    bir, ir = base_spec.get("item_regression"), spec.get("item_regression")
    if bir:
        if not ir or ir.get("metric", "pass_rate") != bir.get("metric", "pass_rate"):
            out.append("RATCHET item_regression removed or moved to another metric")
        else:
            if bir.get("blocking") and not ir.get("blocking"):
                out.append("RATCHET item_regression no longer blocking")
            if int(ir.get("max_new_failures", 0)) > int(bir.get("max_new_failures", 0)):
                out.append("RATCHET item_regression max_new_failures raised")
    sl = {(s["name"], s["metric"]): s for s in spec.get("slices") or []}
    for bs in base_spec.get("slices") or []:
        s = sl.get((bs["name"], bs["metric"]))
        where = f"slice:{bs['name']}/{bs['metric']}"
        if s is None:
            out.append(f"RATCHET {where} removed")
            continue
        if bs.get("blocking") and not s.get("blocking"):
            out.append(f"RATCHET {where} no longer blocking")
        out += [f"RATCHET {x}" for x in _loosened(where, {k: s.get(k) for k in ("min", "max", "no_significant_drop")},
                                                     {k: bs[k] for k in ("min", "max", "no_significant_drop") if k in bs})]
    return out


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for s in spec.get("slices") or []:
        name, mid = s["name"], s["metric"]
        sid = f"slice:{name}/{mid}"
        a, b = per_item(pr, mid, slice_name=name), per_item(base, mid, slice_name=name)
        verdict, note, val = "ok", "", None
        if not a:
            verdict = "missing"              # an empty slice is a relabelled slice, never a pass
        else:
            val = round(sum(a.values()) / len(a), 4)
            if "min" in s and val < s["min"]:
                verdict = "fail"
            if "max" in s and val > s["max"]:
                verdict = "fail"
            keys = sorted(set(a) & set(b))
            if s.get("no_significant_drop") and keys:
                d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                   n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
                note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    verdict = "fail"
        if verdict != "ok" and s.get("blocking"):
            blocked.append(sid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if s.get("blocking") else "warn")
        th = {k: s[k] for k in ("min", "max", "no_significant_drop") if k in s}
        lines.append(f"  {sid:<34} {val!s:<8} n={len(a):<3} {json.dumps(th):<36} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    if spec.get("item_regression"):
        ib, il = item_regression_check(pr, base, spec["item_regression"])
        blocked += ib
        lines += il
    return blocked, lines


def item_regression_check(pr: dict, base: dict, rule: dict) -> tuple[list[str], list[str]]:
    """Per-item reliability ratchet: an item that passed `metric` on EVERY seed in the baseline
    may not fail on any seed in the PR. Means and pass^5 can stay flat when a PR fixes one flaky
    item and breaks another (a 1-in-5-seed bug on 2 items); this counts the newly broken items.
    Sound only where A/A noise is 0 (MockLLM); with a real LLM use more seeds or a binomial test."""
    mid, cap = rule.get("metric", "pass_rate"), int(rule.get("max_new_failures", 0))

    def solid(rep: dict) -> dict[str, bool]:
        acc: dict[str, bool] = {}
        for r in rep["rows"]:
            acc[r["item_id"]] = acc.get(r["item_id"], True) and r.get(mid) is True
        return acc

    b, p = solid(base), solid(pr)
    new = sorted(i for i, ok in b.items() if ok and i in p and not p[i])
    sid = f"item_regression:{mid}"
    bad = len(new) > cap
    blocked = [sid] if bad and rule.get("blocking") else []
    label = ("BLOCK" if rule.get("blocking") else "warn") if bad else "ok"
    shown = ", ".join(new[:6]) + (" …" if len(new) > 6 else "")
    return blocked, [f"  {sid:<34} {len(new):<8} {json.dumps({'max_new_failures': cap}):<41} {shown:<28} {label}"]


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
