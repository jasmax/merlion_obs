import json
import sqlite3

db = sqlite3.connect("traces.db")
for (trace_id,) in db.execute("SELECT DISTINCT trace_id FROM spans ORDER BY rowid"):
    rows = db.execute("SELECT span_id, parent_id, name, duration_ms, status, attrs "
                      "FROM spans WHERE trace_id=? ORDER BY start", (trace_id,)).fetchall()
    tokens = cost = 0
    estimated = False
    for *_, attrs in rows:
        a = json.loads(attrs)
        tokens += a.get("llm.tokens_in", 0) + a.get("llm.tokens_out", 0)
        cost += a.get("llm.cost_usd") or 0
        estimated |= a.get("llm.estimated", False)
    print(f"trace {trace_id}   {tokens} tokens   US${cost:.6f}{'  (estimated)' if estimated else ''}")
    depth = {}
    for span_id, parent_id, name, ms, status, attrs in rows:
        depth[span_id] = depth.get(parent_id, -1) + 1
        print(f"  {'  ' * depth[span_id]}{name:<14} {ms:7.1f} ms  {status}  {json.loads(attrs)}")