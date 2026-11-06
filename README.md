# Merlion triage agent: minimum viable AI observability stacks

To connect GitHub, Claude, CircleCI and Railway, see **SETUP.md**.

Two stacks over the same synthetic Merlion world:

- **`merlion_obs/`: production / ops.** Is the live agent making good decisions for customers? For Priya and the CS team.
- **`merlion_devobs/`: dev loop.** Did this commit break anything, which agent caused it, which prompt changed, what did it cost? For the engineers building the agent network. See the second half of this file.

---

# Stack 1: production observability

Pure Python 3.10+, standard library only. No services to run.

```
python run_demo.py          # synthetic data -> evals -> 4 weeks of traced prod traffic -> checks -> alerts
python build_dashboard.py   # out/dashboard.html
python -m merlion_obs.evals --version v0.2 --workdir out   # CI gate: exit 1 if any gate fails
```

## The five layers

| Layer | File | What it does |
|---|---|---|
| Tracing | `merlion_obs/tracer.py` | One trace per complaint, one span per model/tool call. OTel-shaped, stored in SQLite. Tokens and cost on every model span. PII (emails, SG phones, NRIC/FIN) redacted **before** storage. |
| Guardrails | `merlion_obs/checks.py` | 9 checks on every production trace, using only spans + ops DB (no labels): `order_disambiguation`, `timezone_alignment`, `postal_code_valid` (the three landmines in the brief), plus `evidence_grounding`, `amount_within_policy`, `pii_leak`, `tool_errors`, `latency_budget`, `cost_budget`. |
| Evals | `merlion_obs/evals.py` | Golden set (90 labelled emails) -> order accuracy, resolution accuracy, wrongful-reject rate, critical-check rate, p95 latency. Gates block release. |
| Metrics + alerts | `merlion_obs/metrics.py`, `alerts.py` | Daily rollups, per-version comparison, per-span latency, Priya's weekly driver/zone view. 8 alert rules (page vs ticket). |
| Export | `merlion_obs/otlp.py` | `out/otlp_traces.json` in OTLP/JSON with OpenInference span kinds, for Phoenix, Langfuse, Jaeger or Tempo. |

Online quality signal without labels: the `feedback` table records the CS reviewer's final decision; **override rate** is the metric.

## The demo scenario (synthetic data)

- `data.py` builds v1 jobs (Mar 2025 to Feb 2026) and v2 orders (Feb 2026 on), both numbered from 100001; GPS pings in UTC; delivery times as naive SGT (v1 as dd/mm/yyyy); ~7% bad postal codes.
- 5 to 18 Oct: **v0.1** in production (takes the v2 order on a collision, reads naive times as UTC, trusts postal codes).
- 19 Oct: **v0.2** deployed (disambiguates by customer email + date, reads times as SGT, falls back to the street address for bad postal codes).
- 28 to 29 Oct: rate-card OCR degraded (6x latency, 30% timeouts).

## To go live

1. Replace `data.build()` with a loader for `sql/merlion_ops.sql` + `logs/gps_pings_sample.jsonl`.
2. Keep the `with tracer.span(...)` blocks in `agent.py`; swap simulated bodies for real calls (token counts from the API response). Set `PRICE_IN/PRICE_OUT` in `tracer.py` to your model's pricing.
3. Hand-label Priya's 90 sample emails into `golden_set.json`; run the eval CLI in CI.
4. Run `run_checks()` per trace; post `alerts.evaluate()` to the CS channel.

---

# Stack 2: dev-loop observability (`merlion_devobs/`)

```
python simulate_dev.py          # replays 7 commits (r01–r07) over a 120-case dataset -> out_dev/
python build_dev_dashboard.py   # out_dev/dev_dashboard.html
python -m merlion_devobs.cli runs | diff r05 r06 | clusters r06 | show r06 E0067 | promote out/traces.db
python -m merlion_devobs.cli run --config configs/main.json -m "msg" --baseline main   # CI: exit 1 if blocked
```

The agent is modelled as a network: `router → order_resolver → evidence → policy → writer`, with explicit handoffs and a max-hops loop guard.

| Piece | File | What it gives developers |
|---|---|---|
| Prompt registry | `prompts.py` (+ `prompts/*.md`) | Every prompt versioned and content-hashed; every model span records `agent@version#hash`. |
| Run registry + tracer | `tracer.py` | Every span carries run, case, agent, prompt ref, model, tokens, cost and an output digest. Handoffs are spans too. PII is redacted. |
| Component evals | `runner.py` | Each agent scored on its own job (router: category; resolver: order; evidence: facts vs an oracle; policy: followed the rules on its inputs). Failures are blamed on the first agent that went wrong. |
| Regression diff | `analysis.py` | Case by case: fixed / broken / still failing, plus the **first agent whose output or control flow diverged**, with prompt and model changes listed. |
| Failure clusters | `analysis.py` | Failures grouped by signature (`evidence: wrong gps_at_address`, `network: handoff loop`), so the team fixes the biggest cluster first. |
| Gates | `runner.py` `GATES` | Accuracy ≥ 90%, order accuracy ≥ 95%, zero loops, p95 ≤ 12 s, ≤ $0.025/case, ≤ 1 case newly broken vs main. |
| Prod → dataset | `analysis.promote_from_prod` | Pulls cases the CS team overrode from the production stack into `candidates.jsonl` for labelling. |

Model outputs are simulated but deterministic per (case, agent, prompt hash, model), like temperature-0 calls. That makes diffs exact: an agent whose prompt and model didn't change gives the same output.

## The simulated sprint

| Run | Branch | Change | Result |
|---|---|---|---|
| r01 | main | initial network | 72.5%, blocked |
| r02 | main | resolver checks v1 + v2 | 80.0%, legacy-refund cluster gone |
| r03 | main | evidence reads times as SGT | 89.2%, GPS cluster gone |
| r04 | feat/policy-haiku | policy on a cheaper model | −28% cost but 4 cases broke (damage cap dropped), **blocked** |
| r05 | main | postal-code address fallback | 90.8%, **passes** |
| r06 | feat/router-verify | router double-checks with resolver | 10 handoff loops, 12 cases broke, **blocked** |
| r07 | main | router few-shot examples | 94.2%, **passes** |

Cases where every agent passed but the answer is still wrong are flagged `dataset:`. In this data that is a real quirk (two orders share a postal code, so geocoding lands at the wrong address), which is exactly what a developer should send back for relabelling or data cleanup.
