"""Daily metrics from the traces: volume, outcomes, honesty, speed and spend.

    python metrics.py                                # reads traces.db
    TRACES_DB=demo_traces.db python metrics.py       # reads the practice week
"""
import datetime as dt
import json
import os
import sqlite3
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()
SGT = dt.timezone(dt.timedelta(hours=8))
BUDGET = float(os.environ.get("BUDGET_USD", "20.00"))


def load_requests(path=None):
    """One record per complaint, built from its spans."""
    db = sqlite3.connect(path or os.environ.get("TRACES_DB", "traces.db"))
    spans = defaultdict(list)
    for trace_id, name, start, ms, status, attrs in db.execute(
            "SELECT trace_id, name, start, duration_ms, status, attrs FROM spans"):
        spans[trace_id].append((name, start, ms, status, json.loads(attrs)))
    out = []
    for trace_id, rows in spans.items():
        root = next((r for r in rows if r[0] == "triage"), None)
        if root is None:
            continue
        _, start, ms, status, a = root
        llm = [r[4] for r in rows if "llm.model" in r[4]]
        out.append({
            "trace_id": trace_id, "start": start, "latency_ms": ms,
            "resolution": a.get("resolution"), "refused_by": a.get("refused_by"),
            "degraded": a.get("degraded"), "sender_code": a.get("sender_code"),
            "errors": sum(r[3] != "ok" for r in rows),
            "model_calls": len(llm), "tokens": sum(x["llm.tokens_in"] + x["llm.tokens_out"] for x in llm),
            "cost": sum(x.get("llm.cost_usd") or 0 for x in llm),
            "output_blocked": any(r[0] == "output_guard" and r[4].get("blocked") for r in rows),
            "pii_removed": sum(r[4].get("pii.redacted", 0) for r in rows),
        })
    return sorted(out, key=lambda r: r["start"])


def pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))] if xs else 0


def summarize(reqs):
    n = len(reqs)
    refusals = Counter(r["refused_by"] for r in reqs if r["refused_by"])
    return {
        "requests": n,
        "refused": sum(refusals.values()),
        "refused_by": dict(refusals),
        "escalated_rate": sum(r["resolution"] in ("escalate", "clarify") for r in reqs) / n if n else 0,
        "degraded_rate": sum(bool(r["degraded"]) for r in reqs) / n if n else 0,
        "output_blocked": sum(r["output_blocked"] for r in reqs),
        "p50_ms": round(pct([r["latency_ms"] for r in reqs], .5)),
        "p95_ms": round(pct([r["latency_ms"] for r in reqs], .95)),
        "model_calls": sum(r["model_calls"] for r in reqs),
        "cost_usd": round(sum(r["cost"] for r in reqs), 6),
        "cost_per_request": round(sum(r["cost"] for r in reqs) / n, 6) if n else 0,
        "pii_removed": sum(r["pii_removed"] for r in reqs),
    }


def by_day(reqs):
    days = defaultdict(list)
    for r in reqs:
        days[dt.datetime.fromtimestamp(r["start"], SGT).date().isoformat()].append(r)
    return {d: summarize(rs) for d, rs in sorted(days.items())}


if __name__ == "__main__":
    reqs = load_requests()
    daily = by_day(reqs)
    print(f"{'day (SGT)':<12}{'reqs':>5}{'refused':>8}{'escal.':>7}{'degr.':>6}{'p95 ms':>8}{'calls':>6}{'US$':>10}  top refusals")
    for day, m in daily.items():
        top = ", ".join(f"{k} {v}" for k, v in Counter(m["refused_by"]).most_common(2))
        print(f"{day:<12}{m['requests']:>5}{m['refused']:>8}{m['escalated_rate']:>7.0%}{m['degraded_rate']:>6.0%}"
              f"{m['p95_ms']:>8}{m['model_calls']:>6}{m['cost_usd']:>10.5f}  {top}")
    week = summarize([r for r in reqs if r["start"] >= reqs[-1]["start"] - 7 * 86400]) if reqs else summarize([])
    print(f"\nlast 7 days: {week['requests']} requests, US${week['cost_usd']:.4f} spent "
          f"(US${week['cost_per_request']:.6f} per request), {week['cost_usd'] / BUDGET:.2%} of the US${BUDGET:.2f} budget")
    with open("metrics.json", "w") as f:
        json.dump({"daily": daily, "last_7_days": week}, f, indent=1)
    print("written to metrics.json")