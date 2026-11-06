"""Rollups over the trace store: what the dashboard and alerts read."""
import datetime as dt
import json
import statistics as st
from collections import Counter, defaultdict

SGT = dt.timezone(dt.timedelta(hours=8))


def _pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))] if xs else 0


def load(tdb, env="prod"):
    cols = ["trace_id", "agent_version", "start_ts", "duration_ms", "status", "category", "order_system",
            "order_no", "resolution", "amount", "driver_id", "zone", "tokens_in", "tokens_out", "cost_usd", "redactions"]
    traces = [dict(zip(cols, r)) for r in tdb.execute(f"SELECT {','.join(cols)} FROM traces WHERE env=?", (env,))]
    by_id = {t["trace_id"]: t for t in traces}
    for t in traces:
        t["day"] = dt.datetime.fromisoformat(t["start_ts"]).astimezone(SGT).date()
        t["checks"], t["feedback"] = {}, None
    for tid, name, passed, sev, detail in tdb.execute("SELECT * FROM checks"):
        if tid in by_id:
            by_id[tid]["checks"][name] = (bool(passed), sev, detail)
    for tid, _, agent_res, final_res, outcome in tdb.execute("SELECT * FROM feedback"):
        if tid in by_id:
            by_id[tid]["feedback"] = {"final": final_res, "outcome": outcome}
    return traces


def _agg(ts):
    lat = [t["duration_ms"] / 1000 for t in ts]
    fb = [t for t in ts if t["feedback"]]
    crit = [t for t in ts if any(not p and s == "critical" for p, s, _ in t["checks"].values())]
    return {
        "n": len(ts),
        "p50_latency_s": round(_pct(lat, 0.5), 2), "p95_latency_s": round(_pct(lat, 0.95), 2),
        "cost_usd": round(sum(t["cost_usd"] for t in ts), 4),
        "cost_per_complaint": round(st.mean(t["cost_usd"] for t in ts), 4) if ts else 0,
        "tool_error_rate": sum(not t["checks"].get("tool_errors", (True,))[0] for t in ts) / len(ts) if ts else 0,
        "escalation_rate": sum(t["resolution"] in ("escalate", "clarify") for t in ts) / len(ts) if ts else 0,
        "override_rate": sum(t["feedback"]["outcome"] == "overridden" for t in fb) / len(fb) if fb else 0,
        "critical_fail_rate": len(crit) / len(ts) if ts else 0,
        "check_fail": {c: sum(1 for t in ts if c in t["checks"] and not t["checks"][c][0])
                       for c in sorted({c for t in ts for c in t["checks"]})},
        "check_n": dict(Counter(c for t in ts for c in t["checks"])),
        "resolutions": dict(Counter(t["resolution"] for t in ts)),
    }


def daily(traces):
    days = defaultdict(list)
    for t in traces:
        days[t["day"]].append(t)
    out = []
    for d in sorted(days):
        a = _agg(days[d])
        a["day"] = d.isoformat()
        a["version"] = Counter(t["agent_version"] for t in days[d]).most_common(1)[0][0]
        out.append(a)
    return out


def by_version(traces):
    vs = defaultdict(list)
    for t in traces:
        vs[t["agent_version"]].append(t)
    return {v: _agg(ts) for v, ts in sorted(vs.items())}


def weekly_drivers(traces, version=None):
    """Priya's view: complaints per driver / zone per week, as attributed by the agent from the order it
    resolved (the Spring weekly report reads the same links from complaint_emails.resolved_*).
    'Substantiated' excludes complaints the CS team finally rejected."""
    rows = defaultdict(lambda: {"complaints": 0, "substantiated": 0})
    for t in traces:
        if version and t["agent_version"] != version:
            continue
        if not t["driver_id"]:
            continue
        wk = (t["day"] - dt.timedelta(days=t["day"].weekday())).isoformat()
        final = t["feedback"]["final"] if t["feedback"] else t["resolution"]
        for key in (("driver", t["driver_id"]), ("zone", t["zone"] or "Unknown")):
            r = rows[(wk, *key)]
            r["complaints"] += 1
            r["substantiated"] += final not in ("reject",)
    return [{"week": k[0], "dim": k[1], "key": k[2], **v} for k, v in sorted(rows.items())]


def span_latency(tdb, env="prod"):
    rows = tdb.execute("SELECT s.name, s.kind, s.duration_ms, s.status FROM spans s JOIN traces t USING(trace_id) WHERE t.env=?", (env,))
    d = defaultdict(list)
    err = Counter()
    kinds = {}
    for name, kind, ms, status in rows:
        d[name].append(ms)
        err[name] += status == "error"
        kinds[name] = kind
    return [{"span": n, "kind": kinds[n], "calls": len(v), "p50_ms": round(_pct(v, .5)), "p95_ms": round(_pct(v, .95)),
             "error_rate": err[n] / len(v)} for n, v in sorted(d.items(), key=lambda x: -_pct(x[1], .5))]


def trace_detail(tdb, trace_id):
    spans = []
    for r in tdb.execute("SELECT span_id, parent_id, name, kind, start_ts, duration_ms, status, error, attrs FROM spans WHERE trace_id=? ORDER BY start_ts, parent_id IS NOT NULL", (trace_id,)):
        spans.append(dict(zip(["span_id", "parent_id", "name", "kind", "start", "ms", "status", "error", "attrs"], r)))
        spans[-1]["attrs"] = json.loads(spans[-1]["attrs"])
    checks = [dict(zip(["check", "passed", "severity", "detail"], r[1:]))
              for r in tdb.execute("SELECT * FROM checks WHERE trace_id=?", (trace_id,))]
    return {"trace_id": trace_id, "spans": spans, "checks": checks}
