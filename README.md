# Merlion Express triage app: evals, guardrails and observability

This is the harness around Team 2's complaint-triage app (team repo `team2_merlion_express`). It answers four questions about the app:

- Did it answer correctly?
- Which step broke?
- What did it cost?
- Can we ship this commit?

It mirrors the design in `docs/architecture.md`:

```
intake (code) -> resolveOrder (code, before any LLM call) -> chat: OpenAI + tools -> answer_check (code)
                                                              getDeliveryHistory · getPodScan · getGpsTrail · getRate · searchPolicy
```

It reads the same nine `merlion_clean` tables the Spring tools read. Traces go to **Langfuse Cloud**, CI runs in **GitHub Actions**, and the dashboards deploy to **Railway** after CI passes. To connect them, see **SETUP.md**.

There are two stacks over the same pipeline:

- **`merlion_obs/`: production.** Is the live app making good decisions for Priya's CS team?
- **`merlion_devobs/`: dev loop.** Did this commit break anything? Which step caused it, which prompt or rule changed, and what did it cost?

## Run it

```
pip install -r requirements.txt     # optional: the simulated stack needs only the standard library
python run_demo.py                  # merlion_clean -> evals -> 4 weeks of traced traffic -> checks -> alerts
python build_dashboard.py           # out/dashboard.html
python simulate_dev.py              # 7 commits gated against main -> out_dev/
python build_dev_dashboard.py       # out_dev/dev_dashboard.html
python -m merlion_obs.evals --version v0.2 --workdir out        # CI gate: exit 1 if any gate fails
```

### Settings

| Setting | Values | Effect |
|---|---|---|
| `MERLION_SOURCE` | `fixture` (default), `mysql` | `fixture` is a synthetic `merlion_clean` with the real schema. `mysql` is the real database: the same `DB_*` settings as the ingestion scripts, or Railway's `MYSQL_URL`. |
| `LLM_MODE` | `sim` (default), `replay`, `record`, `live` | `sim` simulates the chat model deterministically. The others call OpenAI with real function calling; `replay` serves unchanged requests from a cache. |
| `OPENAI_MODEL_MINI` / `OPENAI_MODEL_NANO` | model IDs | What the config aliases `mini` and `nano` mean. Defaults: `gpt-4o-mini`, `gpt-4.1-nano`. |
| `LLM_PRICES` | JSON, e.g. `{"gpt-4o-mini": [0.15, 0.60]}` | USD per 1M tokens. Costs are **estimates** until this matches the class provider's pricing. |

## Data: the audit's landmines

`merlion_obs/fixture.py` builds a fake `merlion_clean` that reproduces the team's data audit (`docs/data-quality-report.md`). CI and the public demo need it because client data never goes into Git. The tools, checks and evals run unchanged against the real database.

| Audit | What the data does | Where the harness catches it |
|---|---|---|
| T01, T26 | Order numbers 20001.. exist in v1 and v2; emails give only the number | `resolveOrder` scores phone, name and date, or asks the CS agent. Check: `order_disambiguation` |
| T16 | GPS `ts_epoch` is 8 h slow: Singapore time = epoch + 16 h | Check: `timezone_alignment` recomputes the GPS window and compares it with `delivered_time` |
| T04 | Postal codes are unrelated to the town; the zone comes from the town | Check: `zone_from_town` (rate and weekly report) |
| T23, O02–O04 | POD remark or COD disagrees with the database on ~94% of scans | Check: `conflict_disclosed`; gate: `hidden_conflicts` |
| T24 | No rate card before 1 Apr 2026 | Check: `honest_gaps` (the answer must say the data cannot answer) |
| T21 | No POD scan for v2 orders | `getPodScan` returns the gap "no POD scan on file" |
| T27 | Corporate accounts (`MERL-C-xxxx`) are in no table | Refused by code, no LLM call. Check: `refusal_without_llm`; eval: `refusal_accuracy` |
| T28 | Emails hold names, phones, addresses | `merlion_obs/pii.py` masks them before the LLM, in every span, and in the answer. Check: `pii_leak` |

The verdicts follow `docs/policies.md`, the team's **assumed** policy. Every verdict says "based on assumed policy".

---

# Stack 1: production (`merlion_obs/`)

| Layer | File | What it does |
|---|---|---|
| Pipeline | `agent.py`, `tools.py` | The triage flow from architecture.md. Tools are Python twins of the Spring `@Tool`s. Each returns data, its sources (`deliveries#73`, `POD_v1_20078.jpg`), its conflicts and its gaps. |
| Data | `db.py`, `fixture.py`, `dataset.py` | Read-only access to `merlion_clean` (SQLite fixture or MySQL), the complaint emails, and their labels. |
| Tracing | `tracer.py`, `otlp.py` | One trace per email; one span per step, model call and tool call. PII is masked before storage. Exported as OTLP with GenAI attributes and pushed to Langfuse. |
| Guardrails | `checks.py` | 13 checks on every production trace, using only spans and `merlion_clean` (no labels). |
| Evals | `evals.py` | Golden set → order accuracy, resolution accuracy, wrongful rejects, refusals, critical checks, p95 latency. The gates block the merge. |
| Metrics, alerts | `metrics.py`, `alerts.py` | Daily rollups, v0.1 vs v0.2, per-span latency, Priya's weekly driver and zone view, and 10 alert rules (page vs ticket). |

The demo timeline (fixture):

- 4–17 May: **v0.1** runs in production. On a collision it takes the v2 order, reads the GPS clock with +8 h, and takes the zone from the postal sector.
- 18 May: **v0.2** is deployed.
- 26–27 May: the OpenAI API degrades (6× latency, 30% timeouts), and the app escalates instead of guessing.

The online quality signal is the CS agent's final decision, stored in `feedback`; the override rate needs no labels.

# Stack 2: dev loop (`merlion_devobs/`)

```
python -m merlion_devobs.cli runs | diff r05 r06 | clusters r06 | show r06 2026-05-07_008 | promote out/traces.db
python -m merlion_devobs.cli run --config configs/main.json -m "msg" --baseline main   # CI: exit 1 if blocked
```

| Piece | File | What it gives developers |
|---|---|---|
| Registry | `merlion_obs/registry.py` (+ `prompts/*.md`) | Every step versioned and content-hashed: the chat system prompt, and the rule each code step implements. Every span records `step@version#hash`. |
| Component evals | `runner.py` | Each step scored on its own job, against the correct pipeline run on the true order. A failure is blamed on the first step that went wrong. |
| Regression diff | `analysis.py` | Case by case: fixed, broken or still failing, plus the **first step whose output or control flow diverged**, with prompt, rule and model changes listed. |
| Failure clusters | `analysis.py` | Failures grouped by signature (`tools: wrong evidence_of_delivery`, `chat: tool-call loop`), so the team fixes the biggest cluster first. |
| Gates | `runner.py` `GATES` | Accuracy ≥ 90%, order accuracy ≥ 95%, zero tool-call loops, zero hidden conflicts, p95 ≤ 12 s, ≤ $0.002 per case, ≤ 2 cases newly broken vs main (noise budget). |

The simulated sprint (fixture, 120 cases):

| Run | Branch | Change | Result |
|---|---|---|---|
| r01 | main | initial pipeline | 72.5%, blocked |
| r02 | main | resolveOrder scores phone, name and date (T26) | 78.3%; order accuracy 100% |
| r03 | main | GPS time = epoch + 16 h (T16) | 94.2%, blocked: chat@v1 hides POD conflicts |
| r04 | feat/chat-nano | chat on the cheaper model | −34% cost, but 35 cases broke (skipped GPS, dropped citations), **blocked** |
| r05 | main | zone from the town (T04) | 98.3%, still blocked: hidden conflicts |
| r06 | feat/recheck-conflicts | chat re-confirms the order on conflicts | 6 tool-call loops, **blocked** |
| r07 | main | prompt: show both sides, say when data is missing | 98.3%, **passes** |

## What is real and what is simulated

- **Real:** every tool query, the checks, the scoring, the masking, and the export.
- **Simulated in `LLM_MODE=sim`:**
  - the chat model's complaint type, tool choice and slips (deterministic per case, step version and model);
  - latency and token counts.
- **Not covered yet:** the Spring Boot app itself. Once it emits the same span names and attributes through Micrometer and OpenTelemetry, the checks can run on its Langfuse traces (step 3 of "Wiring it" on the production dashboard).
