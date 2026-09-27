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
Status: CONFLUENCE SCALE MADE HONEST — PAPER_READY IS NOW REACHABLE
Stage commit: see the commit titled "Make the confluence scale honest so paper-ready is reachable".

What was wrong
- The engine advertised a 100-point `fil_confluence_score` and a `PAPER_READY`
  threshold of 70, but the scale was really an 88-point scale. Four of the five
  buckets could never reach their declared cap because the criteria that would
  award the missing points were absent from the scoring code:
  project max 17/20, volume 18/20, market 8/10, safety 15/20.
- Requiring 70 on an 88-point scale meant 79.5% of an unattainable maximum. The
  best observed production score was ~55.6, so 0 of 113 candidates ever became
  paper-ready. The threshold was never the bug; the scale was.

What changed
- Each bucket can now reach its declared cap using evidence the layers already
  collect, instead of a magic constant:
  - project +3 for broad buyer participation (`buyers_7d >= 25`)
  - volume +2 for fresh acceleration (1d growth at least 2d growth), which was
    already computed as evidence but never scored
  - market +2 for breadth (BTC and ETH both positive on 24h), not just a
    positive average that can hide one strongly negative major
  - safety +3 market cap and +2 futures volume above explicit execution floors
    ($500M / $10M), which is operational executability, not a price forecast
- `PAPER_READY_MIN_CONFLUENCE` and the bucket caps are now declared in
  `confluence_engine.py` and imported by `trade_readiness.py`, so the gate and
  the advertised scale cannot drift apart. The threshold stays at 70/100 and the
  wallet bucket keeps its 0-30 cap.
- Every candidate now reports `fil_confluence_scale` with bucket caps, fill pct,
  `evidence_coverage` and `project_layer_present`, so a weak candidate can be
  told apart from one whose provider layer simply returned nothing.
- Technical indicators remain context-only and never reject, and no live
  execution was introduced.

Forward-only measurement
- `historical_replay.py` now reports a forward-only wallet-history cohort split.
  The cohort is fixed per entry from rows strictly earlier than the entry
  timestamp, so it cannot leak the outcome it measures. On 1488 observations:
  first-entry wallets hit +10% at 3.94% versus 0.10% for repeat entries in 6h,
  with 3x the MFE (3.10% vs 1.03%) and shallower MAE; by 24h the hit-rate edge
  has largely decayed (7.99% vs 7.40%). This supports the engine's existing
  `pre_pump_first_entry_rate` scoring and the short-horizon bias.
- A symbol-level "has a long-term profile" split was rejected and documented: the
  replay population is generated by the same wallets that build
  `wallet_signal_profiles.json`, so 94% of observations fall in that cohort and
  the split carries almost no information.

Validation
- `smoke_test.py`: 24 deterministic tests. `_caps_reachable()` is asserted, so a
  future edit cannot quietly reintroduce a dead cap. Verified by mutation:
  removing the market-breadth criterion makes the suite fail.
- Both artifact validators reject an unreachable scale, a component over its
  cap, a PAPER_READY plan below the threshold, and a PAPER_READY plan carrying
  blockers. Each was confirmed to fail on an injected violation.
- `test_historical_replay.py` no longer overwrites the committed dataset; it
  replays into a temporary directory.

Blockers / open items
- **Unmeasured**: how many real production candidates now clear 70 is not yet
  known, because it depends on how much project-layer evidence the providers
  return for the top-300 futures universe. The final gate now prints project
  layer coverage and confluence fill pct, so the next scheduled run settles it.
  Do not assume the synthetic harness result (21/21 paper-ready on maximal
  fixture data) reflects production.
- `wallet_signal_profiles.py` still hardcodes `paper_feedback_calibration: 0.0`
  in every profile, so the profile artifact never reflects closed paper outcomes.
- The repo has no `.gitignore`, so `__pycache__/` shows up as untracked after
  any local test run. Untracked only; never committed.

Recommended next task
Read the project layer coverage and fill pct from the next CI run of
`final_integration_gate.py` to find out whether the reweighting actually opened
the gate in production. If the project layer is the binding constraint, extend
its coverage rather than lowering the threshold again. Then feed
`paper_feedback_calibration` from closed paper trades so the bounded calibration
memory can move off `NO_HISTORY`.

## Communication
Every completed stage must leave a concise repository-based handoff containing: status, commit SHA, changed files, tests/results, blockers, and next task.
