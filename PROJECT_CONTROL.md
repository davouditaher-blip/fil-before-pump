# Fil Before Pump — Autonomous Project Control

## Authority
ChatGPT is the project architect/manager. The coding agent (AndCode/OpenCode) is the execution engineer. GitHub main is the shared source of truth.

## Operating loop
1. Read AGENTS.md and this file before each new task.
2. Inspect current main and existing implementation before changing anything.
3. Implement the next coherent stage of the Wallet Intelligence + Pre-Pump engine.
4. Run compile, smoke, integration, and relevant tests.
5. Fix failures caused by the implementation.
6. Preserve historical JSON/data and scheduled workflows.
7. Never expose secrets.
8. Never enable live trading.
9. Commit only coherent, validated changes.
10. After each completed stage, update the "Current task state" section in this file with: status, commit SHA, tests, blockers, and next recommended task.

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
Status: SYNC/BASELINE HANDOFF
Instruction: First establish a clean, up-to-date local clone of origin/main. Then inspect the current implementation and identify the highest-priority incomplete integration stage. Do not make speculative architectural changes.

After sync, continue autonomously through implementation, validation, and coherent commits until the current integration stage is complete. If blocked by network/provider limitations, document the exact blocker and continue with non-blocked validation work.

## Communication
Do not require the project owner to rediscover repository state manually. Repository files, commit history, tests, and this control file are the shared handoff mechanism.
