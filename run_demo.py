"""Production stack end to end: evals -> production replay -> guardrail checks -> alerts -> dashboard data.

    python run_demo.py                          # synthetic merlion_clean (fixture), writes out/
    MERLION_SOURCE=mysql python run_demo.py     # the real merlion_clean and the 90 real complaint emails

Fixture timeline (SGT, 4 weeks of complaint traffic):
  4 May - 17 May   v0.1 in production (naive build)
  18 May           v0.2 deployed (after the offline evals caught v0.1's failures)
  26 May 10:00 - 27 May 14:00   OpenAI API degraded (6x latency, 30% timeouts)
Real data: the emails are replayed in date order, v0.1 on the first half of the days and v0.2 on the
second, so the dashboard can compare the two releases on the same client data. No incident is injected.
"""
import datetime as dt
import json
import os
import random

from merlion_obs import alerts, checks, dataset, db, fixture, metrics, otlp
from merlion_obs.agent import TriageAgent
from merlion_obs.clock import CLOCK
from merlion_obs.evals import evaluate, report
from merlion_obs.reference import SGT
from merlion_obs.tracer import Tracer

OUT = "out"
SOURCE = os.environ.get("MERLION_SOURCE", "fixture")
INCIDENT = (dt.datetime(2026, 5, 26, 10, 0, tzinfo=SGT), dt.datetime(2026, 5, 27, 14, 0, tzinfo=SGT))


def cs_feedback(rng, t, truth):
    """Simulated CS-team review of each recommendation (the online quality signal)."""
    got = t.out.get("resolution")
    if truth is None:
        return None
    if got in ("escalate", "clarify"):
        return got, truth["resolution"], "escalated"
    if got == truth["resolution"]:
        return got, got, "accepted"
    if rng.random() < 0.85:
        return got, truth["resolution"], "overridden"
    return got, got, "accepted"  # rubber-stamped: a wrong answer nobody caught


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in ("traces.db", "traces_eval.db"):
        if os.path.exists(f"{OUT}/{f}"):
            os.remove(f"{OUT}/{f}")
    if SOURCE == "fixture":
        print("Building the synthetic merlion_clean ...")
        fixture.build(f"{OUT}/merlion_clean.db")
    ops = db.connect(SOURCE, f"{OUT}/merlion_clean.db")
    complaints = dataset.load(ops)
    labelled = [e for e in complaints if e["truth"]]
    days = sorted({e["received_at"].date() for e in complaints})
    if SOURCE == "fixture":
        deploy, incident_win = dt.date(2026, 5, 18), INCIDENT
    else:
        deploy, incident_win = days[len(days) // 2], None

    def incident(now):
        return {"latency_x": 6, "fail_rate": 0.30} if incident_win and incident_win[0] <= now < incident_win[1] else None

    golden = random.Random(11).sample(labelled, min(90, len(labelled)))
    with open(f"{OUT}/golden_set.json", "w", encoding="utf-8") as f:
        json.dump(golden, f, default=str, indent=1)

    evals = {}
    if golden:
        print(f"Offline evals (golden set, n={len(golden)}) ...")
        etr = Tracer(f"{OUT}/traces_eval.db")
        evals = {v: evaluate(v, golden, etr, ops) for v in ("v0.1", "v0.2")}
        for m in evals.values():
            print(report(m))
    else:
        print("No labelled emails yet: skipping evals (fill golden/labels.json).")

    print(f"Replaying {len(complaints)} production complaints ...")
    tr = Tracer(f"{OUT}/traces.db")
    agents = {v: TriageAgent(v, tr, ops, incident=incident) for v in ("v0.1", "v0.2")}
    fb_rng = random.Random(5)
    for e in complaints:
        CLOCK.set(e["received_at"])
        v = "v0.2" if e["received_at"].date() >= deploy else "v0.1"
        t = agents[v].run(e)
        fb = cs_feedback(fb_rng, t, e["truth"])
        if fb:
            tr.db.execute("INSERT INTO feedback VALUES (?,?,?,?,?)",
                          (t.trace_id, (CLOCK.now() + dt.timedelta(minutes=20)).isoformat(), *fb))
    tr.db.commit()
    CLOCK.reset()

    print("Running guardrail checks ...")
    checks.run_checks(tr.db, ops, "env='prod'")
    traces = metrics.load(tr.db)
    daily = metrics.daily(traces)
    fired = alerts.evaluate(daily)
    n_spans = otlp.export(tr.db, f"{OUT}/otlp_traces.json")

    truth_by_email = {e["email_id"]: e["truth"] for e in complaints}
    email_of = dict(tr.db.execute("SELECT trace_id, email_id FROM traces WHERE env='prod'"))

    def pick(cond, k):
        ids = [t["trace_id"] for t in traces if cond(t)]
        return random.Random(3).sample(ids, min(k, len(ids)))

    fails = lambda c: (lambda t: t["checks"].get(c, (True,))[0] is False)
    examples = {"tz": pick(fails("timezone_alignment"), 2), "order": pick(fails("order_disambiguation"), 2),
                "conflict": pick(fails("conflict_disclosed"), 1), "gap": pick(fails("honest_gaps"), 1),
                "zone": pick(fails("zone_from_town"), 1), "incident": pick(fails("tool_errors"), 2),
                "refuse": pick(lambda t: t["resolution"] == "refuse" and t["agent_version"] == "v0.2", 1),
                "ok": pick(lambda t: t["agent_version"] == "v0.2" and t["checks"] and all(p for p, _, _ in t["checks"].values())
                           and t["resolution"] in ("reject", "refund", "compensate", "redeliver"), 2)}
    explorer = []
    for tag, ids in examples.items():
        for tid in ids:
            d = metrics.trace_detail(tr.db, tid)
            t = next(x for x in traces if x["trace_id"] == tid)
            d.update(tag=tag, version=t["agent_version"], day=t["day"].isoformat(), category=t["category"] or "unknown",
                     resolution=t["resolution"], amount=t["amount"], duration_ms=t["duration_ms"], cost_usd=t["cost_usd"],
                     feedback=t["feedback"], truth=truth_by_email[email_of[tid]])
            explorer.append(d)

    true_zone, attr_zone = {}, {}
    for t in traces:
        tru = truth_by_email[email_of[t["trace_id"]]]
        if tru and tru.get("zone"):
            k = (t["agent_version"], tru["zone"])
            true_zone[k] = true_zone.get(k, 0) + 1
        if t["driver_id"]:
            k = (t["agent_version"], t["zone"] or "Unknown")
            attr_zone[k] = attr_zone.get(k, 0) + 1

    def count(sql):
        return tr.db.execute("SELECT COUNT(*) FROM spans s JOIN traces t USING(trace_id) WHERE t.env='prod' AND " + sql).fetchone()[0]

    dash = {
        "generated_at": dt.datetime.now(SGT).isoformat(timespec="minutes"),
        "source": {"kind": SOURCE, "db": ops.source, "labelled": len(labelled), "emails": len(complaints)},
        "period": [daily[0]["day"], daily[-1]["day"]],
        "events": {"deploy_v02": deploy.isoformat(),
                   "incident": [incident_win[0].date().isoformat(), incident_win[1].date().isoformat()] if incident_win else None},
        "daily": daily, "by_version": metrics.by_version(traces), "alerts": fired,
        "spans": metrics.span_latency(tr.db), "weekly": metrics.weekly_drivers(traces),
        "zone_truth": [{"version": k[0], "zone": k[1], "n": v} for k, v in sorted(true_zone.items())],
        "zone_attr": [{"version": k[0], "zone": k[1], "n": v} for k, v in sorted(attr_zone.items())],
        "evals": {v: {k: m[k] for k in m if k != "failures"} for v, m in evals.items()} or None,
        "eval_failures": {v: m["failures"][:8] for v, m in evals.items()},
        "explorer": explorer,
        "totals": {"traces": len(traces), "spans": n_spans, "redactions": sum(t["redactions"] for t in traces)},
        "data_quality": {
            "collisions": count("s.name='resolveOrder.query' AND json_extract(s.attrs,'$.\"order.candidates\"')>1"),
            "lookups": count("s.name='resolveOrder.query'"),
            "pod_conflicts": count("s.name='getPodScan' AND json_array_length(json_extract(s.attrs,'$.\"pod.conflicts\"'))>0"),
            "pods": count("s.name='getPodScan' AND json_extract(s.attrs,'$.\"pod.found\"')=1"),
            "gps_empty": count("s.name='getGpsTrail' AND json_extract(s.attrs,'$.\"gps.pings_in_window\"')=0"),
            "gps_calls": count("s.name='getGpsTrail'"),
        },
        "alert_rules": [{"rule": r[0], "metric": r[1] if isinstance(r[1], str) else f"{r[1][1]} failures", "op": r[2],
                         "threshold": r[3], "severity": r[4], "why": r[5]} for r in alerts.RULES],
    }
    with open(f"{OUT}/dashboard_data.json", "w", encoding="utf-8") as f:
        json.dump(dash, f, default=str)
    print(f"\n{len(traces)} traces, {n_spans} spans, {len(fired)} alert firings -> {OUT}/")
    for a in fired[:40]:
        print(f"  {a['day']} [{a['severity']}] {a['rule']}: {a['value']:.3g} (threshold {a['threshold']}) {a['version']}")


if __name__ == "__main__":
    main()
