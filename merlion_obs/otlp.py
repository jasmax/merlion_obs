"""Export the SQLite trace store as OTLP/JSON and, optionally, push it to Langfuse Cloud, the team's AI
tracing tool (architecture.md "Logging and observability").

Spans carry the OpenTelemetry GenAI attributes Langfuse maps onto generations (gen_ai.request.model,
gen_ai.usage.input_tokens / output_tokens) plus langfuse.* trace attributes, so a trace opens in
Langfuse with model, tokens and estimated cost on each LLM call and the tool calls nested under it.

    python -m merlion_obs.otlp out/traces.db --push        # needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
                                                           # and LANGFUSE_HOST (default https://cloud.langfuse.com)
The push uses Langfuse's OTLP endpoint (/api/public/otel/v1/traces) with HTTP/JSON and basic auth.
"""
import base64
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.request

from .db import load_env


def _ns(iso):
    return str(int(dt.datetime.fromisoformat(iso).timestamp() * 1e9))


def _val(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": v if isinstance(v, str) else json.dumps(v, default=str)}


KIND = {"agent": 1, "llm": 3, "tool": 3, "handoff": 1}  # INTERNAL / CLIENT
GENAI = {"llm.model": "gen_ai.request.model", "llm.tokens_in": "gen_ai.usage.input_tokens",
         "llm.tokens_out": "gen_ai.usage.output_tokens", "llm.cost_usd": "gen_ai.usage.cost"}


def build(tdb, env="prod", limit=None):
    q = ("SELECT s.*, t.agent_version, t.email_id FROM spans s JOIN traces t USING(trace_id) "
         "WHERE t.env=? ORDER BY s.trace_id, s.start_ts")
    spans = []
    for (tid, sid, pid, name, kind, start, end, _ms, status, err, attrs, version, email_id) in tdb.execute(q, (env,)):
        a = json.loads(attrs)
        for old, new in GENAI.items():
            if old in a:
                a[new] = a[old]
        a["openinference.span.kind"] = {"agent": "AGENT", "llm": "LLM", "tool": "TOOL", "handoff": "CHAIN"}[kind]
        if kind == "llm":
            a["langfuse.observation.type"] = "generation"
        if not pid:
            a.update({"langfuse.trace.name": "triage", "langfuse.trace.tags": [env, version],
                      "langfuse.trace.metadata.email_id": email_id, "langfuse.release": version})
        spans.append({
            "traceId": tid, "spanId": sid, **({"parentSpanId": pid} if pid else {}),
            "name": name, "kind": KIND[kind], "startTimeUnixNano": _ns(start), "endTimeUnixNano": _ns(end),
            "attributes": [{"key": k, "value": _val(v)} for k, v in a.items() if v is not None],
            "status": {"code": 2, "message": err} if status == "error" else {"code": 1},
        })
        if limit and len(spans) >= limit:
            break
    return spans


def _doc(spans):
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "merlion-triage"}}]},
        "scopeSpans": [{"scope": {"name": "merlion_obs"}, "spans": spans}]}]}


def export(tdb, path, env="prod", limit=None):
    spans = build(tdb, env, limit)
    with open(path, "w") as f:
        json.dump(_doc(spans), f)
    return len(spans)


def push(tdb, env="prod", batch=400):
    """Send every span of `env` to Langfuse. Returns the number of spans sent."""
    load_env()
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (pk and sk):
        raise SystemExit("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (Langfuse > Settings > API keys)")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    spans, sent = build(tdb, env), 0
    for i in range(0, len(spans), batch):
        req = urllib.request.Request(f"{host}/api/public/otel/v1/traces", data=json.dumps(_doc(spans[i:i + batch])).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status >= 300:
                raise SystemExit(f"Langfuse returned HTTP {r.status}")
        sent += len(spans[i:i + batch])
    return sent


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "out/traces.db"
    tdb = sqlite3.connect(path)
    if "--push" in sys.argv:
        print(f"{push(tdb)} spans sent to Langfuse")
    else:
        print(f"{export(tdb, 'out/otlp_traces.json')} spans -> out/otlp_traces.json")
