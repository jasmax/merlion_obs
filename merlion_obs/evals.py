"""Offline eval harness over a labelled golden set, with release gates (harness checklist #8).

    python -m merlion_obs.evals --version v0.2      # exit code 1 if any gate fails

Golden set:
  fixture   out/golden_set.json, 90 emails sampled from the synthetic stream (written by run_demo.py)
  mysql     every email labelled in golden/labels.json (see merlion_obs/dataset.py)
Run it in CI before every prompt, model or tool change; GitHub Actions blocks the merge on a failure.
"""
import argparse
import datetime as dt
import json
import os
import statistics as st
import sys

from . import dataset, db
from .agent import TriageAgent
from .checks import run_checks
from .clock import CLOCK
from .tracer import Tracer

GATES = {  # metric: (comparator, threshold)
    "eval_size": (">=", 20),
    "order_accuracy": (">=", 0.95),
    "resolution_accuracy": (">=", 0.90),
    "wrongful_reject_rate": ("<=", 0.02),
    "refusal_accuracy": (">=", 1.0),
    "critical_check_fail_rate": ("<=", 0.02),
    "p95_latency_s": ("<=", 12.0),
}


def order_ok(o, tr):
    if tr["resolution"] == "clarify" or not tr["system"]:
        return not o.get("order_no") or (o.get("order_system"), o.get("order_no")) == (tr["system"], tr["order_no"])
    return (o.get("order_system"), o.get("order_no")) == (tr["system"], tr["order_no"])


def evaluate(version, golden, tracer, ops):
    agent = TriageAgent(version, tracer, ops, env=f"eval:{version}")
    rows = []
    for e in golden:
        CLOCK.set(e["received_at"])
        rows.append((e, agent.run(e)))
    ids = [t.trace_id for _, t in rows]
    q = ",".join("?" * len(ids))
    run_checks(tracer.db, ops, f"trace_id IN ({q})", ids)
    crit = tracer.db.execute(f"SELECT COUNT(DISTINCT trace_id) FROM checks WHERE passed=0 AND severity='critical' AND trace_id IN ({q})", ids).fetchone()[0]

    n = len(rows)
    n_order = n_res = amt_ok = amt_n = wrong_reject = esc = ref_ok = ref_n = 0
    by_cat, failures = {}, []
    for e, t in rows:
        tr, o = e["truth"], t.out
        oc = order_ok(o, tr)
        rc = o.get("resolution") == tr["resolution"]
        n_order += oc
        n_res += rc
        esc += o.get("resolution") in ("escalate", "clarify")
        wrong_reject += o.get("resolution") == "reject" and tr["resolution"] != "reject"
        if tr["resolution"] == "refuse":
            ref_n += 1
            ref_ok += o.get("resolution") == "refuse"
        if rc and tr["amount"] is not None:
            amt_n += 1
            amt_ok += abs((o.get("amount") or 0) - tr["amount"]) < 0.01
        c = by_cat.setdefault(tr["category"], [0, 0])
        c[0] += rc
        c[1] += 1
        if not rc:
            failures.append({"email_id": e["email_id"], "trace_id": t.trace_id, "category": tr["category"],
                             "expected": tr["resolution"], "got": o.get("resolution"),
                             "order_expected": f"{tr['system']}:{tr['order_no']}" if tr["system"] else "none",
                             "order_got": f"{o.get('order_system')}:{o.get('order_no')}" if o.get("order_no") else "none"})
    lat = sorted(r[0] for r in tracer.db.execute(f"SELECT duration_ms FROM traces WHERE trace_id IN ({q})", ids))
    m = {
        "version": version, "n": n, "eval_size": n,
        "order_accuracy": n_order / n, "resolution_accuracy": n_res / n,
        "amount_accuracy": amt_ok / amt_n if amt_n else 1.0,
        "wrongful_reject_rate": wrong_reject / n, "escalation_rate": esc / n,
        "refusal_accuracy": ref_ok / ref_n if ref_n else 0.0, "refusals_in_set": ref_n,
        "critical_check_fail_rate": crit / n,
        "p95_latency_s": lat[int(0.95 * (len(lat) - 1))] / 1000,
        "mean_cost_usd": st.mean(t.cost for _, t in rows),
        "by_category": {k: v[0] / v[1] for k, v in sorted(by_cat.items())},
        "failures": failures,
    }
    m["gates"] = {k: (m[k] >= th if op == ">=" else m[k] <= th) for k, (op, th) in GATES.items()}
    m["passed"] = all(m["gates"].values())
    tracer.db.execute("CREATE TABLE IF NOT EXISTS eval_runs(version TEXT, result TEXT)")
    tracer.db.execute("INSERT INTO eval_runs VALUES (?,?)", (version, json.dumps(m, default=str)))
    tracer.db.commit()
    return m


def report(m):
    lines = [f"Eval {m['version']}  (n={m['n']}, refusals in set={m['refusals_in_set']})  -> {'PASS' if m['passed'] else 'FAIL'}"]
    for k, (op, th) in GATES.items():
        lines.append(f"  {'PASS' if m['gates'][k] else 'FAIL'} {k:<26} {m[k]:.3f}  (gate {op} {th})")
    lines.append(f"       amount_accuracy            {m['amount_accuracy']:.3f}")
    lines.append(f"       escalation_rate            {m['escalation_rate']:.3f}")
    lines.append(f"       mean_cost_usd              {m['mean_cost_usd']:.5f} (estimated)")
    lines.append("  by category: " + ", ".join(f"{k} {v:.0%}" for k, v in m["by_category"].items()))
    return "\n".join(lines)


def load_golden(workdir, ops):
    if ops.kind == "sqlite":
        golden = json.load(open(f"{workdir}/golden_set.json", encoding="utf-8"))
        for e in golden:
            e["received_at"] = dt.datetime.fromisoformat(e["received_at"])
        return golden
    return dataset.load(ops, labelled_only=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v0.2")
    ap.add_argument("--workdir", default="out")
    a = ap.parse_args()
    ops = db.connect(fixture_path=os.path.join(a.workdir, "merlion_clean.db"))
    golden = load_golden(a.workdir, ops)
    if not golden:
        sys.exit("no labelled emails: fill golden/labels.json (python -m merlion_obs.dataset init)")
    m = evaluate(a.version, golden, Tracer(f"{a.workdir}/traces_eval.db"), ops)
    print(report(m))
    CLOCK.reset()
    sys.exit(0 if m["passed"] else 1)
