#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

TASK=".agent-loop/TASK.md"
REPORT=".agent-loop/REPORT.md"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [[ ! -f "$TASK" ]]; then
  echo "NO_TASK: $TASK not found"
  exit 0
fi

STATUS="$(awk -F= '/^STATUS=/{print $2; exit}' "$TASK" || true)"
if [[ "$STATUS" != "PENDING" ]]; then
  echo "NO_PENDING_TASK: STATUS=$STATUS"
  exit 0
fi

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "main" ]]; then
  echo "BLOCKED: runner must operate on main; current branch=$BRANCH"
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "BLOCKED: working tree is not clean"
  git status --short
  exit 2
fi

git fetch origin main --quiet
git merge --ff-only origin/main --quiet

if [[ -n "$(git status --porcelain)" ]]; then
  echo "BLOCKED: repository changed during sync"
  exit 2
fi

TASK_TEXT="$(cat "$TASK")"
PROMPT="$(cat <<EOF
You are the execution engineer for the Fil Before Pump repository.

Read AGENTS.md and PROJECT_CONTROL.md before acting.
The task below is authoritative for this run.

STRICT RULES:
- Do not expose, print, commit, or request secrets/API keys.
- Never enable live trading or live order execution.
- Never delete historical datasets or workflow artifacts.
- Inspect before editing.
- Make the smallest coherent change required by the task.
- Run the relevant validation/tests.
- Do NOT create a git commit and do NOT push. The wrapper does that.
- Do NOT modify .agent-loop/TASK.md or .agent-loop/REPORT.md.
- End your response with: changed files, tests/results, blockers, and recommended next task.

TASK:
$TASK_TEXT
EOF
)"

if command -v agy >/dev/null 2>&1; then
  AGENT="agy"
  if ! agy -p "$PROMPT" --output-format json --dangerously-skip-permissions >"$TMP"; then
    RC=$?
    printf 'STATUS=FAILED\n\nAgent: agy\nExit code: %s\n\n%s\n' "$RC" "$(cat "$TMP")" > "$REPORT"
    sed -i 's/^STATUS=PENDING$/STATUS=FAILED/' "$TASK"
    git add .agent-loop/TASK.md .agent-loop/REPORT.md
    git commit -m "agent-loop: record failed task smoke run" >/dev/null
    git push origin main
    exit "$RC"
  fi
elif command -v gemini >/dev/null 2>&1; then
  AGENT="gemini"
  if ! gemini -p "$PROMPT" --output-format json --approval-mode=yolo >"$TMP"; then
    RC=$?
    printf 'STATUS=FAILED\n\nAgent: gemini\nExit code: %s\n\n%s\n' "$RC" "$(cat "$TMP")" > "$REPORT"
    sed -i 's/^STATUS=PENDING$/STATUS=FAILED/' "$TASK"
    git add .agent-loop/TASK.md .agent-loop/REPORT.md
    git commit -m "agent-loop: record failed task smoke run" >/dev/null
    git push origin main
    exit "$RC"
  fi
else
  echo "BLOCKED: neither agy nor gemini is available in this Acode environment"
  exit 3
fi

python3 - "$TMP" "$REPORT" "$AGENT" <<'PY'
import json, sys
src, dst, agent = sys.argv[1:]
raw = open(src, encoding="utf-8").read()
try:
    obj = json.loads(raw)
    response = obj.get("response", "")
    status = obj.get("status", "UNKNOWN")
    error = obj.get("error")
    usage = obj.get("usage", obj.get("stats", {}))
    out = [
        "STATUS=SUCCESS" if status == "SUCCESS" else f"STATUS={status}",
        "",
        f"Agent: {agent}",
        "",
        "## Agent response",
        response or "(empty response)",
    ]
    if error:
        out += ["", "## Agent error", str(error)]
    if usage:
        out += ["", "## Usage", json.dumps(usage, ensure_ascii=False, indent=2)]
except Exception as exc:
    out = ["STATUS=SUCCESS", "", f"Agent: {agent}", "", "## Raw agent output", raw, "", f"JSON parse note: {exc}"]
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY

if grep -q '^STATUS=SUCCESS$' "$REPORT"; then
  sed -i 's/^STATUS=PENDING$/STATUS=DONE/' "$TASK"
else
  sed -i 's/^STATUS=PENDING$/STATUS=FAILED/' "$TASK"
fi

git add -A
git commit -m "agent-loop: complete $(awk -F= '/^ID=/{print $2; exit}' "$TASK")" >/dev/null
git push origin main

echo "AGENT_LOOP_COMPLETE"
git log -1 --oneline
