"""Dev-loop tracer: every span belongs to a run (one commit/config evaluated on a
dataset) and a case (one dataset email). Adds what developers need on top of the
production tracer: pipeline step, prompt/step ref, model, handoffs between steps, and an
output digest on every span so two runs can be compared span by span.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager

from merlion_obs import llm
from merlion_obs.clock import CLOCK
from merlion_obs.pii import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, ts TEXT, git_sha TEXT, branch TEXT,
  author TEXT, message TEXT, dataset TEXT, config TEXT, prompts TEXT, summary TEXT, gate TEXT);
CREATE TABLE IF NOT EXISTS cases(run_id TEXT, case_id TEXT, trace_id TEXT, source TEXT,
  category TEXT, expected TEXT, got TEXT, passed INTEGER, components TEXT, blame TEXT,
  failure_sig TEXT, latency_ms REAL, cost_usd REAL, tokens INTEGER, hops INTEGER, loop INTEGER,
  PRIMARY KEY(run_id, case_id));
CREATE TABLE IF NOT EXISTS spans(run_id TEXT, case_id TEXT, trace_id TEXT, span_id TEXT,
  parent_id TEXT, name TEXT, kind TEXT, agent TEXT, start_ms REAL, dur_ms REAL, status TEXT,
  error TEXT, attrs TEXT, out_digest TEXT);
CREATE INDEX IF NOT EXISTS ix_sp ON spans(run_id, case_id);
"""


def digest(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


class Span:
    def __init__(self, ctx, name, kind, agent, parent):
        self.ctx, self.name, self.kind, self.agent = ctx, name, kind, agent
        self.span_id, self.parent_id = uuid.uuid4().hex[:16], parent.span_id if parent else None
        self.start, self.attrs, self.output = CLOCK.now(), {}, None
        self.status, self.error = "ok", None

    def set(self, **kw):
        self.attrs.update(kw)
        return self

    def out(self, value):
        self.output = value
        self.attrs["output"] = value
        return value

    def llm(self, model, prompt_ref, tin, tout):
        cost, basis = llm.cost(model, tin, tout)
        self.set(**{"llm.model": llm.model_id(model), "prompt.ref": prompt_ref, "llm.tokens_in": tin,
                    "llm.tokens_out": tout, "llm.cost_usd": round(cost, 6), "llm.cost_basis": basis})
        self.ctx["cost"] += cost
        self.ctx["tokens"] += tin + tout


class DevTracer:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.ctx, self.stack = None, []

    @contextmanager
    def case(self, run_id, case_id):
        self.ctx = {"run_id": run_id, "case_id": case_id, "trace_id": uuid.uuid4().hex,
                    "t0": CLOCK.now(), "cost": 0.0, "tokens": 0, "hops": 0, "spans": []}
        try:
            yield self.ctx
        finally:
            self.ctx["latency_ms"] = (CLOCK.now() - self.ctx["t0"]).total_seconds() * 1000
            self._flush()

    @contextmanager
    def span(self, name, kind="tool", agent=None):
        parent = self.stack[-1] if self.stack else None
        s = Span(self.ctx, name, kind, agent or (parent.agent if parent else None), parent)
        self.stack.append(s)
        try:
            yield s
        except Exception as e:
            s.status, s.error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            self.stack.pop()
            s.end = CLOCK.now()
            self.ctx["spans"].append(s)

    def handoff(self, src, dst, payload):
        self.ctx["hops"] += 1
        with self.span(f"handoff {src} → {dst}", "handoff", src) as s:
            s.set(**{"handoff.from": src, "handoff.to": dst, "handoff.hop": self.ctx["hops"]})
            s.out(payload)

    def _flush(self):
        c, t0 = self.ctx, self.ctx["t0"]
        rows = []
        for s in c["spans"]:
            attrs, _ = redact(s.attrs)
            rows.append((c["run_id"], c["case_id"], c["trace_id"], s.span_id, s.parent_id, s.name, s.kind,
                         s.agent, (s.start - t0).total_seconds() * 1000, (s.end - s.start).total_seconds() * 1000,
                         s.status, s.error, json.dumps(attrs, default=str),
                         digest(s.output) if s.output is not None else None))
        self.db.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
