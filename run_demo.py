"""End-to-end demo: 4 weeks of Merlion complaint traffic through the observed agent.

Timeline (SGT):
  5 Oct – 18 Oct  v0.1 in production (naive build)
  19 Oct          v0.2 deployed (after evals caught v0.1's failures)
  28 Oct 10:00 – 29 Oct 14:00  rate-card OCR service degraded (6x latency, 30% timeouts)

    python run_demo.py            # writes everything to out/
"""
import datetime as dt
import json
import os
import random
import sqlite3

from merlion_obs import alerts, checks, data, metrics, otlp
from merlion_obs.agent import TriageAgent
from merlion_obs.clock import CLOCK
from merlion_obs.evals import evaluate, report
from merlion_obs.tracer import Tracer

OUT = "out"
SGT = data.SGT
DEPLOY_V02 = dt.datetime(2026, 10, 19, 0, 0, tzinfo=SGT)
INC = (dt.datetime(2026, 10, 28, 10, 0, tzinfo=SGT), dt.datetime(2026, 10, 29, 14, 0, tzinfo=SGT))


def incident(now):
    return {"latency_x": 6, "fail_rate": 0.30} if INC[0] <= now < INC[1] else None


def cs_feedback(rng, t, truth):
    """Simulated CS-team review of each recommendation (the online quality signal)."""
    got = t.out.get("resolution")
    if got == "escalate":
        return got, truth["resolution"], "escalated"
    if got == truth["resolution"]:
        return got, got, "accepted"
    if rng.random() < 0.85:
        return got, truth["resolution"], "overridden"
    return got, got, "accepted"  # rubber-stamped: a wrong answer nobody caught


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in ("merlion_ops.db", "traces.db", "traces_eval.db"):
        if os.path.exists(f"{OUT}/{f}"):
            os.remove(f"{OUT}/{f}")
    print("Building synthetic ops data ...")
    complaints = data.build(f"{OUT}/merlion_ops.db")
    ops = sqlite3.connect(f"{OUT}/merlion_ops.db")

    rng = random.Random(11)
    golden = rng.sample(complaints, 90)
    with open(f"{OUT}/golden_set.json", "w") as f:
        json.dump(golden, f, default=str, indent=1)

    print("Offline evals (golden set, n=90) ...")
    etr = Tracer(f"{OUT}/traces_eval.db")
    evals = {v: evaluate(v, golden, etr, ops) for v in ("v0.1", "v0.2")}
    for m in evals.values():
        print(report(m))

    print(f"Replaying {len(complaints)} production complaints ...")
    tr = Tracer(f"{OUT}/traces.db")
    agents = {v: TriageAgent(v, tr, ops, seed=i, incident=incident) for i, v in enumerate(("v0.1", "v0.2"))}
    fb_rng = random.Random(5)
    for e in complaints:
        CLOCK.set(e["received_at"])
        v = "v0.2" if e["received_at"] >= DEPLOY_V02 else "v0.1"
        t = agents[v].run(e)
        a, final, outcome = cs_feedback(fb_rng, t, e["truth"])
        tr.db.execute("INSERT INTO feedback VALUES (?,?,?,?,?)",
                      (t.trace_id, (CLOCK.now() + dt.timedelta(minutes=20)).isoformat(), a, final, outcome))
    tr.db.commit()
    CLOCK.reset()

    print("Running guardrail checks ...")
    checks.run_checks(tr.db, ops, "env='prod'")

    traces = metrics.load(tr.db)
    daily = metrics.daily(traces)
    fired = alerts.evaluate(daily)
    n_spans = otlp.export(tr.db, f"{OUT}/otlp_traces.json")

    # Ground truth for the driver view, so the dashboard can show attribution error.
    truth_by_email = {e["email_id"]: e["truth"] for e in complaints}
    email_of = dict(tr.db.execute("SELECT trace_id, email_id FROM traces WHERE env='prod'"))

    def pick(cond, k=6):
        ids = [t["trace_id"] for t in traces if cond(t)]
        return random.Random(3).sample(ids, min(k, len(ids)))

    examples = {
        "tz": pick(lambda t: t["checks"].get("timezone_alignment", (True,))[0] is False, 3),
        "order": pick(lambda t: t["checks"].get("order_disambiguation", (True,))[0] is False, 2),
        "postal": pick(lambda t: t["checks"].get("postal_code_valid", (True,))[0] is False, 1),
        "incident": pick(lambda t: t["checks"].get("tool_errors", (True,))[0] is False, 2),
        "ok": pick(lambda t: t["agent_version"] == "v0.2" and all(p for p, _, _ in t["checks"].values()), 2),
    }
    explorer = []
    for tag, ids in examples.items():
        for tid in ids:
            d = metrics.trace_detail(tr.db, tid)
            t = next(x for x in traces if x["trace_id"] == tid)
            d.update(tag=tag, version=t["agent_version"], day=t["day"].isoformat(), category=t["category"],
                     resolution=t["resolution"], amount=t["amount"], duration_ms=t["duration_ms"],
                     cost_usd=t["cost_usd"], feedback=t["feedback"],
                     truth=truth_by_email[email_of[tid]])
            explorer.append(d)

    # True complaints per driver (what Priya *should* see) vs agent-attributed, per version period.
    true_drv, attr_drv = {}, {}
    for t in traces:
        tru = truth_by_email[email_of[t["trace_id"]]]
        if tru["system"] == "v2":
            k = (t["agent_version"], tru["zone"])
            true_drv[k] = true_drv.get(k, 0) + 1
        if t["order_system"] == "v2":
            k = (t["agent_version"], t["zone"] or "UNKNOWN")
            attr_drv[k] = attr_drv.get(k, 0) + 1

    dash = {
        "generated_at": dt.datetime.now(SGT).isoformat(timespec="minutes"),
        "period": [daily[0]["day"], daily[-1]["day"]],
        "events": {"deploy_v02": "2026-10-19", "incident": ["2026-10-28", "2026-10-29"]},
        "daily": daily, "by_version": metrics.by_version(traces), "alerts": fired,
        "spans": metrics.span_latency(tr.db), "weekly": metrics.weekly_drivers(traces),
        "zone_truth": [{"version": k[0], "zone": k[1], "n": v} for k, v in sorted(true_drv.items())],
        "zone_attr": [{"version": k[0], "zone": k[1], "n": v} for k, v in sorted(attr_drv.items())],
        "evals": {v: {k: m[k] for k in m if k != "failures"} for v, m in evals.items()},
        "eval_failures": {v: m["failures"][:8] for v, m in evals.items()},
        "explorer": explorer,
        "totals": {"traces": len(traces), "spans": n_spans,
                   "redactions": sum(t["redactions"] for t in traces)},
        "data_quality": {
            "collisions": tr.db.execute("SELECT COUNT(*) FROM spans s JOIN traces t USING(trace_id) WHERE t.env='prod' AND s.name='resolve_order' AND json_extract(s.attrs,'$.\"order.candidates\"')>1").fetchone()[0],
            "lookups": tr.db.execute("SELECT COUNT(*) FROM spans s JOIN traces t USING(trace_id) WHERE t.env='prod' AND s.name='resolve_order'").fetchone()[0],
            "bad_postal": tr.db.execute("SELECT COUNT(*) FROM spans s JOIN traces t USING(trace_id) WHERE t.env='prod' AND s.name='get_delivery_history' AND json_extract(s.attrs,'$.\"delivery.postal_valid\"')=0").fetchone()[0],
            "v2_lookups": tr.db.execute("SELECT COUNT(*) FROM spans s JOIN traces t USING(trace_id) WHERE t.env='prod' AND s.name='get_delivery_history' AND json_extract(s.attrs,'$.\"delivery.postal_valid\"') IS NOT NULL").fetchone()[0],
        },
        "alert_rules": [{"rule": r[0], "metric": r[1] if isinstance(r[1], str) else f"{r[1][1]} failures", "op": r[2],
                         "threshold": r[3], "severity": r[4], "why": r[5]} for r in alerts.RULES],
    }
    with open(f"{OUT}/dashboard_data.json", "w") as f:
        json.dump(dash, f, default=str)
    print(f"\n{len(traces)} traces, {n_spans} spans, {len(fired)} alert firings -> {OUT}/")
    for a in fired[:40]:
        print(f"  {a['day']} [{a['severity']}] {a['rule']}: {a['value']:.3g} (threshold {a['threshold']}) {a['version']}")


if __name__ == "__main__":
    main()
