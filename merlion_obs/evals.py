"""Offline eval harness over a labelled golden set, with release gates.

    python -m merlion_obs.evals --version v0.2      # exit code 1 if any gate fails

Run it in CI before every prompt / model / tool change. The golden set should
be the client's 90 sample emails, hand-labelled with the right order and
resolution (here: a 90-email sample of the synthetic stream).
"""
import argparse
import json
import sqlite3
import statistics as st
import sys

from .agent import TriageAgent
from .checks import run_checks
from .clock import CLOCK
from .tracer import Tracer

GATES = {  # metric: (comparator, threshold)
    "order_accuracy": (">=", 0.95),
    "resolution_accuracy": (">=", 0.90),
    "wrongful_reject_rate": ("<=", 0.02),
    "critical_check_fail_rate": ("<=", 0.02),
    "p95_latency_s": ("<=", 12.0),
}


def evaluate(version, golden, tracer, ops, seed=99):
    agent = TriageAgent(version, tracer, ops, seed=seed, env=f"eval:{version}")
    rows = []
    for e in golden:
        CLOCK.set(e["received_at"])
        t = agent.run(e)
        rows.append((e, t))
    ids = [t.trace_id for _, t in rows]
    run_checks(tracer.db, ops, f"trace_id IN ({','.join('?' * len(ids))})", ids)
    crit = tracer.db.execute(
        f"SELECT COUNT(DISTINCT trace_id) FROM checks WHERE passed=0 AND severity='critical' AND trace_id IN ({','.join('?' * len(ids))})",
        ids).fetchone()[0]

    n = len(rows)
    order_ok = res_ok = amt_ok = amt_n = wrong_reject = esc = 0
    by_cat, failures = {}, []
    for e, t in rows:
        tr, o = e["truth"], t.out
        oc = o.get("order_system") == tr["system"] and o.get("order_no") == tr["order_no"]
        rc = o.get("resolution") == tr["resolution"]
        order_ok += oc
        res_ok += rc
        esc += o.get("resolution") == "escalate"
        wrong_reject += o.get("resolution") == "reject" and tr["resolution"] != "reject"
        if rc and tr["amount"] is not None:
            amt_n += 1
            amt_ok += abs((o.get("amount") or 0) - tr["amount"]) < 0.01
        c = by_cat.setdefault(tr["category"], [0, 0])
        c[0] += rc
        c[1] += 1
        if not rc:
            failures.append({"email_id": e["email_id"], "trace_id": t.trace_id, "category": tr["category"],
                             "expected": tr["resolution"], "got": o.get("resolution"),
                             "order_expected": f"{tr['system']}:{tr['order_no']}",
                             "order_got": f"{o.get('order_system')}:{o.get('order_no')}"})
    lat = sorted(tracer.db.execute(
        f"SELECT duration_ms FROM traces WHERE trace_id IN ({','.join('?' * len(ids))})", ids).fetchall())
    cost = [t.cost for _, t in rows]
    m = {
        "version": version, "n": n,
        "order_accuracy": order_ok / n,
        "resolution_accuracy": res_ok / n,
        "amount_accuracy": amt_ok / amt_n if amt_n else 1.0,
        "wrongful_reject_rate": wrong_reject / n,
        "escalation_rate": esc / n,
        "critical_check_fail_rate": crit / n,
        "p95_latency_s": lat[int(0.95 * (len(lat) - 1))][0] / 1000,
        "mean_cost_usd": st.mean(cost),
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
    lines = [f"Eval {m['version']}  (n={m['n']})  -> {'PASS' if m['passed'] else 'FAIL'}"]
    for k, (op, th) in GATES.items():
        lines.append(f"  {'✓' if m['gates'][k] else '✗'} {k:<26} {m[k]:.3f}  (gate {op} {th})")
    lines.append(f"    amount_accuracy            {m['amount_accuracy']:.3f}")
    lines.append(f"    escalation_rate            {m['escalation_rate']:.3f}")
    lines.append(f"    mean_cost_usd              {m['mean_cost_usd']:.4f}")
    lines.append("  by category: " + ", ".join(f"{k} {v:.0%}" for k, v in m["by_category"].items()))
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v0.2")
    ap.add_argument("--workdir", default="out")
    a = ap.parse_args()
    golden = json.load(open(f"{a.workdir}/golden_set.json"), object_hook=lambda d: d)
    import datetime as dt
    for e in golden:
        e["received_at"] = dt.datetime.fromisoformat(e["received_at"])
    ops = sqlite3.connect(f"{a.workdir}/merlion_ops.db")
    tracer = Tracer(f"{a.workdir}/traces_eval.db")
    m = evaluate(a.version, golden, tracer, ops)
    print(report(m))
    CLOCK.reset()
    sys.exit(0 if m["passed"] else 1)
