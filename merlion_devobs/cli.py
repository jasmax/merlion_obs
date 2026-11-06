"""Developer CLI.

  python -m merlion_devobs.cli runs                              # run history
  python -m merlion_devobs.cli run --config configs/main.json -m "msg" --baseline main   # exit 1 if gated
  python -m merlion_devobs.cli diff <run_a> <run_b>              # fixed / broken cases + where they diverged
  python -m merlion_devobs.cli clusters <run>                    # failures grouped by signature
  python -m merlion_devobs.cli show <run> <case_id>              # span tree of one case
  python -m merlion_devobs.cli promote ../out/traces.db          # prod overrides -> dataset candidates
"""
import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys

from .analysis import clusters, diff_runs, promote_from_prod
from .runner import GATES, run_experiment

import os

DB = os.environ.get("DEVOBS_DB", "out_dev/devobs.db")
OPS = os.environ.get("DEVOBS_OPS", "out_dev/merlion_ops.db")
DATA = os.environ.get("DEVOBS_DATA", "out_dev/dataset.json")


def load_dataset(path=DATA):
    ds = json.load(open(path))
    for e in ds:
        e["received_at"] = dt.datetime.fromisoformat(e["received_at"])
    return ds


def latest_on(db, branch):
    from .tracer import SCHEMA
    db.executescript(SCHEMA)
    r = db.execute("SELECT run_id FROM runs WHERE branch=? ORDER BY ts DESC LIMIT 1", (branch,)).fetchone()
    return r[0] if r else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="devobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("runs")
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("-m", "--message", default="")
    r.add_argument("--branch", default=None)
    r.add_argument("--baseline", default="main", help="branch name or run id to diff against")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    c = sub.add_parser("clusters")
    c.add_argument("run")
    s = sub.add_parser("show")
    s.add_argument("run")
    s.add_argument("case")
    p = sub.add_parser("promote")
    p.add_argument("prod_db")
    p.add_argument("--out", default="out_dev/candidates.jsonl")
    a = ap.parse_args(argv)
    db = sqlite3.connect(DB)

    if a.cmd == "runs":
        for rid, ts, sha, br, msg, summ, gate in db.execute("SELECT run_id, ts, git_sha, branch, message, summary, gate FROM runs ORDER BY ts"):
            sm, g = json.loads(summ), json.loads(gate)
            print(f"{rid}  {ts[:16]}  {br:<22} {sha}  acc {sm['resolution_accuracy']:.0%}  ${sm['cost_per_case_usd']:.4f}/case  "
                  f"p95 {sm['p95_latency_s']:.1f}s  {'PASS' if all(g.values()) else 'BLOCK'}  {msg}")
    elif a.cmd == "run":
        cfg = json.load(open(a.config))
        try:
            sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
            branch = a.branch or subprocess.check_output(["git", "branch", "--show-current"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            sha, branch = "local", a.branch or "local"
        base = latest_on(db, a.baseline) or a.baseline
        rid, summ, gate = run_experiment(DB, sqlite3.connect(OPS), load_dataset(), cfg,
                                         {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "git_sha": sha, "branch": branch,
                                          "message": a.message, "dataset": DATA}, baseline=base)
        print(f"run {rid}  (baseline {base})")
        for k, (op, th) in GATES.items():
            print(f"  {'✓' if gate[k] else '✗'} {k:<22} {summ[k]}  (gate {op} {th})")
        sys.exit(0 if all(gate.values()) else 1)
    elif a.cmd == "diff":
        dd = diff_runs(db, a.a, a.b)
        print(f"{a.a} -> {a.b}   prompts changed: {dd['prompts_changed'] or '-'}   models changed: {dd['models_changed'] or '-'}")
        for k, (x, y) in dd["metrics"].items():
            print(f"  {k:<22} {x} -> {y}")
        print("  " + "  ".join(f"{k}={v}" for k, v in dd["counts"].items()))
        for st in ("broken", "fixed"):
            for c in dd["cases"].get(st, []):
                print(f"  {st.upper():<7} {c['case_id']} {c['category']:<16} diverged at {c.get('diverged_at')}  {c['sig_after'] or c['sig_before']}")
    elif a.cmd == "clusters":
        for c in clusters(db, a.run):
            print(f"{c['n']:>4}  {c['signature']}  {c['categories']}  e.g. {', '.join(c['examples'])}")
    elif a.cmd == "show":
        for name, kind, agent, start, dur, status, out in db.execute(
                "SELECT name, kind, agent, start_ms, dur_ms, status, json_extract(attrs,'$.output') FROM spans WHERE run_id=? AND case_id=? ORDER BY start_ms, CASE WHEN name='triage_network' THEN 0 WHEN kind='handoff' THEN 1 WHEN kind='agent' THEN 2 ELSE 3 END",
                (a.run, a.case)):
            print(f"{start / 1000:7.2f}s {dur:8.0f}ms  {kind:<8} {name:<34} {status:<5} {str(out)[:80]}")
    elif a.cmd == "promote":
        print(f"{promote_from_prod(a.prod_db, a.out)} candidates -> {a.out}")


if __name__ == "__main__":
    main()
