import json
import os
import sqlite3

BUDGET = float(os.environ.get("BUDGET_USD", "4.00"))
db = sqlite3.connect("traces.db")
calls = cost = 0
for (attrs,) in db.execute("SELECT attrs FROM spans"):
    a = json.loads(attrs)
    if "llm.cost_usd" in a:
        calls += 1
        cost += a["llm.cost_usd"] or 0
traces = db.execute("SELECT COUNT(DISTINCT trace_id) FROM spans").fetchone()[0]
print(f"{traces} requests, {calls} model calls, US${cost:.6f} spent of US${BUDGET:.2f} budget ({cost / BUDGET:.2%})")
if traces:
    print(f"average US${cost / traces:.6f} per request -> about {int((BUDGET - cost) / (cost / traces)) if cost else 'unlimited'} more requests left")