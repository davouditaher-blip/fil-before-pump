ID=smoke-001
STATUS=PENDING

# Purpose
Prove the complete repository handoff loop without changing the Fil Before Pump implementation.

# Task
Read AGENTS.md and PROJECT_CONTROL.md.

This is an orchestration smoke test only.

1. Inspect the repository state.
2. Confirm that this task was received from .agent-loop/TASK.md.
3. Do NOT modify any project source code.
4. Do NOT modify historical data, workflows, scoring, wallet logic, or trading logic.
5. Run only safe read-only checks needed to prove that the agent can access the repository (for example git status and a lightweight file/read check).
6. Return a concise report containing:
   - TASK_RECEIVED=YES
   - repository branch
   - current HEAD
   - changed_files=NONE
   - tests/checks performed
   - blockers
   - recommended next task
