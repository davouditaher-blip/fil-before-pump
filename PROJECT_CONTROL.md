# Fil Before Pump — Autonomous Project Control

## Authority
ChatGPT is the project architect/manager. AndCode/OpenCode is the execution engineer. GitHub main is the shared source of truth.

## Autonomous operating loop
The execution engineer must continue through the next coherent implementation/validation stages without requiring the project owner to manually issue a "next step" after every completed stage.

Before each stage:
1. Read AGENTS.md and this file.
2. Inspect current main, recent commits, relevant modules, tests, workflows, and data artifacts.
3. Identify the highest-priority incomplete stage from the architecture below.
4. Do not make speculative or destructive architectural changes.

For each stage:
1. Implement the smallest coherent change that advances the architecture.
2. Run compile/syntax, smoke, integration, replay/historical, and other relevant tests.
3. Fix failures caused by the change.
4. Preserve historical JSON/data and scheduled workflows.
5. Never expose, print, commit, or request secret/API-key values.
6. Never enable live trading or live order execution.
7. Prefer read-only wallet intelligence and paper/replay validation.
8. Commit only coherent, validated changes with a clear message.
9. Update this file's Current task state with status, commit SHA, tests, blockers, and next task.
10. If the next task is safe, clearly defined, and within this architecture, continue to it autonomously rather than waiting for another user message.
11. Stop and report only when blocked by missing credentials/access, an ambiguous product decision, a destructive/risky operation, or an explicit human approval requirement.

The project owner should not need to manually rediscover repository state or relay routine "continue" instructions. Repository files, commit history, tests, and this control file are the shared handoff mechanism.

## Architecture priority
Smart Money -> shared/common wallets -> wallet history -> exit/distribution -> whale -> volume.

Primary integration target:
- Connect wallet intelligence layers into the final decision/scoring gate.
- Do not let technical indicators reject a strong wallet candidate.
- Preserve Binance/Bybit/Gate futures universe and top-300 rules.
- Preserve GMGN/Solscan/GoldRush, wallet clustering, wallet quality/history, radar, paper feedback and performance memory.
- Preserve Telegram Persian-friendly reporting and 30-minute shared-wallet intelligence.
- No live order execution.

## Current task state
Status: READY FOR AUTONOMOUS IMPLEMENTATION
Current stage: Inspect the final decision/scoring gate and trace all wallet-intelligence outputs into it.
Required sequence:
1. Audit decision/scoring flow and identify which wallet signals are currently consumed, partially consumed, or disconnected.
2. Map each relevant Wallet Intelligence output to the scoring/gating path without allowing technical indicators to reject a strong wallet candidate.
3. Implement the smallest coherent integration.
4. Run relevant tests and fix implementation-caused failures.
5. Commit and update this state.
6. Continue to the next safe validation/integration stage automatically.

Current commit/state should always be refreshed by the execution engineer after each completed stage.

## Communication
Every completed stage must leave a concise repository-based handoff containing: status, commit SHA, changed files, tests/results, blockers, and next task.
