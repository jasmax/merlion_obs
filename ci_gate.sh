#!/usr/bin/env bash
# CI gate for a branch: evaluate main and this branch on the same dataset, diff, block on regression.
# Used by .github/workflows/ci.yml. Runs locally too: ./ci_gate.sh
set -euo pipefail

BRANCH="${GITHUB_HEAD_REF:-${GITHUB_REF_NAME:-$(git branch --show-current)}}"
SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
export DEVOBS_DB="$PWD/out_dev/ci.db" DEVOBS_OPS="$PWD/out_dev/merlion_clean.db" DEVOBS_DATA="$PWD/out_dev/dataset.json"
mkdir -p out out_dev && rm -f "$DEVOBS_DB"

# 1. Production stack: offline evals on the golden set must pass (harness checklist #8).
python run_demo.py > out/run_demo.log
python -m merlion_obs.evals --version v0.2 --workdir out

# 2. Dev stack: build the dataset and merlion_clean fixture (plus the demo history for the dashboard).
python simulate_dev.py > out_dev/simulate.log

# 3. Baseline = main's code, evaluated now. A separate checkout, so main's prompts and tools are used.
MAIN_DIR="${RUNNER_TEMP:-/tmp}/merlion-main"
git fetch -q origin main
rm -rf "$MAIN_DIR" && git worktree add -q --force "$MAIN_DIR" origin/main
if [ -f "$MAIN_DIR/merlion_devobs/cli.py" ]; then
  (cd "$MAIN_DIR" && python -m merlion_devobs.cli run --config configs/main.json -m "baseline: main" --branch main) || true
fi

# 4. Candidate = this commit. Exit code 1 if any gate fails (accuracy, order, loops, hidden conflicts,
#    latency, cost, cases newly broken vs main).
python -m merlion_devobs.cli run --config configs/main.json -m "$BRANCH @ ${SHA:0:7}" --branch "$BRANCH" --baseline main
