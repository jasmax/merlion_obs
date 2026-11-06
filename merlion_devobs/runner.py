"""Run the agent network over a dataset, score every agent separately, and gate.

Component evals answer "which agent broke?", not just "did the answer change?":
  router         category == label
  order_resolver (system, order_no) == label
  evidence       facts == oracle facts for the true order   (only when the two above are right)
  policy         output == policy(facts it was given)       (did it follow the rules on its inputs?)
The first failing component in pipeline order is blamed.
"""
import datetime as dt
import json
import statistics as st
import uuid
from collections import Counter, defaultdict

from merlion_obs.clock import CLOCK
from merlion_obs.data import policy as rules
from .network import Network, oracle_facts
from .prompts import ref
from .tracer import DevTracer

AGENTS = ["router", "order_resolver", "evidence", "policy", "writer"]
FACT_KEYS = ["has_pod", "damage_visible", "gps_at_address", "late_hours", "service", "billed_service",
             "fee_billed", "declared_value", "pending_refund"]
GATES = {
    "resolution_accuracy": (">=", 0.90),
    "order_accuracy": (">=", 0.95),
    "loops": ("<=", 0),
    "p95_latency_s": ("<=", 12.0),
    "cost_per_case_usd": ("<=", 0.025),
    "newly_broken": ("<=", 1),
}


def _pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))] if xs else 0


def score_case(ops, email, out, oracle_cache):
    t = email["truth"]
    comp, detail = {}, {}
    comp["router"] = out["router"]["category"] == t["category"]
    detail["router"] = f"labelled {out['router']['category']}, expected {t['category']}"
    o = out["order"]
    comp["order_resolver"] = bool(o) and o["system"] == t["system"] and o["no"] == t["order_no"]
    detail["order_resolver"] = (f"picked {o['system']}, expected {t['system']}" if o else "no order chosen (ambiguous / not found)")
    if comp["router"] and comp["order_resolver"]:
        if email["email_id"] not in oracle_cache:
            oracle_cache[email["email_id"]] = oracle_facts(ops, email)
        orc = oracle_cache[email["email_id"]]
        diff = [k for k in FACT_KEYS if out["facts"].get(k) != orc.get(k)]
        comp["evidence"] = not diff
        detail["evidence"] = "wrong " + ", ".join(diff) if diff else ""
    expect = rules(out["facts"])
    got = (out["policy"]["action"], out["policy"]["amount"])
    comp["policy"] = got[0] == expect[0] and (expect[1] is None or abs((got[1] or 0) - expect[1]) < 0.01)
    detail["policy"] = ("wrong amount" if got[0] == expect[0] else "wrong action") + f": {got[0]} {got[1]} vs rules {expect[0]} {expect[1]}"
    action_ok = got[0] == t["resolution"]
    amount_ok = t["amount"] is None or (got[1] is not None and abs(got[1] - t["amount"]) < 0.01)
    passed = action_ok and amount_ok
    blame, sig = None, None
    if not passed:
        if out["loop"]:
            blame, sig = "network", "network: handoff loop router ↔ order_resolver"
        else:
            for a in ["router", "order_resolver", "evidence", "policy"]:
                if comp.get(a) is False:
                    blame = a
                    d = detail[a]
                    if a == "router":
                        d = "misclassified category"
                    if a == "policy":
                        d = d.split(":")[0]
                    sig = f"{a}: {d}"
                    break
            blame = blame or "dataset"
            sig = sig or "dataset: every agent passed, so check the label or the source data"
    return passed, comp, blame, sig, {"action": got[0], "amount": got[1], "order": o}


def run_experiment(db_path, ops, dataset, cfg, meta, baseline=None):
    tr = DevTracer(db_path)
    run_id = meta.get("run_id") or uuid.uuid4().hex[:8]
    net = Network(cfg, tr, ops)
    base_t = dt.datetime.fromisoformat(meta["ts"])
    oracle_cache, rows = {}, []
    for e in dataset:
        CLOCK.set(base_t)
        with tr.case(run_id, e["email_id"]) as ctx:
            out = net.run(e)
        passed, comp, blame, sig, got = score_case(ops, e, out, oracle_cache)
        t = e["truth"]
        rows.append((run_id, e["email_id"], ctx["trace_id"], e.get("source", "golden"), t["category"],
                     json.dumps({"action": t["resolution"], "amount": t["amount"], "order": f"{t['system']}:{t['order_no']}"}),
                     json.dumps(got, default=str), int(passed), json.dumps(comp), blame, sig,
                     ctx["latency_ms"], ctx["cost"], ctx["tokens"], ctx["hops"], int(out["loop"])))
    CLOCK.reset()
    tr.db.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    summary = summarise(tr.db, run_id)
    gate = None
    if baseline:
        broken = tr.db.execute(
            "SELECT COUNT(*) FROM cases b JOIN cases a ON a.case_id=b.case_id AND a.run_id=? WHERE b.run_id=? AND a.passed=1 AND b.passed=0",
            (baseline, run_id)).fetchone()[0]
        summary["newly_broken"] = broken
        summary["baseline"] = baseline
    else:
        summary["newly_broken"] = 0
    gate = {k: (summary[k] >= th if op == ">=" else summary[k] <= th) for k, (op, th) in GATES.items()}
    prompts = {a: ref(a, v) for a, v in cfg["prompts"].items()}
    tr.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, meta["ts"], meta.get("git_sha"), meta.get("branch"), meta.get("author"), meta.get("message"),
                   meta.get("dataset"), json.dumps(cfg), json.dumps(prompts), json.dumps(summary), json.dumps(gate)))
    tr.db.commit()
    return run_id, summary, gate


def summarise(db, run_id):
    cases = db.execute("SELECT category, passed, components, latency_ms, cost_usd, tokens, hops, loop, got, expected, blame FROM cases WHERE run_id=?", (run_id,)).fetchall()
    n = len(cases)
    comp_ok, comp_n = Counter(), Counter()
    by_cat = defaultdict(lambda: [0, 0])
    order_ok = esc = amt_ok = amt_n = 0
    for cat, passed, comps, *_rest in cases:
        for a, ok in json.loads(comps).items():
            comp_n[a] += 1
            comp_ok[a] += ok
        by_cat[cat][0] += passed
        by_cat[cat][1] += 1
    for c in cases:
        g, e = json.loads(c[8]), json.loads(c[9])
        o = g["order"]
        order_ok += bool(o) and f"{o['system']}:{o['no']}" == e["order"]
        esc += g["action"] == "escalate"
        if e["amount"] is not None and g["action"] == e["action"]:
            amt_n += 1
            amt_ok += g["amount"] is not None and abs(g["amount"] - e["amount"]) < 0.01
    agent_cost, agent_ms = defaultdict(float), defaultdict(float)
    for agent, kind, dur, attrs in db.execute("SELECT agent, kind, dur_ms, attrs FROM spans WHERE run_id=?", (run_id,)):
        if kind == "llm":
            agent_cost[agent] += json.loads(attrs).get("llm.cost_usd", 0)
        if kind == "agent" and agent != "network":
            agent_ms[agent] += dur
    lat = [c[3] / 1000 for c in cases]
    return {
        "n": n,
        "resolution_accuracy": sum(c[1] for c in cases) / n,
        "order_accuracy": order_ok / n,
        "amount_accuracy": amt_ok / amt_n if amt_n else 1.0,
        "escalation_rate": esc / n,
        "loops": sum(c[7] for c in cases),
        "p50_latency_s": round(_pct(lat, .5), 2), "p95_latency_s": round(_pct(lat, .95), 2),
        "cost_per_case_usd": round(sum(c[4] for c in cases) / n, 5),
        "tokens_per_case": round(sum(c[5] for c in cases) / n),
        "mean_hops": round(st.mean(c[6] for c in cases), 2),
        "components": {a: comp_ok[a] / comp_n[a] for a in AGENTS if comp_n[a]},
        "component_n": dict(comp_n),
        "blame": dict(Counter(c[10] for c in cases if c[10])),
        "by_category": {k: v[0] / v[1] for k, v in sorted(by_cat.items())},
        "agent_cost_usd": {a: round(agent_cost[a] / n, 5) for a in AGENTS},
        "agent_ms": {a: round(agent_ms[a] / n) for a in AGENTS},
    }
