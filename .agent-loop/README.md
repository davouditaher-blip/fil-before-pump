# ChatGPT ↔ Acode Agent Loop

This directory is a repository-based handoff channel between ChatGPT and the local coding agent running inside Acode/Alpine.

## Protocol

1. ChatGPT creates or updates `TASK.md` with `STATUS=PENDING`.
2. The local runner pulls `main` and detects the pending task.
3. The runner invokes the available local agent:
   - `agy` (preferred)
   - `gemini` (fallback)
4. The agent reads the repository and performs only the requested task.
5. The runner captures the agent report into `REPORT.md`.
6. The runner marks the task DONE/FAILED and commits/pushes the result.
7. ChatGPT reads the report from GitHub and issues the next task.

## Safety

- The runner refuses to start with a dirty working tree.
- The agent is instructed not to expose secrets.
- Live trading/order execution is forbidden.
- Historical datasets must not be deleted.
- The runner itself performs the commit/push; the agent is not asked to commit.
- A task is executed only when TASK.md says STATUS=PENDING.

This is an orchestration layer only. It does not modify the project's trading/scoring architecture by itself.
