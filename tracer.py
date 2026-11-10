import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from redact import redact

_LETTERS = str.maketrans("0123456789", "ghijklmnop")


def new_id(length):
    """A random ID made of letters only, so no ID can ever look like a phone, NRIC or card number."""
    return uuid.uuid4().hex[:length].translate(_LETTERS)

class Tracer:
    def __init__(self, path=None):
        path = path or os.environ.get("TRACES_DB", "traces.db")
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS spans(
            trace_id TEXT, span_id TEXT, parent_id TEXT, name TEXT,
            start REAL, duration_ms REAL, status TEXT, attrs TEXT)""")
        self.trace_id = None
        self.stack = []  # span ids of the spans currently open

    @contextmanager
    def span(self, name, **attrs):
        if not self.stack:                      # outermost span = a new trace
            self.trace_id = new_id(12)
        span_id = new_id(8)
        parent_id = self.stack[-1] if self.stack else None
        self.stack.append(span_id)
        start, status = time.time(), "ok"
        try:
            yield attrs                          # the caller can add attributes
        except Exception as e:
            status = f"error: {e}"
            raise
        finally:
            self.stack.pop()
            clean, n = redact(attrs)                 # nothing personal is ever written to disk
            status, n_status = redact(status)
            if n + n_status:
                clean["pii.redacted"] = n + n_status
            self.db.execute("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?)",
                            (self.trace_id, span_id, parent_id, name, start,
                             (time.time() - start) * 1000, status, json.dumps(clean)))
            self.db.commit()


# USD per 1M tokens: (input, cached input, output). Starting values from developers.openai.com/api/docs/pricing
# (Nov 2026). openai_billing.py overwrites them with the prices OpenAI actually billed, via prices.json.
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
}
if os.path.exists("prices.json"):
    with open("prices.json") as f:
        PRICES.update({m: tuple(p) for m, p in json.load(f).items()})


def price_for(model):
    """Exact model ID first, then without a date suffix (gpt-6-luna-2026-08-01 -> gpt-6-luna)."""
    if model in PRICES:
        return PRICES[model]
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model or "")
    return PRICES.get(base)


def record_llm(attrs, model, tokens_in, tokens_out, cached=0, estimated=False, resolved=None):
    """Put a model call's tokens and cost on the current span. An unknown model never crashes the request:
    its cost is left empty and flagged, so the report shows the gap instead of a wrong number."""
    p = price_for(resolved or model) or price_for(model)
    cost = None if p is None else round(((tokens_in - cached) * p[0] + cached * p[1] + tokens_out * p[2]) / 1_000_000, 6)
    attrs.update({"llm.model": model, "llm.model_resolved": resolved or model, "llm.tokens_in": tokens_in,
                  "llm.tokens_out": tokens_out, "llm.cached": cached, "llm.cost_usd": cost,
                  "llm.estimated": estimated, "llm.price_missing": p is None})