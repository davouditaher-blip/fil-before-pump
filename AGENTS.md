# Fil Before Pump — OpenCode Autonomous Engineering Protocol

## Role
OpenCode is the execution engineer for this repository. The project owner/architect is ChatGPT. Work from the repository state and the architecture defined below.

## Autonomous workflow
When this repository is opened in OpenCode, read this file first.

For each assigned project task:
1. Inspect the existing implementation before editing.
2. Preserve working behavior and historical data.
3. Make the smallest coherent set of changes required.
4. Run relevant validation/tests after changes.
5. Fix failures caused by your changes before stopping.
6. Never expose, print, commit, or request secret/API-key values.
7. Do not delete historical datasets or workflow artifacts unless explicitly instructed.
8. Do not enable live trading or trade execution.
9. Prefer read-only wallet intelligence and paper/replay validation.
10. Commit completed coherent changes with a clear message when repository write access is available.
11. End every task with a concise report: changed files, tests run, results, remaining blockers, and recommended next task.

## Project architecture
The target is a Wallet Intelligence + Pre-Pump Auto-Trading Engine, not a simple technical-analysis bot or copy trader.

Signal priority:
Smart Money -> shared/common wallets -> wallet history -> exit/distribution status -> whale activity -> volume.

Technical indicators must not reject a strong wallet candidate and should not be part of the primary Telegram full output.

Universe:
- Binance perpetual/futures
- Bybit perpetual/futures
- Gate perpetual/futures as fallback
- Top 300 assets
- Exclude stablecoins, tokenized stocks, and gold-backed tokens

Wallet intelligence:
- GMGN
- Solscan
- GoldRush
- Shared-wallet clustering
- Wallet quality/history
- Pre-pump historical evidence where available
- Holding / partial selling / exit-distribution / unknown states
- Historical replay and forward-only validation

Volume:
- Use 24h, previous 1 day, and previous 2 days for the visible report.
- Longer history may remain internal for intelligence and validation.
- 3-day data is context, not a hard exclusion.

Telegram:
- Persian-friendly output where practical.
- Show BTC/ETH regime context.
- Show high-quality early candidates without requiring a prior price pump.
- Include the 30-minute repeated/shared-wallet intelligence report.

Safety:
- No live order execution unless explicitly authorized in a future task.
- Never weaken safety gates merely to make a test pass.
- Never fabricate wallet activity, historical performance, or provider data.

## Completion behavior
Do not stop at superficial formatting changes. When a task says to continue building the project, trace dependencies, implement the next coherent stage, validate it, and report the exact remaining work.

If requirements conflict with existing code, preserve the architecture above and report the conflict before making a destructive change.

## Current handoff
At the beginning of a new OpenCode session, inspect the repository and determine:
- what is already implemented,
- what is partially implemented,
- what is broken,
- and the next highest-priority implementation step.

Do not ask the user to manually rediscover repository state when it can be determined from the codebase.
