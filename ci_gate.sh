#!/usr/bin/env bash
# CI gate for a branch: evaluate main and this branch on the same dataset, diff, block on regression.
# Used by .circleci/config.yml. Runs locally too: ./ci_gate.sh
set -euo pipefail

BRANCH="${CIRCLE_BRANCH:-$(git branch --show-current)}"
SHA="${CIRCLE_SHA1:-$(git rev-parse HEAD)}"
export DEVOBS_DB="$PWD/out_dev/ci.db" DEVOBS_OPS="$PWD/out_dev/merlion_ops.db" DEVOBS_DATA="$PWD/out_dev/dataset.json"
mkdir -p out out_dev && rm -f "$DEVOBS_DB"

# 1. Production stack: offline evals must pass.
python run_demo.py > out/run_demo.log
python -m merlion_obs.evals --version v0.2 --workdir out

# 2. Dev stack: build the dataset and ops DB (plus the demo history for the dashboard).
python simulate_dev.py > out_dev/simulate.log

# 3. Baseline = main's code, evaluated now. Uses a separate checkout so main's prompts are used.
git fetch -q origin main
rm -rf /tmp/main && git worktree add -q --force /tmp/main origin/main
if [ -f /tmp/main/merlion_devobs/cli.py ]; then
  (cd /tmp/main && python -m merlion_devobs.cli run --config configs/main.json -m "baseline: main" --branch main) || true
fi

# 4. Candidate = this commit. Exit code 1 if any gate fails (accuracy, loops, cost, newly broken vs main).
python -m merlion_devobs.cli run --config configs/main.json -m "$BRANCH @ ${SHA:0:7}" --branch "$BRANCH" --baseline main
