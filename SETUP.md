# Connecting GitHub, Claude, CircleCI and Railway

```
 push ─▶ GitHub ─▶ CircleCI: evals + regression gate ──(main only, gate passed)──▶ Railway: dashboards
                        │                                                              │
                        └── Claude API (replay cache keeps diffs exact)                └── Claude API (live)
```

Order matters: GitHub first (the others connect to it), then Claude keys, CircleCI, Railway, and finally the Railway token back into CircleCI.

## 1. GitHub

```bash
cd merlion-obs
git init -b main && git add -A && git commit -m "Merlion observability stacks"
gh repo create merlion-obs --private --source . --push      # or create it on github.com and `git remote add origin … && git push -u origin main`
```

`.gitignore` already keeps out generated output (`out/`, `out_dev/`), SQLite files, the Claude response cache and `.env`.

After CircleCI's first run on a PR (step 3), protect `main`: **Settings → Branches → Add rule** for `main` → *Require status checks to pass* → pick the CircleCI `gate` check. Merges are then blocked when the gate fails.

## 2. Claude API

1. Create a key at **platform.claude.com → API keys**. Set a monthly spend limit in the console while you are testing.
2. Locally: put `ANTHROPIC_API_KEY=sk-ant-…` in `.env` (ignored by git) and `pip install -r requirements.txt`.
3. Swap simulated calls for real ones with `merlion_obs/llm.py`. Inside each existing span:

   ```python
   from merlion_obs.llm import claude
   with self.tracer.span("router.classify", "llm") as s:
       text = claude(s, "sonnet", system=PROMPTS["router"]["v2"], user=email["body"],
                     prompt_ref=ref("router", "v2"))
   ```

   Tokens, cost, model ID and prompt hash land on the span automatically, so both dashboards keep working.
4. Models: `claude-sonnet-5-5` ($2 / $10 per M tokens) and `claude-haiku-5-5` ($0.10 / $0.50). Don't pass `temperature`, `top_p` or `top_k`: these models return a 400 error if you do.
5. Because outputs aren't fixed by temperature, CI runs with `LLM_MODE=replay`: unchanged requests are served from a recorded cache, so only agents whose prompt, model or input changed make new calls. Production uses `LLM_MODE=live`.

## 3. CircleCI

1. Sign in at **circleci.com** with GitHub and authorise the GitHub app.
2. **Projects → Set up project** → pick `merlion-obs` → choose *use the existing config* on `main` (it reads `.circleci/config.yml`).
3. **Organization Settings → Contexts → Create context** named `merlion`, with these variables:
   - `ANTHROPIC_API_KEY` (from step 2; only used once real calls are wired in)
   - `RAILWAY_TOKEN` (from step 4, added after Railway exists)
4. Every push now runs `ci_gate.sh`:
   - production evals (`merlion_obs.evals`)
   - main's code evaluated from a separate checkout as the baseline
   - this commit evaluated and gated against it: accuracy, loops, cost, cases newly broken vs main
   - both dashboards saved under the job's **Artifacts** tab (`dashboards/production.html`, `dashboards/dev.html`)
5. On `main`, once `gate` passes, the `deploy` job ships to Railway.

## 4. Railway

1. **railway.com → New Project → Empty project**, then add an **Empty service** named `merlion-obs`. Leave it unconnected to GitHub: Railway's *Wait for CI* only waits for GitHub Actions, not CircleCI, so CircleCI does the deploying after the gate.
2. Service **Variables**:
   - `OPS_DASH_USER`, `OPS_DASH_PASS` (required; protects `/`)
   - `DEV_DASH_USER`, `DEV_DASH_PASS` (optional; if unset, `/dev` reuses ops creds)
   - `ENABLE_DEV_DASHBOARD=true` (set `false` if you do not want `/dev` exposed on the public internet)
   - `ANTHROPIC_API_KEY` and `LLM_MODE=live` (once the agent makes real calls)
3. **Settings → Networking → Generate domain** gives you a URL. The service serves:
   - `/` production dashboard
   - `/dev` developer dashboard
   - `/health` health check

   `railway.json` sets the start command, health check and restart policy. Railpack detects Python from `requirements.txt` and `.python-version`.
4. **Project Settings → Tokens → create a project token** for the production environment. Paste it into the CircleCI `merlion` context as `RAILWAY_TOKEN`.
5. Push to `main`. CircleCI runs the gate and then `railway up --ci --service merlion-obs`.
6. Build dashboards once before serving:

   ```bash
   python run_demo.py
   python build_dashboard.py
   python simulate_dev.py
   python build_dev_dashboard.py
   ```

7. When real traces start arriving, attach a **volume** mounted at `/app/out` so the SQLite trace store survives redeploys (Railway sets `RAILWAY_VOLUME_MOUNT_PATH` for you).

## Check it works

- Open a PR that changes `configs/main.json` to `configs/policy-haiku.json`'s contents. CircleCI should go red with `newly_broken 11` and GitHub should block the merge.
- Merge a passing change. Railway should start successfully and require login for `/` and `/dev` (if enabled).
