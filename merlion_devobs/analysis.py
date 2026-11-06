"""Run-to-run diffs, failure clusters, and prod-to-dataset promotion."""
import json
import sqlite3
from collections import defaultdict

from .runner import AGENTS


def _run(db, rid):
    r = db.execute("SELECT run_id, ts, git_sha, branch, message, config, prompts, summary, gate FROM runs WHERE run_id=?", (rid,)).fetchone()
    keys = ["run_id", "ts", "git_sha", "branch", "message", "config", "prompts", "summary", "gate"]
    d = dict(zip(keys, r))
    for k in ("config", "prompts", "summary", "gate"):
        d[k] = json.loads(d[k])
    return d


def first_divergence(db, a, b, case_id):
    """The first agent step whose output, or whose place in the control flow, differs between two runs."""
    def seq(rid):
        return db.execute(
            "SELECT agent, out_digest FROM spans WHERE run_id=? AND case_id=? AND kind='agent' AND name=agent ORDER BY start_ms",
            (rid, case_id)).fetchall()
    sa, sb = seq(a), seq(b)
    for i in range(max(len(sa), len(sb))):
        x = sa[i] if i < len(sa) else (None, None)
        y = sb[i] if i < len(sb) else (None, None)
        if x[0] != y[0]:
            return f"{y[0] or x[0]} (control flow: {x[0]} → {y[0]})"
        if x[1] != y[1]:
            return x[0]
    return None


def diff_runs(db, a, b):
    A, B = _run(db, a), _run(db, b)
    ca = {r[0]: r[1:] for r in db.execute("SELECT case_id, passed, failure_sig, category, latency_ms, cost_usd FROM cases WHERE run_id=?", (a,))}
    cb = {r[0]: r[1:] for r in db.execute("SELECT case_id, passed, failure_sig, category, latency_ms, cost_usd FROM cases WHERE run_id=?", (b,))}
    states = defaultdict(list)
    for cid in sorted(set(ca) & set(cb)):
        pa, pb = ca[cid][0], cb[cid][0]
        st = {(1, 1): "still_passing", (0, 0): "still_failing", (0, 1): "fixed", (1, 0): "broken"}[(pa, pb)]
        rec = {"case_id": cid, "category": cb[cid][2], "sig_before": ca[cid][1], "sig_after": cb[cid][1],
               "latency_delta_ms": round(cb[cid][3] - ca[cid][3]), "cost_delta": round(cb[cid][4] - ca[cid][4], 5)}
        if st in ("fixed", "broken") or (st == "still_failing" and ca[cid][1] != cb[cid][1]):
            rec["diverged_at"] = first_divergence(db, a, b, cid)
        states[st].append(rec)
    prompts_changed = {ag: [A["prompts"].get(ag), B["prompts"].get(ag)] for ag in B["prompts"] if A["prompts"].get(ag) != B["prompts"].get(ag)}
    models_changed = {ag: [A["config"]["models"].get(ag), B["config"]["models"].get(ag)] for ag in B["config"]["models"]
                      if A["config"]["models"].get(ag) != B["config"]["models"].get(ag)}
    metrics = {}
    for k in ("resolution_accuracy", "order_accuracy", "amount_accuracy", "escalation_rate", "loops",
              "p50_latency_s", "p95_latency_s", "cost_per_case_usd", "tokens_per_case", "mean_hops"):
        metrics[k] = [A["summary"][k], B["summary"][k]]
    return {"a": a, "b": b, "prompts_changed": prompts_changed, "models_changed": models_changed,
            "metrics": metrics, "counts": {k: len(v) for k, v in states.items()}, "cases": dict(states)}


def clusters(db, run_id):
    out = defaultdict(lambda: {"n": 0, "categories": defaultdict(int), "examples": []})
    for cid, sig, cat in db.execute("SELECT case_id, failure_sig, category FROM cases WHERE run_id=? AND passed=0", (run_id,)):
        c = out[sig]
        c["n"] += 1
        c["categories"][cat] += 1
        if len(c["examples"]) < 4:
            c["examples"].append(cid)
    return sorted(({"signature": k, "n": v["n"], "categories": dict(v["categories"]), "examples": v["examples"]}
                   for k, v in out.items()), key=lambda x: -x["n"])


def promote_from_prod(prod_traces_db, out_path, limit=50):
    """Turn production traces the CS team overrode into dataset candidates (need a human label)."""
    db = sqlite3.connect(prod_traces_db)
    rows = db.execute(
        "SELECT t.email_id, t.category, f.agent_resolution, f.final_resolution, t.trace_id FROM feedback f "
        "JOIN traces t USING(trace_id) WHERE f.outcome='overridden' ORDER BY t.start_ts DESC LIMIT ?", (limit,)).fetchall()
    with open(out_path, "w") as f:
        for email_id, cat, agent_res, final, tid in rows:
            f.write(json.dumps({"email_id": email_id, "category": cat, "agent_said": agent_res, "cs_decided": final,
                                "prod_trace_id": tid, "status": "needs_label"}) + "\n")
    return len(rows)
