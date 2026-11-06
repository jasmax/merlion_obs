"""Replay two weeks of the dev team's commits through the dev observability stack.

Each commit is a config (prompt versions + models) evaluated on the same
120-case dataset (90 golden + 30 promoted from production overrides) and gated
against the latest run on main. Writes out_dev/.
"""
import copy
import json
import os
import random
import sqlite3

from merlion_devobs import prompts
from merlion_devobs.analysis import clusters, diff_runs
from merlion_devobs.cli import latest_on
from merlion_devobs.runner import run_experiment
from merlion_obs import data

OUT = "out_dev"
BASE = {"prompts": {"router": "v1", "order_resolver": "v1", "evidence": "v1", "policy": "v1", "writer": "v1"},
        "models": {"router": "sonnet", "order_resolver": "sonnet", "evidence": "sonnet", "policy": "sonnet", "writer": "sonnet"},
        "max_hops": 8}


def cfg(**changes):
    c = copy.deepcopy(BASE)
    for k, v in changes.items():
        sect, agent = k.split("__")
        c[sect][agent] = v
    return c


COMMITS = [  # (run_id, ts, sha, branch, author, message, config)
    ("r01", "2026-10-22T10:05:00+08:00", "a1c9e02", "main", "wei.ling", "Initial triage agent network",
     cfg()),
    ("r02", "2026-10-23T15:40:00+08:00", "4be7713", "main", "arjun", "order_resolver: query v1 and v2, disambiguate by email + date",
     cfg(prompts__order_resolver="v2")),
    ("r03", "2026-10-26T11:20:00+08:00", "9d02f4a", "main", "wei.ling", "evidence: treat delivered_at as SGT before GPS lookup",
     cfg(prompts__order_resolver="v2", prompts__evidence="v2")),
    ("r04", "2026-10-28T09:55:00+08:00", "c37b19e", "feat/policy-haiku", "daniel", "policy agent on haiku to cut cost",
     cfg(prompts__order_resolver="v2", prompts__evidence="v2", models__policy="haiku")),
    ("r05", "2026-10-29T16:10:00+08:00", "e5a8d30", "main", "arjun", "evidence: validate postal codes, fall back to address geocode",
     cfg(prompts__order_resolver="v2", prompts__evidence="v3")),
    ("r06", "2026-10-31T14:30:00+08:00", "71f0c6b", "feat/router-verify", "daniel", "router: confirm category with order_resolver before handoff",
     cfg(prompts__order_resolver="v2", prompts__evidence="v3", prompts__router="v3")),
    ("r07", "2026-11-03T10:45:00+08:00", "b82e4d1", "main", "wei.ling", "router: few-shot examples for category edge cases",
     cfg(prompts__order_resolver="v2", prompts__evidence="v3", prompts__router="v2")),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in ("devobs.db", "merlion_ops.db"):
        if os.path.exists(f"{OUT}/{f}"):
            os.remove(f"{OUT}/{f}")
    prompts.write_files("prompts")
    complaints = data.build(f"{OUT}/merlion_ops.db")  # same seed: the same world as the prod stack
    ops = sqlite3.connect(f"{OUT}/merlion_ops.db")
    rng = random.Random(23)
    ds = rng.sample(complaints, 120)
    for i, e in enumerate(ds):
        e["source"] = "golden" if i < 90 else "promoted_from_prod"
    with open(f"{OUT}/dataset.json", "w") as f:
        json.dump(ds, f, default=str, indent=1)

    db_path = f"{OUT}/devobs.db"
    for rid, ts, sha, br, who, msg, c in COMMITS:
        db = sqlite3.connect(db_path)
        base = latest_on(db, "main")
        db.close()
        _, s, g = run_experiment(db_path, ops, ds, c, {"run_id": rid, "ts": ts, "git_sha": sha, "branch": br,
                                                       "author": who, "message": msg, "dataset": "dataset.json"}, baseline=base)
        fails = [k for k, ok in g.items() if not ok]
        print(f"{rid} {br:<20} acc {s['resolution_accuracy']:.1%} order {s['order_accuracy']:.1%} amount {s['amount_accuracy']:.1%} "
              f"${s['cost_per_case_usd']:.4f} p95 {s['p95_latency_s']}s loops {s['loops']} broken {s['newly_broken']} "
              f"-> {'PASS' if not fails else 'BLOCK ' + ','.join(fails)}  | comps {', '.join(f'{k} {v:.0%}' for k, v in s['components'].items())}")

    db = sqlite3.connect(db_path)
    for a, b in [("r03", "r04"), ("r05", "r06"), ("r01", "r07")]:
        d = diff_runs(db, a, b)
        print(f"\ndiff {a}->{b}: {d['counts']} prompts {d['prompts_changed']} models {d['models_changed']}")
        for c in d["cases"].get("broken", [])[:5]:
            print("  broken", c["case_id"], c["category"], c.get("diverged_at"), c["sig_after"])
    print("\nclusters r01:")
    for c in clusters(db, "r01"):
        print(f"  {c['n']:>3} {c['signature']}")
    print("clusters r07:")
    for c in clusters(db, "r07"):
        print(f"  {c['n']:>3} {c['signature']}")


if __name__ == "__main__":
    main()
