"""OpenAI calls for either stack, recorded into the current span (the team's LLM is the OpenAI API,
key from the 31 Oct briefing; BuildGuide "TO CHECK"; brief: "No other paid AI services").

Model aliases keep configs stable while the team confirms which models the key allows:
    mini -> $OPENAI_MODEL_MINI (default gpt-4o-mini)      nano -> $OPENAI_MODEL_NANO (default gpt-4.1-nano)

Modes (env LLM_MODE):
  sim     (default) no API call: the pipeline simulates the model deterministically. Used by CI and the demo.
  live    always call the API (production)
  record  call the API and save every response to the cache
  replay  serve from the cache when the exact request was seen before, else call and save

Replay keeps run-to-run diffs exact: an unchanged prompt + model + input returns the recorded
answer, so only steps whose prompt, model or input changed make new calls.

Cost: prices below are list prices per 1M tokens as we last knew them. They are ESTIMATES: set
LLM_PRICES='{"gpt-4o-mini": [0.15, 0.60]}' to the class provider's real pricing. A model with no
known price is costed at 0 and its spans carry llm.cost_estimated = "unknown price".
"""
import hashlib
import json
import os
import sqlite3

from .db import load_env

load_env()
MODE = os.environ.get("LLM_MODE", "sim")
CACHE = os.environ.get("LLM_CACHE", "llm_cache.db")
ALIASES = {"mini": os.environ.get("OPENAI_MODEL_MINI", "gpt-4o-mini"),
           "nano": os.environ.get("OPENAI_MODEL_NANO", "gpt-4.1-nano")}
PRICES = {  # USD per 1M tokens (input, output), estimates
    "gpt-4o-mini": (0.15, 0.60), "gpt-4.1-nano": (0.10, 0.40), "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00), "gpt-4.1": (2.00, 8.00),
    **{k: tuple(v) for k, v in json.loads(os.environ.get("LLM_PRICES", "{}")).items()},
}

_client = None
_db = None


def model_id(alias):
    return ALIASES.get(alias, alias)


def cost(model, tokens_in, tokens_out):
    p = PRICES.get(model_id(model))
    if not p:
        return 0.0, "unknown price"
    return tokens_in / 1e6 * p[0] + tokens_out / 1e6 * p[1], "estimate from list price"


def _cache():
    global _db
    if _db is None:
        _db = sqlite3.connect(CACHE)
        _db.execute("CREATE TABLE IF NOT EXISTS calls(key TEXT PRIMARY KEY, msg TEXT, tin INTEGER, tout INTEGER)")
    return _db


def _api():
    global _client
    if _client is None:
        try:
            from openai import OpenAI  # imported lazily so the simulated stack needs no key or package
        except ImportError:
            raise SystemExit("LLM_MODE=live/record/replay needs `pip install openai`")
        _client = OpenAI()  # reads OPENAI_API_KEY (and OPENAI_BASE_URL if the class gave one)
    return _client


def chat(span, alias, messages, tools=None, prompt_ref=None, max_tokens=800):
    """One chat completion. Records tokens, cost, model and prompt on `span`. Returns the message dict
    {"content": str|None, "tool_calls": [{"id", "name", "arguments"}]}."""
    mid = model_id(alias)
    key = hashlib.sha256(json.dumps([mid, messages, tools, max_tokens], sort_keys=True, default=str).encode()).hexdigest()
    hit = None
    if MODE in ("replay", "record"):
        hit = _cache().execute("SELECT msg, tin, tout FROM calls WHERE key=?", (key,)).fetchone()
    if hit and MODE == "replay":
        msg, tin, tout = json.loads(hit[0]), hit[1], hit[2]
    else:
        kw = {"tools": tools} if tools else {}
        r = _api().chat.completions.create(model=mid, messages=messages, max_tokens=max_tokens, **kw)
        m = r.choices[0].message
        msg = {"content": m.content, "tool_calls": [{"id": t.id, "name": t.function.name, "arguments": t.function.arguments}
                                                    for t in (m.tool_calls or [])]}
        tin, tout = r.usage.prompt_tokens, r.usage.completion_tokens
        if MODE in ("replay", "record"):
            _cache().execute("INSERT OR REPLACE INTO calls VALUES (?,?,?,?)", (key, json.dumps(msg), tin, tout))
            _cache().commit()
    span.set(**{"llm.cache": "hit" if hit and MODE == "replay" else "miss"})
    span.llm(alias, prompt_ref, tin, tout)
    return msg
