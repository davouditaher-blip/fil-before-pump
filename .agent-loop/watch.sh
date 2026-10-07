#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

INTERVAL="${AGENT_LOOP_INTERVAL:-20}"

while true; do
  git fetch origin main --quiet || true
  git merge --ff-only origin/main --quiet || true

  if grep -q '^STATUS=PENDING$' .agent-loop/TASK.md 2>/dev/null; then
    .agent-loop/run-once.sh || true
  fi

  sleep "$INTERVAL"
done
