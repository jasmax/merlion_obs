"""Render out_dev/dev_dashboard.html from out_dev/devobs.db (run after simulate_dev.py)."""
import json
import sqlite3
from collections import Counter

from merlion_devobs.analysis import clusters
from merlion_obs.registry import PROMPTS, phash

import os

EXTRA = ("prompt.ref", "llm.model", "llm.cost_usd", "order.basis", "order.candidates", "order.status", "order.reason",
         "gps.epoch_window", "gps.pings_in_window", "gps.min_dist_m", "gps.near_town", "delivery.zone", "delivery.zone_basis",
         "rate.zone", "rate.zone_basis", "rate.expected_price", "rate.guessed", "chat.tools", "chat.conflicts_found",
         "chat.conflicts_disclosed", "check.problems", "tool.gaps", "handoff.hop")
db = sqlite3.connect(os.environ.get("DEVOBS_DB", "out_dev/devobs.db"))
runs = []
for rid, ts, sha, br, who, msg, cfg, prompts, summ, gate in db.execute(
        "SELECT run_id, ts, git_sha, branch, author, message, config, prompts, summary, gate FROM runs ORDER BY ts"):
    runs.append({"id": rid, "ts": ts, "sha": sha, "branch": br, "author": who, "message": msg,
                 "config": json.loads(cfg), "prompts": json.loads(prompts), "summary": json.loads(summ),
                 "gate": json.loads(gate), "clusters": clusters(db, rid)})

cases = {}
for rid, cid, cat, src, exp, got, passed, comps, blame, sig, lat, cost, hops, loop in db.execute(
        "SELECT run_id, case_id, category, source, expected, got, passed, components, blame, failure_sig, latency_ms, cost_usd, hops, loop FROM cases"):
    cases.setdefault(rid, {})[cid] = [cat, src, json.loads(exp), json.loads(got), passed, json.loads(comps), blame, sig,
                                      round(lat), round(cost, 5), hops, loop]

spans = {}
for rid, cid, name, kind, agent, start, dur, status, attrs, dg in db.execute(
        "SELECT run_id, case_id, name, kind, agent, start_ms, dur_ms, status, attrs, out_digest FROM spans ORDER BY run_id, case_id, start_ms, "
        "CASE WHEN name='triage' THEN 0 WHEN kind='handoff' THEN 1 WHEN kind='agent' THEN 2 ELSE 3 END"):
    a = json.loads(attrs)
    out = a.get("output")
    extra = {k: v for k, v in a.items() if k in EXTRA}
    spans.setdefault(rid, {}).setdefault(cid, []).append(
        [name, kind, agent, round(start), round(dur), status, json.dumps(out, default=str)[:180] if out is not None else "", dg, extra])

registry = []
for ag, vs in PROMPTS.items():
    for v, text in vs.items():
        pref = f"{ag}@{v}#{phash(ag, v)}"
        registry.append({"agent": ag, "version": v, "hash": phash(ag, v), "text": text,
                         "runs": [r["id"] for r in runs if r["prompts"].get(ag) == pref]})

ds = json.load(open(os.environ.get("DEVOBS_DATA", "out_dev/dataset.json"), encoding="utf-8"))
data = {"runs": runs, "cases": cases, "spans": spans, "registry": registry,
        "dataset": {"n": len(ds), "sources": dict(Counter(e["source"] for e in ds)),
                    "categories": dict(Counter(e["truth"]["category"] for e in ds))},
        "source": os.environ.get("MERLION_SOURCE", "fixture")}
tpl = open("dashboard/dev_template.html", encoding="utf-8").read()
html = tpl.replace("/*DATA*/null", json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/"))
open("out_dev/dev_dashboard.html", "w", encoding="utf-8").write(html)
print(f"out_dev/dev_dashboard.html ({len(html) / 1e6:.2f} MB)")
