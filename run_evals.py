"""The eval suite and release gate. One command:

    python run_evals.py               # all cases; LLM-quality cases are skipped when there is no OPENAI_API_KEY
    python run_evals.py --repeat 3    # run each case 3 times; a case that disagrees with itself is "flaky"

It runs the unit tests, every case in evals/cases.jsonl through the real triage(), and a PII scan of the
traces it produced. It writes evals/results/latest.md (commit it: it is your checklist evidence), compares
with the previous run, and exits with code 1 if any gate fails, so CI or a git hook can block the change.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

os.makedirs("evals/results", exist_ok=True)
os.environ["TRACES_DB"] = "evals/eval_traces.db"     # keep eval traces apart from your dev traces
if os.path.exists(os.environ["TRACES_DB"]):
    os.remove(os.environ["TRACES_DB"])

import agent                                         # noqa: E402  (must come after TRACES_DB is set)
from redact import PATTERNS, _card                  # noqa: E402

GATES = {  # metric: (comparison, threshold). Deterministic behaviour must be perfect; LLM quality can't be.
    "unit_tests_pass": ("==", True),
    "deterministic_pass_rate": (">=", 1.00),
    "refusal_pass_rate": (">=", 1.00),
    "kill_shot_pass_rate": (">=", 1.00),
    "llm_quality_pass_rate": (">=", 0.80),
    "pii_leaks": ("==", 0),
    "avg_cost_usd": ("<=", 0.01),
}


def run_case(case):
    out = agent.triage(case["email"], case["sender"])
    rows = agent.tracer.db.execute("SELECT name, duration_ms, attrs FROM spans WHERE trace_id=?",
                                   (agent.tracer.trace_id,)).fetchall()
    attrs = [json.loads(a) for _, _, a in rows]
    calls = sum("llm.model" in a for a in attrs)
    cost = sum(a.get("llm.cost_usd") or 0 for a in attrs)
    latency = next(ms for name, ms, _ in rows if name == "triage")
    return out, calls, cost, latency


def check(expect, out, calls):
    """Every expectation is checkable by code. Returns a list of reasons it failed."""
    why = []
    status = "refused" if out["resolution"] == "refused" else "answered"
    if expect.get("status") and status != expect["status"]:
        why.append(f"status {status}, expected {expect['status']}")
    if expect.get("rule") and out.get("rule") != expect["rule"]:
        why.append(f"rule {out.get('rule')}, expected {expect['rule']}")
    if expect.get("action_in") and out["resolution"] not in expect["action_in"]:
        why.append(f"action {out['resolution']}, expected one of {expect['action_in']}")
    if "max_model_calls" in expect and calls > expect["max_model_calls"]:
        why.append(f"{calls} model calls, expected at most {expect['max_model_calls']}")
    notes = " ".join(out.get("notes", []))
    for text in expect.get("notes_include", []):
        if text.lower() not in notes.lower():
            why.append(f"notes missing {text!r}")
    for text in expect.get("notes_exclude", []):
        if text.lower() in notes.lower():
            why.append(f"notes leak {text!r}")
    for src in expect.get("sources_include", []):
        if src not in out.get("sources", []):
            why.append(f"source {src} not cited (got {out.get('sources')})")
    for text in expect.get("reply_excludes", []):
        if text.lower() in out["reply"].lower():
            why.append(f"reply contains {text!r}")
    return why


def pii_leaks():
    leaks = []
    for (attrs,) in agent.tracer.db.execute("SELECT attrs || ' ' || status FROM spans"):
        for kind, pattern, _ in PATTERNS:
            for m in pattern.finditer(attrs):
                if kind != "card" or _card(m) != m.group():
                    leaks.append(f"{kind}: {m.group()}")
    return leaks


def rate(results, pred):
    picked = [r for r in results if pred(r) and r["result"] != "skipped"]
    return sum(r["result"] == "pass" for r in picked) / len(picked) if picked else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    online = bool(os.environ.get("OPENAI_API_KEY"))
    cases = [json.loads(line) for line in open("evals/cases.jsonl") if line.strip()]

    unit = subprocess.run([sys.executable, "test_redact.py"], capture_output=True, text=True)
    results, started = [], time.time()
    for case in cases:
        if case.get("needs_llm") and not online:
            results.append({"id": case["id"], "tags": case["tags"], "needs_llm": True, "result": "skipped",
                            "why": ["no OPENAI_API_KEY"]})
            continue
        runs = [run_case(case) for _ in range(args.repeat)]
        verdicts = [check(case["expect"], out, calls) for out, calls, _, _ in runs]
        passes = sum(not v for v in verdicts)
        result = "pass" if passes == len(runs) else "fail" if passes == 0 else "flaky"
        results.append({"id": case["id"], "tags": case["tags"], "needs_llm": case.get("needs_llm", False),
                        "result": result, "why": next((v for v in verdicts if v), []),
                        "model_calls": runs[0][1], "cost_usd": sum(r[2] for r in runs) / len(runs),
                        "latency_ms": max(r[3] for r in runs), "action": runs[0][0]["resolution"]})

    ran = [r for r in results if r["result"] != "skipped"]
    leaks = pii_leaks()
    lat = sorted(r["latency_ms"] for r in ran)
    m = {
        "unit_tests_pass": unit.returncode == 0,
        "deterministic_pass_rate": rate(results, lambda r: not r["needs_llm"]),
        "refusal_pass_rate": rate(results, lambda r: "refusal" in r["tags"]),
        "kill_shot_pass_rate": rate(results, lambda r: "kill-shot" in r["tags"]),
        "llm_quality_pass_rate": rate(results, lambda r: r["needs_llm"]),
        "pii_leaks": len(leaks),
        "avg_cost_usd": sum(r["cost_usd"] for r in ran) / len(ran) if ran else 0,
        "p95_latency_ms": lat[int(0.95 * (len(lat) - 1))] if lat else 0,
        "flaky": sum(r["result"] == "flaky" for r in results),
    }
    gates = {}
    for k, (op, th) in GATES.items():
        v = m[k]
        gates[k] = "skipped" if v is None else "pass" if {"==": v == th, ">=": v >= th, "<=": v <= th}[op] else "fail"
    passed = all(g != "fail" for g in gates.values())

    # Compare with the previous run, so a change that breaks a case that used to pass is called out.
    prev_path = "evals/results/latest.json"
    prev = {r["id"]: r["result"] for r in json.load(open(prev_path))["results"]} if os.path.exists(prev_path) else {}
    regressions = [r["id"] for r in results if prev.get(r["id"]) == "pass" and r["result"] in ("fail", "flaky")]
    fixed = [r["id"] for r in results if prev.get(r["id"]) in ("fail", "flaky") and r["result"] == "pass"]

    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    report = {"when": stamp, "commit": commit or "not a git repo", "mode": "online" if online else "offline",
              "repeat": args.repeat, "metrics": m, "gates": gates, "passed": passed,
              "regressions": regressions, "fixed": fixed, "pii_leaks": leaks, "results": results}
    with open(f"evals/results/{dt.datetime.now():%Y%m%d-%H%M%S}.json", "w") as f:
        json.dump(report, f, indent=1)
    with open(prev_path, "w") as f:
        json.dump(report, f, indent=1)

    fmt = lambda v: "n/a" if v is None else f"{v:.0%}" if isinstance(v, float) and v <= 1 and not isinstance(v, bool) else v
    lines = [f"# Eval results: {'PASS' if passed else 'FAIL'}", "",
             f"{stamp} · commit {report['commit']} · {report['mode']} · {len(ran)} of {len(cases)} cases run"
             f" · repeat {args.repeat} · {time.time() - started:.1f}s", "",
             "| Gate | Value | Threshold | Result |", "|---|---|---|---|"]
    for k, (op, th) in GATES.items():
        lines.append(f"| {k} | {fmt(m[k]) if k != 'avg_cost_usd' else f'US${m[k]:.5f}'} | {op} {th} | {gates[k]} |")
    lines += ["", f"p95 latency {m['p95_latency_ms']:.0f} ms · flaky cases {m['flaky']}", ""]
    if regressions:
        lines += ["**Regressions (passed last time, fail now):** " + ", ".join(regressions), ""]
    if fixed:
        lines += ["**Fixed since last run:** " + ", ".join(fixed), ""]
    lines += ["| Case | Tags | Result | Action | Model calls | Why |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {', '.join(r['tags'])} | {r['result']} | {r.get('action', '')} | "
                     f"{r.get('model_calls', '')} | {'; '.join(r['why'])} |")
    with open("evals/results/latest.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines[:6 + len(GATES) + 2]))
    for r in results:
        if r["result"] in ("fail", "flaky"):
            print(f"  {r['result'].upper():<6} {r['id']}: {'; '.join(r['why'])}")
    if regressions:
        print("  REGRESSIONS: " + ", ".join(regressions))
    print(f"\nRelease gate: {'PASS' if passed else 'FAIL'}   (details in evals/results/latest.md)")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()