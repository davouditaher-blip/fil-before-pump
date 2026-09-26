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
Status: WALLET INTELLIGENCE CONNECTED TO THE DECISION GATE
Stage commit: see the commit titled "Connect long-term wallet intelligence to the decision gate".

What was connected
- `wallet_signal_profiles.json` (long-term signal-wallet history) and
  `wallet_performance_memory.json` (bounded paper calibration) were produced by
  the pipeline and shape-validated, but no decision path read them. Long-term
  wallet history is priority 3 of the declared signal priority, so a proven
  wallet record contributed nothing unless it also appeared in the current
  Radar/Cluster/Quality/GMGN snapshot.
- New read-only layer `wallet_intel_gate.py` normalizes profile records into the
  same wallet-row contract as the other layers, so they merge into the single
  deduplicated pool in `scanner.wallet_conviction_signals`.
- Long-term profile evidence now adds a bounded contribution (cap 12) to the
  existing 0-30 wallet conviction bucket. The 100-point confluence scale and the
  PAPER_READY threshold were deliberately left unchanged.
- The fresh-volume guard can no longer delete a candidate backed by a proven
  long-term wallet record, mirroring the existing proven-GMGN override.
- Readiness plans, the risk gate and the Telegram report now expose long-term
  profile score, long-term proven wallet count, calibration bonus/status and the
  pre-calibration conviction for audit.
- `risk_engine` blocks a plan whose measurable paper calibration is maximally
  adverse. This is a rejection reason, never a relaxation.

Validation
- `smoke_test.py`: 18 deterministic tests, including proof thresholds, row
  identity, component caps, cross-layer deduplication, calibration sample rules,
  the adverse-calibration rejection and a no-live-execution assertion.
- `e2e_validate.py` and `final_integration_gate.py` validate the new plan fields
  and reject out-of-bounds profile scores, unmeasured calibration and missing
  fields. Both were confirmed to fail on injected violations.
- Verified offline against the committed provider artifacts: LINK resolves 23
  long-term proven wallets, LIT 2, while QNT/ENA honestly resolve none.

Blockers / open items
- `PAPER_READY` is effectively unreachable in production: the best observed
  `fil_confluence_score` is ~56 while the state requires >= 70, so 0 of 113
  candidates become paper-ready. This is a scoring-weight decision, not a bug,
  and it was left untouched. See the next task.
- `wallet_signal_profiles.py` still hardcodes
  `paper_feedback_calibration: 0.0` in every profile, so the profile artifact
  never reflects closed paper outcomes.
- `test_historical_replay.py` overwrites the committed `historical_replay.json`
  with its fixture. CI regenerates it on the next step, but a local run destroys
  the dataset until it is restored with `git checkout`.
- The repo has no `.gitignore`, so `__pycache__/` shows up as untracked after
  any local test run.

Recommended next task
Decide and implement the `fil_confluence_score` weighting so wallet-first
candidates can actually reach `PAPER_READY`, then measure forward-only with
`historical_replay.py`. Preserve the 0-30 wallet cap, keep technical indicators
non-rejecting, and do not enable live execution.

## Communication
Every completed stage must leave a concise repository-based handoff containing: status, commit SHA, changed files, tests/results, blockers, and next task.
