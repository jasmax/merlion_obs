"""Scan every stored span for personal data. Should always print 0 leaks: evidence the redaction holds.
Exits with code 1 if it finds any, so you can run it in CI."""
import sqlite3

from redact import PATTERNS, _card

db = sqlite3.connect("traces.db")
leaks = 0
for trace_id, name, status, attrs in db.execute("SELECT trace_id, name, status, attrs FROM spans"):
    for kind, pattern, _ in PATTERNS:
        for m in pattern.finditer(attrs + " " + status):
            if kind == "card" and _card(m) == m.group():
                continue                         # long number that isn't a real card
            leaks += 1
            print(f"LEAK {kind} in trace {trace_id}, span {name}: {m.group()!r}")
spans = db.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
print(f"{spans} spans scanned, {leaks} personal data leaks")
raise SystemExit(1 if leaks else 0)