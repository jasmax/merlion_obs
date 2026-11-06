# Connecting GitHub, OpenAI, Langfuse, GitHub Actions and Railway

```
 push ─▶ GitHub ─▶ GitHub Actions: evals + regression gate ──(main, CI passed)──▶ Railway "Wait for CI": dashboards
                          │                                                          │
                          └── OpenAI (sim by default; replay cache keeps diffs exact) └── merlion_clean (fixture or MySQL)
 traces ─▶ Langfuse Cloud (python -m merlion_obs.otlp out/traces.db --push)
```

Set things up in this order: GitHub first, because Actions and Railway connect to it. Then the OpenAI key and the data, then Langfuse, then Railway.

## 1. GitHub

```bash
cd merlion-obs
git add -A && git commit -m "Align with team architecture: OpenAI, GitHub Actions, Langfuse, merlion_clean"
git push -u origin main
```

`.gitignore` already keeps out generated output (`out/`, `out_dev/`), SQLite files (including the OpenAI response cache) and `.env`.

After the first workflow run on a pull request, protect `main`:

1. Go to **Settings → Branches → Add rule** for `main`.
2. Select *Require status checks to pass*.
3. Pick the `gate` check.

Merges are then blocked whenever the gate fails.

## 2. OpenAI API

1. Put the team key from the 31 Oct briefing in `.env` as `OPENAI_API_KEY=...`. `.env` is git-ignored.
2. Find out which models the key allows:

   ```bash
   curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"
   ```

   Then point the aliases at them: `OPENAI_MODEL_MINI` (default `gpt-4o-mini`) and `OPENAI_MODEL_NANO` (default `gpt-4.1-nano`).
3. Set `LLM_PRICES` to the real per-token pricing. Until then, every cost is labelled an estimate.
4. Choose a mode with `LLM_MODE`:

   | Mode | Behaviour | Use it for |
   |---|---|---|
   | `sim` (default) | Simulates the chat model. Costs nothing. | CI and the demo |
   | `replay` | Calls OpenAI with real function calling, and serves unchanged requests from `llm_cache.db` | Measuring the real model |
   | `live` | Always calls OpenAI | The deployed app |

   The key has a hard spending cap, so try one email in `replay` before running all 120.

## 3. The data: `merlion_clean`

By default everything runs on the synthetic fixture.

To use the real database, add the same settings the ingestion scripts use (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`; see the team repo's `docs/dev-setup.md`) and set `MERLION_SOURCE=mysql`. You can use `MYSQL_URL` instead of the `DB_*` settings. `DB_NAME` defaults to `merlion_clean`.

Then label the real emails:

```bash
MERLION_SOURCE=mysql python -m merlion_obs.dataset init   # writes golden/labels.json, one row per email
```

- For each email, fill in `system_version`, `order_id`, `category` and `resolution` (`amount` where there is one).
- The file holds IDs and verdicts only. Keep names, phones and addresses out of `notes`, because the file is committed.
- Label at least 20 emails, including the corporate-account emails (the refusal cases). The eval gate needs both.

## 4. GitHub Actions

`.github/workflows/ci.yml` runs on every push and pull request. Pushing it is all the setup it needs. Each run:

- runs the production evals on the golden set (`merlion_obs.evals`);
- evaluates main's code from a separate checkout, as the baseline;
- evaluates this commit and gates it against main: accuracy, order accuracy, loops, hidden conflicts, latency, cost, and cases newly broken;
- saves both dashboards as the run's **dashboards** artifact.

Optional settings, under **Settings → Secrets and variables → Actions**:
- variable `LLM_MODE=replay`
- secret `OPENAI_API_KEY`

Together they evaluate the real model in CI. The response cache is kept between runs, so only changed prompts cost tokens.

## 5. Langfuse Cloud

1. On **cloud.langfuse.com**, create a project and go to **Settings → API keys**.
2. Put the keys in `.env`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` (default `https://cloud.langfuse.com`; use the region URL your project shows).
3. Run `python run_demo.py`, then `python -m merlion_obs.otlp out/traces.db --push`.

Each trace opens with its steps, tool calls and model calls nested. Model, tokens and estimated cost are on each generation, tagged by release (`v0.1`, `v0.2`). Check the weekly estimated spend there against the team budget.

## 6. Railway

1. On **railway.com**, go to **New Project → Deploy from GitHub repo** and pick `merlion-obs`.
2. In the service's **Settings**, turn on **Wait for CI**. Railway then deploys a commit on `main` only after the GitHub Actions run passes, so no deploy token or deploy job is needed.
3. Under the service's **Variables**, set:

   | Variable | Required? | Notes |
   |---|---|---|
   | `OPS_DASH_USER`, `OPS_DASH_PASS` | Yes | Protect `/` |
   | `DEV_DASH_USER`, `DEV_DASH_PASS` | No | If unset, `/dev` reuses the ops login |
   | `ENABLE_DEV_DASHBOARD` | No (default `true`) | Set `false` to keep `/dev` off the public internet |
   | `MERLION_SOURCE=mysql` and `MYSQL_URL=${{MySQL.MYSQL_URL}}`, plus `DB_NAME=merlion_clean` | For real data | Uses the project's MySQL service |

   With real data, the dashboards show order numbers and driver codes, though never names, phones or addresses. Keep the logins set.
4. Go to **Settings → Networking → Generate domain** to get a URL. The service serves:
   - `/` the production dashboard
   - `/dev` the developer dashboard
   - `/health` the health check

   `railway.json` sets the start command, the health check and the restart policy. Railpack detects Python from `requirements.txt` and `.python-version`.
5. The dashboards build automatically:
   - `out/` and `out_dev/` are git-ignored, so a deploy ships without them.
   - `server.py` starts serving right away (so `/health` passes) and runs `run_demo.py`, `build_dashboard.py`, `simulate_dev.py` and `build_dev_dashboard.py` in the background.
   - On the fixture, `/` is ready after about 20 s and `/dev` after about 35 s. Until then they return *"still being built"*.
   - If a step fails, the page shows the error, and so do the deploy logs (`build failed: …`).

## Check it works

- **The gate blocks a bad change.** Open a PR that replaces `configs/main.json` with the contents of `configs/chat-nano.json`. The `gate` check should go red: `newly_broken 30`, `resolution_accuracy 0.74`. GitHub should block the merge.
- **A good change deploys.** Merge a passing change. Railway should deploy after CI turns green, and ask for a login on `/` and `/dev`.
