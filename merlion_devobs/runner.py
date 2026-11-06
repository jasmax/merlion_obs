"""Run the pipeline over a dataset, score every step separately, and gate.

Component evals answer "which step broke?", not just "did the answer change?":
  intake        the order number (or the corporate reference) was extracted
  resolveOrder  (system_version, order_id) == label, or it asked when the label says ask
  tools         facts from the tools it called == facts from the correct tools   (only when the order is right)
  chat          right complaint type, called the tools the policy needs, verdict == policy(its facts),
                cited sources, showed every conflict, did not guess a missing value
  answer_check  nothing uncited or personal reached the CS agent
The first failing step in pipeline order is blamed.
"""
import datetime as dt
import json
import statistics as st
import uuid
from collections import Counter, defaultdict

from merlion_obs.agent import COMPONENTS, TOOL_PLAN, Pipeline, oracle
from merlion_obs.clock import CLOCK
from merlion_obs.reference import VERDICTS, policy
from merlion_obs.registry import ref
from .tracer import DevTracer

AGENTS = COMPONENTS
FACT_KEYS = ["delivery_outcome", "was_late", "evidence_of_delivery", "expected_price"]  # the facts a verdict rests on
GATES = {
    "resolution_accuracy": (">=", 0.90),
    "order_accuracy": (">=", 0.95),
    "loops": ("<=", 0),
    "hidden_conflicts": ("<=", 0),
    "p95_latency_s": ("<=", 12.0),
    "cost_per_case_usd": ("<=", 0.002),
    "newly_broken": ("<=", 2),  # noise budget: a changed prompt re-rolls ~1-2 cases even when nothing regressed
}


def _pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))] if xs else 0


def _same(k, a, b):
    if k == "evidence_of_delivery":
        return bool(a) == bool(b)
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 0.01
    return a == b


def score_case(ops, email, out, oracle_cache):
    t = email["truth"]
    comp, detail = {}, {}
    clues, ans, o = out["clues"] or {}, out["answer"], out["order"]
    if t["category"] == "corporate":
        comp["intake"] = bool(clues.get("corporate_ref")) and not clues.get("order_id")
        detail["intake"] = "missed the corporate-account reference"
    else:
        comp["intake"] = clues.get("order_id") == t["order_no"]
        detail["intake"] = f"extracted order {clues.get('order_id')}, expected {t['order_no']}"
    if comp["intake"] and t["order_no"]:
        if t["resolution"] == "clarify":
            comp["resolveOrder"] = o is None
            detail["resolveOrder"] = f"chose {o and o['system']} where the label says ask the CS agent"
        else:
            comp["resolveOrder"] = bool(o) and (o["system"], o["no"]) == (t["system"], t["order_no"])
            detail["resolveOrder"] = f"picked {o['system']}, expected {t['system']}" if o else "no order chosen (ambiguous / not found)"
    chat = ans.get("outcome") is not None and o is not None and not out["loop"]
    if comp.get("resolveOrder") and o and t["resolution"] != "clarify":
        if email["email_id"] not in oracle_cache:
            oracle_cache[email["email_id"]] = oracle(ops, email, {"system_version": t["system"], "order_id": t["order_no"],
                                                                  "category": t["category"]})["facts"]
        orc, f = oracle_cache[email["email_id"]], out["facts"]
        keys = [k for k in FACT_KEYS if k in f and not (k == "expected_price" and ans.get("guessed"))]
        diff = [k for k in keys if not _same(k, f.get(k), orc.get(k))]
        comp["tools"] = not diff
        detail["tools"] = "wrong " + ", ".join(diff)
        if chat:
            called = set(ans.get("_tools") or [])
            need = set(TOOL_PLAN.get(t["category"], []))
            exp = policy(f)
            problems = []
            if f.get("category") != t["category"]:
                problems.append("misclassified complaint type")
            elif need - called:
                problems.append("skipped " + ", ".join(sorted(need - called)))
            elif ans.get("guessed"):
                problems.append("guessed a missing rate")
            elif ans["raw_outcome"] != exp[0] or (exp[1] is not None and abs((ans.get("raw_amount") or 0) - exp[1]) > 0.01):
                problems.append("wrong verdict for its facts" if ans["raw_outcome"] != exp[0] else "wrong amount")
            elif ans["raw_outcome"] in VERDICTS and not ans.get("citations"):
                problems.append("verdict without citations (held by answer_check)")
            elif ans.get("conflicts_found", 0) > len(ans.get("conflicts") or []):
                problems.append("hid a conflict between sources")
            comp["chat"] = not problems
            detail["chat"] = "; ".join(problems)
    comp["answer_check"] = not (ans.get("outcome") in VERDICTS and not ans.get("citations"))
    detail["answer_check"] = "uncited verdict reached the CS agent"
    got = (ans["outcome"], ans.get("amount"))
    action_ok = got[0] == t["resolution"]
    amount_ok = t["amount"] is None or (got[1] is not None and abs(got[1] - t["amount"]) < 0.01)
    passed = action_ok and amount_ok
    blame, sig = None, None
    if not passed:
        if out["loop"]:
            blame, sig = "chat", "chat: tool-call loop (resolveOrder re-check never settles)"
        else:
            for a in AGENTS:
                if comp.get(a) is False:
                    blame, sig = a, f"{a}: {detail[a]}"
                    break
            blame = blame or "dataset"
            sig = sig or "dataset: every step passed, so check the label or the source data"
    return passed, comp, blame, sig, {"action": got[0], "amount": got[1], "order": o}


def run_experiment(db_path, ops, dataset, cfg, meta, baseline=None):
    tr = DevTracer(db_path)
    run_id = meta.get("run_id") or uuid.uuid4().hex[:8]
    net = Pipeline(cfg, tr, ops, label=run_id)
    base_t = dt.datetime.fromisoformat(meta["ts"])
    oracle_cache, rows, hidden = {}, [], 0
    for e in dataset:
        CLOCK.set(base_t)
        with tr.case(run_id, e["email_id"]) as ctx:
            out = net.run(e)
        chat_span = next((s for s in ctx["spans"] if s.name == "chat"), None)
        a = out["answer"]
        if chat_span:
            a["_tools"] = chat_span.attrs.get("chat.tools", [])
            a["conflicts_found"] = chat_span.attrs.get("chat.conflicts_found", 0)
            raw = chat_span.output or {}
            a["raw_outcome"], a["raw_amount"] = raw.get("outcome", a["outcome"]), raw.get("amount", a.get("amount"))
            hidden += a["conflicts_found"] > chat_span.attrs.get("chat.conflicts_disclosed", 0)
        passed, comp, blame, sig, got = score_case(ops, e, out, oracle_cache)
        t = e["truth"]
        rows.append((run_id, e["email_id"], ctx["trace_id"], e.get("source", "golden"), t["category"],
                     json.dumps({"action": t["resolution"], "amount": t["amount"],
                                 "order": f"{t['system']}:{t['order_no']}" if t["system"] else "none"}),
                     json.dumps(got, default=str), int(passed), json.dumps(comp), blame, sig,
                     ctx["latency_ms"], ctx["cost"], ctx["tokens"], ctx["hops"], int(out["loop"])))
    CLOCK.reset()
    tr.db.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    summary = summarise(tr.db, run_id)
    summary["hidden_conflicts"] = hidden
    if baseline:
        summary["newly_broken"] = tr.db.execute(
            "SELECT COUNT(*) FROM cases b JOIN cases a ON a.case_id=b.case_id AND a.run_id=? WHERE b.run_id=? AND a.passed=1 AND b.passed=0",
            (baseline, run_id)).fetchone()[0]
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
    cases = db.execute("SELECT category, passed, components, latency_ms, cost_usd, tokens, hops, loop, got, expected, blame "
                       "FROM cases WHERE run_id=?", (run_id,)).fetchall()
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
        got_order = f"{o['system']}:{o['no']}" if o else "none"
        order_ok += got_order == e["order"] or (e["action"] == "clarify" and not o) or e["order"] == "none"
        esc += g["action"] in ("escalate", "clarify")
        if e["amount"] is not None and g["action"] == e["action"]:
            amt_n += 1
            amt_ok += g["amount"] is not None and abs(g["amount"] - e["amount"]) < 0.01
    agent_cost, agent_ms = defaultdict(float), defaultdict(float)
    for agent, kind, dur, attrs in db.execute("SELECT agent, kind, dur_ms, attrs FROM spans WHERE run_id=?", (run_id,)):
        if kind == "llm":
            agent_cost[agent] += json.loads(attrs).get("llm.cost_usd", 0)
        if kind == "agent" and agent in AGENTS:
            agent_ms[agent] += dur
        if kind == "tool" and agent == "tools":
            agent_ms["tools"] += dur
    lat = [c[3] / 1000 for c in cases]
    return {
        "n": n,
        "resolution_accuracy": sum(c[1] for c in cases) / n,
        "order_accuracy": order_ok / n,
        "amount_accuracy": amt_ok / amt_n if amt_n else 1.0,
        "escalation_rate": esc / n,
        "loops": sum(c[7] for c in cases),
        "p50_latency_s": round(_pct(lat, .5), 2), "p95_latency_s": round(_pct(lat, .95), 2),
        "cost_per_case_usd": round(sum(c[4] for c in cases) / n, 6),
        "tokens_per_case": round(sum(c[5] for c in cases) / n),
        "mean_hops": round(st.mean(c[6] for c in cases), 2),
        "components": {a: comp_ok[a] / comp_n[a] for a in AGENTS if comp_n[a]},
        "component_n": dict(comp_n),
        "blame": dict(Counter(c[10] for c in cases if c[10])),
        "by_category": {k: v[0] / v[1] for k, v in sorted(by_cat.items())},
        "agent_cost_usd": {a: round(agent_cost[a] / n, 6) for a in AGENTS},
        "agent_ms": {a: round(agent_ms[a] / n) for a in AGENTS},
    }
