"""Replay two weeks of the team's commits through the dev observability stack.

Each commit is a pipeline config (a version per step + the chat model) evaluated on the same
120-case dataset (90 golden + 30 promoted from production overrides) and gated against the latest
run on main. Writes out_dev/.

    python simulate_dev.py                        # synthetic merlion_clean (same seed as run_demo.py)
    MERLION_SOURCE=mysql python simulate_dev.py   # the real merlion_clean; dataset = labelled real emails
"""
import json
import os
import random

from merlion_devobs.analysis import clusters, diff_runs
from merlion_devobs.cli import latest_on
from merlion_devobs.runner import run_experiment
from merlion_obs import dataset, db, fixture, registry
from merlion_obs.agent import config

import sqlite3

OUT = "out_dev"
SOURCE = os.environ.get("MERLION_SOURCE", "fixture")

COMMITS = [  # (run_id, ts, sha, branch, author, message, config)
    ("r01", "2026-11-02T10:05:00+08:00", "a1c9e02", "main", "shafique", "Initial pipeline: resolveOrder, five tools, ChatClient",
     config(resolve="v1", tools="v1", chat="v1")),
    ("r02", "2026-11-03T15:40:00+08:00", "4be7713", "main", "angeline", "resolveOrder: score phone, name and date; ask when tied (T26)",
     config(resolve="v2", tools="v1", chat="v1")),
    ("r03", "2026-11-05T11:20:00+08:00", "9d02f4a", "main", "jason", "getGpsTrail: Singapore time = ts_epoch + 16 h (T16)",
     config(resolve="v2", tools="v2", chat="v1")),
    ("r04", "2026-11-06T09:55:00+08:00", "c37b19e", "feat/chat-nano", "vincent", "chat on the nano model to cut cost",
     config(resolve="v2", tools="v2", chat="v1", model="nano")),
    ("r05", "2026-11-09T16:10:00+08:00", "e5a8d30", "main", "angeline", "getRate: zone from orders.zone (town), not the postal sector (T04)",
     config(resolve="v2", tools="v3", chat="v1")),
    ("r06", "2026-11-11T14:30:00+08:00", "71f0c6b", "feat/recheck-conflicts", "vincent", "chat: re-confirm the order when sources disagree",
     config(resolve="v2", tools="v3", chat="v3")),
    ("r07", "2026-11-13T10:45:00+08:00", "b82e4d1", "main", "shafique", "System prompt: show both sides of a conflict, say when data is missing",
     config(resolve="v2", tools="v3", chat="v2")),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in ("devobs.db", "merlion_clean.db"):
        if os.path.exists(f"{OUT}/{f}"):
            os.remove(f"{OUT}/{f}")
    registry.write_files("prompts")
    if SOURCE == "fixture":
        fixture.build(f"{OUT}/merlion_clean.db")  # same seed: the same world as the production stack
    ops = db.connect(SOURCE, f"{OUT}/merlion_clean.db")
    labelled = dataset.load(ops, labelled_only=True)
    if not labelled:
        raise SystemExit("no labelled emails: fill golden/labels.json (python -m merlion_obs.dataset init)")
    ds = random.Random(23).sample(labelled, min(120, len(labelled)))
    for i, e in enumerate(ds):
        e["source"] = "golden" if i < 90 else "promoted_from_prod"
    with open(f"{OUT}/dataset.json", "w", encoding="utf-8") as f:
        json.dump(ds, f, default=str, indent=1)

    db_path = f"{OUT}/devobs.db"
    for rid, ts, sha, br, who, msg, c in COMMITS:
        con = sqlite3.connect(db_path)
        base = latest_on(con, "main")
        con.close()
        _, s, g = run_experiment(db_path, ops, ds, c, {"run_id": rid, "ts": ts, "git_sha": sha, "branch": br,
                                                       "author": who, "message": msg, "dataset": "dataset.json"}, baseline=base)
        fails = [k for k, ok in g.items() if not ok]
        print(f"{rid} {br:<22} acc {s['resolution_accuracy']:.1%} order {s['order_accuracy']:.1%} amount {s['amount_accuracy']:.1%} "
              f"${s['cost_per_case_usd']:.5f} p95 {s['p95_latency_s']}s loops {s['loops']} hidden {s['hidden_conflicts']} "
              f"broken {s['newly_broken']} -> {'PASS' if not fails else 'BLOCK ' + ','.join(fails)}"
              f"  | {', '.join(f'{k} {v:.0%}' for k, v in s['components'].items())}")

    con = sqlite3.connect(db_path)
    for a, b in [("r03", "r04"), ("r05", "r06"), ("r01", "r07")]:
        d = diff_runs(con, a, b)
        print(f"\ndiff {a}->{b}: {d['counts']} prompts {d['prompts_changed']} models {d['models_changed']}")
        for c in d["cases"].get("broken", [])[:5]:
            print("  broken", c["case_id"], c["category"], c.get("diverged_at"), c["sig_after"])
    for r in ("r01", "r07"):
        print(f"\nclusters {r}:")
        for c in clusters(con, r):
            print(f"  {c['n']:>3} {c['signature']}")


if __name__ == "__main__":
    main()
