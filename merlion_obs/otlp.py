"""Export the SQLite trace store as OTLP/JSON, so the same traces load into
Arize Phoenix, Langfuse, Jaeger, Grafana Tempo or any OTel collector:

    curl -X POST http://localhost:4318/v1/traces -H 'Content-Type: application/json' -d @out/otlp_traces.json
"""
import datetime as dt
import json


def _ns(iso):
    return str(int(dt.datetime.fromisoformat(iso).timestamp() * 1e9))


def _val(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": v if isinstance(v, str) else json.dumps(v)}


KIND = {"agent": 1, "llm": 3, "tool": 3}  # INTERNAL / CLIENT


def export(tdb, path, env="prod", limit=None):
    q = "SELECT s.* FROM spans s JOIN traces t USING(trace_id) WHERE t.env=? ORDER BY s.trace_id, s.start_ts"
    spans = []
    for (tid, sid, pid, name, kind, start, end, _ms, status, err, attrs) in tdb.execute(q, (env,)):
        a = json.loads(attrs)
        a["openinference.span.kind"] = {"agent": "AGENT", "llm": "LLM", "tool": "TOOL"}[kind]
        spans.append({
            "traceId": tid, "spanId": sid, **({"parentSpanId": pid} if pid else {}),
            "name": name, "kind": KIND[kind], "startTimeUnixNano": _ns(start), "endTimeUnixNano": _ns(end),
            "attributes": [{"key": k, "value": _val(v)} for k, v in a.items() if v is not None],
            "status": {"code": 2, "message": err} if status == "error" else {"code": 1},
        })
        if limit and len(spans) >= limit:
            break
    doc = {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "merlion-triage-agent"}}]},
        "scopeSpans": [{"scope": {"name": "merlion_obs"}, "spans": spans}]}]}
    with open(path, "w") as f:
        json.dump(doc, f)
    return len(spans)
