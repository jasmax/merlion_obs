"""Spend and usage per model, from your own traces."""
import json
import os
import sqlite3
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()
BUDGET = float(os.environ.get("BUDGET_USD", "5.00"))

db = sqlite3.connect("traces.db")
rows = defaultdict(lambda: {"calls": 0, "in": 0, "cached": 0, "out": 0, "cost": 0.0, "steps": set(),
                            "estimated": 0, "missing": 0, "resolved": set()})
for name, attrs in db.execute("SELECT name, attrs FROM spans"):
    a = json.loads(attrs)
    if "llm.model" not in a:
        continue
    r = rows[a["llm.model"]]
    r["calls"] += 1
    r["in"] += a["llm.tokens_in"]
    r["cached"] += a["llm.cached"]
    r["out"] += a["llm.tokens_out"]
    r["cost"] += a["llm.cost_usd"] or 0
    r["steps"].add(name)
    r["estimated"] += a["llm.estimated"]
    r["missing"] += a.get("llm.price_missing", False)
    r["resolved"].add(a.get("llm.model_resolved", a["llm.model"]))

total = sum(r["cost"] for r in rows.values())
print(f"{'model':<16}{'calls':>6}{'in':>9}{'cached':>8}{'out':>7}{'US$':>11}{'share':>7}  used by")
for model, r in sorted(rows.items(), key=lambda x: -x[1]["cost"]):
    flags = (" ESTIMATED" if r["estimated"] else "") + (f" NO PRICE for {r['missing']} calls" if r["missing"] else "")
    snap = "" if r["resolved"] == {model} else f"  (served as {', '.join(sorted(r['resolved']))})"
    print(f"{model:<16}{r['calls']:>6}{r['in']:>9}{r['cached']:>8}{r['out']:>7}{r['cost']:>11.6f}"
          f"{(r['cost'] / total if total else 0):>7.0%}  {', '.join(sorted(r['steps']))}{snap}{flags}")
print(f"total US${total:.6f} of US${BUDGET:.2f} budget ({total / BUDGET:.2%})")