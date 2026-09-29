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

## Current task state
Status: HISTORICAL WALLET VALIDATION REBUILT — "PROVEN WALLETS = 0" WAS A PROVIDER-AVAILABILITY FAULT

What was wrong
- `gmgn_layer.analyze_wallet_activity` is the only function that sets `proven`
  for the wallet-track gate, and it could only reconstruct history from a
  *live* `gmgn portfolio activity` call plus a *live* `gmgn market kline` call.
  When the provider was slow, rate limited or unkeyed, both returned nothing,
  the profile collapsed to `opportunities=0`, and every wallet was reported
  unproven: "Proven wallets (current historical test): 0",
  "historical proof not yet established", "سابقه کافی نیست".
- That is a *provider availability* fault being printed as a *wallet quality*
  fact. The repository already stored 162,081 GMGN records across 2,129
  wallets (1,908 with buys) and that stored history was never replayed through
  the proven-wallet decision.
- `wallet_quality_engine._forward_hit_rate` and `_pre_pump_proof` joined
  forward observations on the ticker symbol. 1,975 of 7,396 stored symbols map
  to more than one contract and one ("SI") maps to 63, so entries were credited
  or blamed for another token's price move. 775 of 962 qualified entries had a
  colliding symbol.
- The live path's own rule was `proven = bool(successes)`, i.e. a single 2x
  observation, which disagreed with the offline rule and made "proven" mean
  "saw one good trade".

What changed
- New `wallet_history_validation.py` reconstructs every stored buy offline:
  canonical chain+contract identity, entry time/price, peak multiple, MFE/MAE,
  target reached, exit timestamp or current holding, and observation-window
  completeness. It never joins on symbol alone.
- Three explicit states replace the binary: `PROVEN`,
  `HISTORICAL_ACTIVITY_BUT_UNPROVEN`, `NO_HISTORY`. A window with no forward
  observation stays UNKNOWN and is excluded from the win rate, so it can never
  be recorded as 0% performance or as a loss.
- `analyze_wallet_activity` now falls back to stored rows when live activity is
  unusable, and both paths share one `whv.classify` threshold.
- The PROVEN threshold is explicit and published on every profile and in the
  summary artifact: >= 3 observed entries AND >= 2 genuine 2x pre-pump hits AND
  win rate >= 60% over observed entries. `MIN_SUCCESSFUL_ENTRIES` is currently
  implied by the other two bounds; it is kept explicit so loosening either
  cannot silently weaken the proof standard.
- Deduplication keys on full event identity and sorts with the original index as
  a stable tie-breaker, so repeat buys survive and chronological order is kept.
- The Telegram report now prints the three states, the reconstructed-entry
  counters and the PROVEN rule, instead of one "sابقه کافی نیست" line.

Validation on the committed 162,081-record dataset
- 2,129 wallets evaluated; 1,616 with sufficient evidence.
- 4 PROVEN, 1,833 HISTORICAL_ACTIVITY_BUT_UNPROVEN, 292 NO_HISTORY/COLD_START.
- 31,382 reconstructed entries; 20,948 with a valid peak/MFE; 18,298 with a
  valid exit; 2,650 still holding with no exit; 10,434 UNKNOWN windows.
- The 292 cold starts are explained: 221 sell-only, 40 buys with no price,
  31 below the entry floor. The 10,434 UNKNOWN windows are all entries made
  inside the last 14 days whose forward window has not elapsed yet.

Tests
- New `test_wallet_history_validation.py`: 27 deterministic fixture tests
  covering a successful pre-pump wallet, a losing/late wallet, cold start,
  missing price, missing exit, a partial exit, multiple buys of one asset, the
  same symbol on different contracts and on different chains, duplicate
  records, deduplicated-but-ordered rows, a current holding with no exit, and a
  wallet with activity but insufficient evidence.
- Verified by mutation: joining on symbol instead of contract, and dividing the
  win rate by all entries instead of observed entries, each make the suite
  fail. Ignoring `trade_timestamp` also fails.
- `smoke_test.py`, `test_historical_replay.py`, `e2e_validate.py` and
  `final_integration_gate.py` all pass; the gate now also requires the two new
  modules.
- `orders_enabled` remains false in every artifact and no live execution exists.

Blockers / open items
- **Provider gap**: 4 PROVEN is a real number from real evidence, not a
  threshold artefact, but it is small. The binding constraint is observation
  density, not criteria: 20,948 of 31,382 entries have a forward window only
  because the wallet traded that same contract again. 4h klines would give far
  denser MFE, but they require a live `market kline` call, which is exactly the
  dependency that was removed. Offline candle backfill is the next real gain.
- **Chain attribution**: only 17.1% of stored rows carry a chain (the GMGN CLI
  omits it and only post-stamp rows have it). Unattributed rows are kept rather
  than discarded, and the contract address carries the identity, so this is
  safe but imprecise.
- `MIN_SUCCESSFUL_ENTRIES` is redundant with the other two bounds; it is
  documented as a guard rather than presented as independent evidence.
- `wallet_signal_profiles.py` still hardcodes `paper_feedback_calibration: 0.0`.
- The repo has no `.gitignore`, so `__pycache__/` shows up as untracked after
  any local test run. Untracked only; never committed.

Recommended next task
Backfill 4h klines into a stored artifact so post-entry MFE no longer depends
on the wallet happening to trade the same contract again, then re-run this
validation and compare `entries_with_valid_peak` against the current 20,948.
Keep the PROVEN threshold fixed while doing it so the comparison is honest.

## Communication
Every completed stage must leave a concise repository-based handoff containing: status, commit SHA, changed files, tests/results, blockers, and next task.
