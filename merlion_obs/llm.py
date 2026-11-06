"""Real Claude calls for either stack, recorded into the current span.

    from merlion_obs.llm import claude
    with tracer.span("router.classify", "llm") as s:
        text = claude(s, "sonnet", system=PROMPT, user=email_body, prompt_ref=ref("router", "v2"))

Modes (env LLM_MODE):
  live    always call the API (production)
  record  call the API and save every response to the cache (CI on main)
  replay  serve from the cache when the exact request was seen before, else call
          and save (CI on branches, local dev)

Why replay matters for the dev stack: current Claude models reject `temperature`,
so two calls with the same input can differ. Replaying unchanged requests means
an agent whose prompt, model and input did not change returns the same output,
which keeps run-to-run diffs exact. Changed prompts miss the cache and hit the API.

Needs: pip install anthropic, and ANTHROPIC_API_KEY in the environment.
"""
import hashlib
import json
import os
import sqlite3

MODEL_IDS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
MODE = os.environ.get("LLM_MODE", "live")
CACHE = os.environ.get("LLM_CACHE", "llm_cache.db")

_client = None
_db = None


def _cache():
    global _db
    if _db is None:
        _db = sqlite3.connect(CACHE)
        _db.execute("CREATE TABLE IF NOT EXISTS calls(key TEXT PRIMARY KEY, text TEXT, tin INTEGER, tout INTEGER)")
    return _db


def _api():
    global _client
    if _client is None:
        import anthropic  # imported lazily so the simulated demo needs no API key
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def claude(span, model, system, user, prompt_ref=None, max_tokens=1024):
    """Call Claude, record tokens/cost/prompt on `span`, return the text."""
    model_id = MODEL_IDS.get(model, model)
    key = hashlib.sha256(json.dumps([model_id, system, user, max_tokens]).encode()).hexdigest()
    hit = None
    if MODE in ("replay", "record"):
        hit = _cache().execute("SELECT text, tin, tout FROM calls WHERE key=?", (key,)).fetchone()
    if hit and MODE == "replay":
        text, tin, tout = hit
    else:
        # No temperature/top_p/top_k: current Claude models return a 400 if they are set.
        r = _api().messages.create(model=model_id, max_tokens=max_tokens, system=system,
                                   messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in r.content if b.type == "text")
        tin, tout = r.usage.input_tokens, r.usage.output_tokens
        if MODE in ("replay", "record"):
            _cache().execute("INSERT OR REPLACE INTO calls VALUES (?,?,?,?)", (key, text, tin, tout))
            _cache().commit()
    span.set(**{"llm.cache": "hit" if hit and MODE == "replay" else "miss", "llm.model_id": model_id})
    if hasattr(span, "llm"):  # dev-stack span
        span.llm(model if model in MODEL_IDS else "sonnet", prompt_ref, tin, tout)
    else:  # production-stack span
        span.llm_usage(model_id, tin, tout)
    return text
