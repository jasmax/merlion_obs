"""Minimal OpenTelemetry-shaped tracer for the triage agent.

Every complaint run is one trace; every pipeline step, LLM call and tool call is a span.
Spans are written to SQLite (local, zero-infra) and exported as OTLP to Langfuse Cloud, the
team's AI tracing tool (see otlp.py), so the same trace can be opened there next to the
Spring Boot request's Railway logs (same request id).

PII (emails, SG phone numbers, NRIC/FIN, addresses, postal codes) is masked *before* anything
is persisted, so the trace store never holds raw customer identifiers.
"""
import contextvars
import json
import sqlite3
import uuid
from contextlib import contextmanager

from . import llm
from .clock import CLOCK
from .pii import PATTERNS as _PII, redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans(
  trace_id TEXT, span_id TEXT PRIMARY KEY, parent_id TEXT, name TEXT, kind TEXT,
  start_ts TEXT, end_ts TEXT, duration_ms REAL, status TEXT, error TEXT, attrs TEXT);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id);
CREATE TABLE IF NOT EXISTS traces(
  trace_id TEXT PRIMARY KEY, env TEXT, agent_version TEXT, email_id TEXT,
  start_ts TEXT, end_ts TEXT, duration_ms REAL, status TEXT,
  category TEXT, order_system TEXT, order_no TEXT, resolution TEXT, amount REAL,
  driver_id TEXT, zone TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  redactions INTEGER, attrs TEXT);
CREATE TABLE IF NOT EXISTS checks(
  trace_id TEXT, check_name TEXT, passed INTEGER, severity TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_checks_trace ON checks(trace_id);
CREATE TABLE IF NOT EXISTS feedback(
  trace_id TEXT PRIMARY KEY, ts TEXT, agent_resolution TEXT, final_resolution TEXT,
  outcome TEXT);
"""



class Span:
    def __init__(self, tracer, trace, name, kind, parent, agent=None):
        self.tracer, self.trace, self.name, self.kind, self.agent = tracer, trace, name, kind, agent
        self.span_id = uuid.uuid4().hex[:16]
        self.parent_id = parent.span_id if parent else None
        self.attrs = {}
        self.start = CLOCK.now()
        self.status, self.error = "ok", None

    def set(self, **attrs):
        self.attrs.update(attrs)
        return self

    def out(self, value):
        self.attrs["output"] = value
        return value

    def llm(self, model, prompt_ref, tokens_in, tokens_out):
        cost, basis = llm.cost(model, tokens_in, tokens_out)
        self.set(**{"llm.model": llm.model_id(model), "prompt.ref": prompt_ref, "llm.tokens_in": tokens_in,
                    "llm.tokens_out": tokens_out, "llm.cost_usd": round(cost, 6), "llm.cost_basis": basis})
        self.trace.tokens_in += tokens_in
        self.trace.tokens_out += tokens_out
        self.trace.cost += cost


class Trace:
    def __init__(self, env, version, email_id):
        self.trace_id = uuid.uuid4().hex
        self.env, self.version, self.email_id = env, version, email_id
        self.start = CLOCK.now()
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.redactions = 0
        self.out = {}
        self.status = "ok"


class Tracer:
    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self._span = contextvars.ContextVar("span", default=None)
        self._trace = contextvars.ContextVar("trace", default=None)

    @contextmanager
    def trace(self, env, version, email_id):
        t = Trace(env, version, email_id)
        tok = self._trace.set(t)
        try:
            yield t
        except Exception:
            t.status = "error"
        finally:
            self._trace.reset(tok)
            self._write_trace(t)

    @contextmanager
    def span(self, name, kind="tool", agent=None):
        t = self._trace.get()
        s = Span(self, t, name, kind, self._span.get(), agent)
        tok = self._span.set(s)
        try:
            yield s
        except Exception as e:
            s.status, s.error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            self._span.reset(tok)
            self._write_span(s)

    def _write_span(self, s):
        attrs, n = redact(s.attrs)
        s.trace.redactions += n
        end = CLOCK.now()
        self.db.execute(
            "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (s.trace.trace_id, s.span_id, s.parent_id, s.name, s.kind,
             s.start.isoformat(), end.isoformat(),
             (end - s.start).total_seconds() * 1000, s.status,
             redact(s.error)[0] if s.error else None, json.dumps(attrs, default=str)))

    def _write_trace(self, t):
        o, _ = redact(t.out)
        end = CLOCK.now()
        self.db.execute(
            "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.trace_id, t.env, t.version, t.email_id, t.start.isoformat(), end.isoformat(),
             (end - t.start).total_seconds() * 1000, t.status,
             o.get("category"), o.get("order_system"), o.get("order_no"),
             o.get("resolution"), o.get("amount"), o.get("driver_id"), o.get("zone"),
             t.tokens_in, t.tokens_out, round(t.cost, 6), t.redactions,
             json.dumps(o, default=str)))
        self.db.commit()
