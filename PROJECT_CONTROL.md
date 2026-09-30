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

## Current task state
Status: ZERION ADDED AS AN ADDITIONAL HISTORICAL WALLET SOURCE (PROVIDER LAYER ONLY), WITH A MANUAL LIVE SMOKE TEST
Stage commit: see the commit titled "Add the Zerion historical wallet-history provider and a manual live smoke test".

What was added
- New `zerion_layer.py`: a provider/adaptor for the Zerion Wallet Transactions
  API. It fetches decoded transaction history for a wallet and emits records in
  the shape the existing wallet history already uses, so
  `wallet_history_validation` consumes them with no other change.
- New `test_zerion_layer.py`: 85 deterministic fixture tests, no network and no
  `requests` dependency (the module imports it lazily, so the suite runs in a
  bare checkout). `final_integration_gate.REQUIRED_CODE` now requires both.
- Endpoint: `GET https://api.zerion.io/v1/wallets/{address}/transactions/`,
  HTTP Basic auth with `ZERION_API_KEY` as the username and an empty password.
- `ZERION_API_KEY` is the only credential read. It is never printed, logged,
  stored in an artifact or placed in `PROVIDER_STATE`, and a test asserts that
  `provider_state()` and the written archive are both key-free.

Supported capabilities
- Wallet address (path-escaped), chain filtering (`filter[chain_ids]`, project
  names `eth`/`bsc`/`base`/`sol` mapped to Zerion's `ethereum`/
  `binance-smart-chain`/`base`/`solana`), trade filtering
  (`filter[operation_types]`, default `trade` only), historical date range
  (`filter[min_mined_at]` / `filter[max_mined_at]`, accepted as seconds,
  milliseconds or ISO and always sent as the 13 digits the API requires),
  cursor pagination, and retries with rate-limit handling.
- Cursor pagination follows `links.next` verbatim and never hand-builds a
  `page[after]` token, because the cursor is opaque. The first page carries the
  filter query string; later pages are the provider's own URL with no extra
  params. Guarded by a max-page cap, a run-scoped request budget, a repeated
  cursor check, and a refusal to follow any cursor that leaves
  `api.zerion.io` (the request carries Basic-auth credentials).

Retry and rate-limit behaviour
- Retries 429/500/503 with exponential backoff (1s, 2s, 4s, capped at 30s),
  honouring `Retry-After` and `RateLimit-Org-Second-Reset`. Never retries
  400/401/422, which return the same answer however often they are sent.
- Deliberately *not* GMGN's rule. `gmgn_layer.run_gmgn_cli` treats a rate limit
  as terminal for the run because GMGN states that retrying during a ban
  extends it. Zerion documents the opposite, so a transient 429 is retried and
  only an exhausted day/month quota (where no wait can help) stops the run. A
  spent per-second window is paced, not treated as terminal.
- A missing key short-circuits before any request, so an unkeyed run fails
  loudly instead of looking like an empty wallet, and a partial fetch keeps the
  pages it got while still reporting `ok=False`, keeping UNAVAILABLE
  distinguishable from EMPTY.

Normalization
- One row per fungible transfer, so a swap yields the buy leg (`direction=in`)
  and the sell leg (`direction=out`), which is the wallet-centric `side`
  semantics the entry/exit reconstruction is defined against.
- Chain is stored in the project's short names and the contract address is the
  implementation for that transaction's own chain, so a Zerion row and a GMGN
  row of the same token land on the same `whv.asset_identity` key. Base58 Solana
  addresses keep their case.
- Emits `source`, `timestamp` (observed) and `trade_timestamp` (mined_at), the
  row contract `wallet_history_validation.event_ts` depends on; plus
  `transaction_hash`, `chain`, `address`, `symbol`, `side`, `amount_usd`,
  `price_usd`, `token_amount`, `operation_type`, `status`, `maker_tags`.
- Fabricates nothing. No `price_change` / `peak_multiple` / `first_*_timestamp`
  keys are emitted, because Zerion reports no current/entry ratio; no module
  indexes those keys on a history row, so their absence is safe and post-entry
  peaks come from the wallet's own later trades, as for GMGN. `is_open_or_close`
  is left explicitly unknown. Dropped with a reason: non-`confirmed`
  transactions, spam-flagged transactions, `self` transfers, and native-asset
  transfers (no contract, so no token identity). A missing price is not turned
  into a zero-value trade.

Preserved behaviour
- `merge_into_history` is strictly additive. It never modifies, replaces or
  deletes an existing GMGN row, writes one record when both providers already
  agree on an event, keeps rows only one provider has, and does nothing at all
  on an empty or failed fetch. Its identity key is built from
  `whv.asset_identity/event_ts/event_side/event_usd/event_price` and is
  cross-checked against `whv._dedupe` by a test, so the two cannot drift into
  double-counting.
- Verified against the committed 162,081-record dataset in memory: merging a
  Zerion row into one of 400 wallets removed nothing and added exactly the one
  new row. `gmgn_wallet_history.json` is unmodified on disk.
- Scoring, candidate selection, trading logic and live execution are untouched.
  `orders_enabled` remains false in every artifact and the gate still reports
  `live exchange execution: DISABLED`.

Live API smoke test (read-only, real requests to api.zerion.io)
- The GitHub Actions secret `ZERION_API_KEY` is encrypted at rest and is only
  ever injected into a workflow run on a GitHub runner. It is not present in the
  AndCode/PRoot sandbox and cannot be read from a local checkout, and running a
  workflow would require committing and pushing, which was explicitly deferred.
  So no *authenticated* request has been made. Everything up to the credential
  check was verified against the live API with a deliberately invalid key,
  which costs no quota and touches no real data.
- The endpoint and the full filter query are real and accepted. Against
  `0x28c6c06298d514db089934071355e5743bf21d60` (Binance hot wallet, already in
  this repo) the request below returns HTTP 401 with
  "The API key is invalid, please, make sure that you are using a valid key",
  issued exactly once and never retried:
  `page[size]=2&currency=usd&filter[chain_ids]=ethereum&filter[operation_types]=trade&filter[min_mined_at]=1750000000000&filter[max_mined_at]=1750086400000&filter[trash]=only_non_trash`
  A 401 (rather than a 400) confirms the credential *shape* is right: HTTP
  Basic with the key as username and an empty password. A malformed filter would
  have been rejected as a 400 before the credential was ever considered.
- **Two real defects found and fixed, both only findable by hitting the API.**
  1. An unauthenticated request answers **402 Payment Required**, not 401, with
     "Provide an API key via `Authorization: Basic <base64(api_key:)>`".
     402 was in neither the retryable nor the terminal set and was falling
     through as a generic error. It is now terminal, and 401/402/403 are
     labelled `CREDENTIAL_REJECTED` on both `fetch_page` and
     `fetch_wallet_transactions`, so a caller can tell "this request will never
     be served" apart from a wallet that genuinely has no history. `400/404/422`
     are labelled `REQUEST_NOT_SERVABLE`. `TERMINAL_STATUS` was declared but
     never actually referenced; it is now load-bearing.
  2. A request carrying the default `Python-urllib/x.y` agent never reaches
     Zerion: Cloudflare refuses it at the edge with
     "Error 1010: The site owner has blocked access based on your browser's
     signature" (HTTP 403). `python-requests/x.y` and a descriptive agent are
     both accepted, so the integration would probably have worked by luck, since
     `requests` is what the live path uses. Relying on a library's default agent
     to stay allowed is a hidden dependency, so an explicit descriptive
     `User-Agent` is now sent by `request_headers()`.
- Not verified: a 200 response body, so the normalizer has still only been
  exercised against fixtures, and `links.next` pagination has not been seen from
  the live API. A deliberately minimal one-day, one-page request was used, so no
  large backfill was performed and no quota beyond the rejected request was
  spent. No credential, Authorization header or wallet data was written to any
  artifact.

Validation
- `test_zerion_layer.py`: 85 tests (12 added for the findings below and for the
  smoke script itself). Confirmed by mutation, each of which makes the suite
  fail: 429 no longer retried; in/out direction mapping inverted; merge replacing
  existing history; chain-specific implementation address ignored; untrusted
  next-cursor host check removed; fabricated `price_change`/`peak_multiple`;
  failed/pending transactions kept; date range sent in seconds instead of
  milliseconds; trade filter default removed; event identity ignoring the
  transaction hash; explicit User-Agent removed; 402/403 treated as retryable;
  terminal flag suppressed; 402 removed from the terminal set; `page_trace`
  dropped; `http_status` never recorded; smoke redaction disabled; short-key
  redaction guard removed; `MAX_PAGES` raised to 20; `PAGE_SIZE` raised to 100;
  a 200 with zero records reported as a failure.
- `final_integration_gate.py`, `e2e_validate.py`, `test_historical_replay.py` and
  `wallet_history_validation.py` pass, and the gate still reports
  `live exchange execution: DISABLED`. `smoke_test.py` and
  `test_wallet_history_validation.py` could not be run in the AndCode/PRoot
  sandbox (no `pip`, `requests` is not installed and `scanner.py`/`gmgn_layer.py`
  import it at module level); both fail identically at HEAD with no changes
  applied, so this is environmental and pre-existing.
- The smoke script is fully unit-tested offline, so the first time the real
  network path runs is inside the workflow, not at the moment of first
  correctness. Its own tests cover the 200 path, an empty 200 window, a
  followed cursor, the `max_pages` cap, cursor-loop detection, a rejected
  credential, an untrackable address, and credential redaction.

Manual live verification path
- `.github/workflows/zerion-smoke-test.yml` ("Zerion API Smoke Test") runs
  `zerion_smoke_test.py` against the real API. It is `workflow_dispatch` only:
  no `schedule`, `push`, `pull_request`, `workflow_call` or any other automatic
  trigger, because the request spends metered third-party quota and must stay a
  deliberate human action. `permissions: contents: read` and
  `persist-credentials: false` mean the job cannot write to the repository or
  leave a token behind.
- The probe asks for `page[size]=2` over a 90-day window and follows at most one
  `links.next` cursor, so it cannot degenerate into a backfill. It prints HTTP
  status, transaction count, per-page trace, normalized row sample, and explicit
  pass/fail checks for authenticated 200, envelope parsing, normalization and
  cursor advancement. A 200 with zero rows is a PASS: the request worked and the
  window was quiet, and failing it would only invite widening the request.
- The default target is `0x3D457D0B79EFAC77ed38F37870C713D0244479EA`, the wallet
  this repository's own tests use and which is present in
  `gmgn_wallet_history.json`. It is deliberately NOT the Binance hot wallet from
  the Nansen workflow: Zerion declines to track high-volume exchange addresses,
  so that address answers 400 and would prove nothing about the credential. The
  smoke script distinguishes the two failure modes by reason, `CREDENTIAL_
  REJECTED` (401/402/403) versus `REQUEST_NOT_SERVABLE` (400/404/422).
- Credential handling: the key is injected as an environment variable and never
  printed. The script redacts the key, its `key:` form and its Base64 `Basic`
  form from all output, so even a provider that echoes the credential back in an
  error body cannot leak it into a possibly-public Actions log. Redaction
  deliberately leaves ordinary prose alone ("The API key is invalid" survives
  intact) and skips values too short to redact safely, because a one-character
  key would otherwise shred every word in the report. A final step greps the
  captured output for the credential value and fails the job if it appears, so a
  future change that prints a header fails loudly instead of quietly publishing
  a live credential. The run commits nothing.

Blockers / open items
- **The authenticated smoke test has still never actually been executed.** It is
  now one manual click away, but until an operator runs the workflow, the live
  response shape, the real record volume, and live `links.next` pagination remain
  unverified. Everything asserted about the live API so far comes from
  unauthenticated and invalid-key probes.
- **Zerion is wired but not scheduled, and that is still deliberate.** No
  backfill automation was added. `zerion_layer.main()` remains a runnable CLI
  (`ZERION_BACKFILL_WALLET`, optional `ZERION_BACKFILL_CHAINS` / `_FROM` / `_TO`)
  writing `wallet_archive/raw/zerion/transactions/`, and `merge_into_history` is
  available to the collector, but a scheduled backfill spends metered quota and
  commits artifacts, which is a separate deployment decision.
- Zerion is the EVM/Solana primary source rather than a GMGN equivalent for
  every token, and it declines addresses it does not track, so cold-start
  coverage per wallet will differ between the two providers.
- The repo has no `.gitignore`, so `__pycache__/` shows up as untracked after
  any local test run. Untracked only; never committed.

Recommended next task
Run `Zerion API Smoke Test` from the Actions tab (90-day window, default wallet)
and read the result. Expected: `HTTP status: 200` with a small number of
normalized rows and a per-page trace showing a followed cursor. Then, and only
then, merge fetched history through `merge_into_history` and re-run
`wallet_history_validation` with the PROVEN threshold held fixed, comparing
`entries_with_valid_peak` and the PROVEN count against the current 20,948 / 4. If
it returns 401/402, the secret is wrong rather than the code; if 400, the wallet
is untracked and the default should move to a known-tracked address.

## Historical wallet discovery foundation (2026-09-30)

Status: implemented, validated, committed. Read-only foundation. No execution.

What this stage adds
- `historical_discovery.py` is the first half of the historical pipeline: source
  adapters -> normalized observations -> identity -> additive dedup with
  provenance -> reconstruction -> candidate generation. Every provider enters
  through the same `SourceAdapter`, so a fourth source cannot become a fourth
  scoring path.
- `proven_wallet_registry.py` is the minimal `PROVEN_WALLET_REGISTRY` schema and
  interface. It is deliberately *not* populated: it defines the contract, the
  merge semantics and the validation, and nothing else.
- `test_historical_discovery.py` covers both, including the properties that
  protect the rest of the repository.

Design decisions worth keeping
- **An observation is also a history row.** `wallet_history_validation` already
  owns the event identity this repository trusts, so the normalized observation
  reuses its field names (`chain`, `address`, `side`, `amount_usd`,
  `trade_timestamp`, `transaction_hash`) and adds provenance alongside. A second
  "discovery row" format would have needed its own dedup rule, and the two rules
  would eventually disagree about whether one on-chain event is one event.
- **`transaction_hash` and `trade_timestamp` are `None`, never `""` or `0`.** A
  missing value that reads as a present-but-empty one lets a row pass an
  identity check it never earned.
- **Chain names are canonicalized.** Nansen says `ethereum`, GMGN says `eth`.
  Untranslated they produce two different `asset_identity` keys for one token, so
  the same position reconstructs as two entries. An *unmapped* chain is kept
  verbatim rather than folded into a neighbour, because guessing would merge two
  different assets. An absent chain stays `""`: 73% of stored GMGN rows carry
  none, and inventing one would attribute history the data does not support.
- **The base of a merge is accepted verbatim.** Only *incoming* rows are tested
  for a match. Re-deduplicating the stored base collapsed 6 real rows out of
  182,588, because the tolerant matcher considers some stored pairs to be the
  same event. The invariant is now `output >= base`: a provider conflict can add
  evidence, never remove a row. Verified on the full committed dataset: 182,588
  -> 182,588, with 6,191 rows corroborated and repeated ingestion a no-op.
- **Nansen stays a snapshot.** Its archived artifact is a
  profiler `historical-balances` response with no side, no entry price and no
  hash, so it normalizes to `KIND_BALANCE_SNAPSHOT` and contributes zero trades.
  Treating a balance as an execution would invent entries that never happened.
- **Hyperliquid is a real read-only source, and still needs no credentials.**
  `hyperliquid_layer` fetches public fills from the unauthenticated
  `https://api.hyperliquid.xyz/info` endpoint (`userFillsByTime` / `userFills`).
  There is no account, no key, no signature and no wallet connection: the
  documented request body carries only a `type`, a `user` address and a time
  window. Superseded on 2026-09-30, when it was genuinely schema-only; see
  "Hyperliquid public read-only integration" below. A perpetual fill is a real
  trade; what is genuinely unknown is its chain identity, which stays empty
  rather than being guessed.
- **PROVEN is copied, never computed.** `proven_status` is `whv`'s
  `history_class` verbatim, and the criteria are untouched
  (`3 / 2 / 60% / 2x`). Discovery adds only weaker descriptive tiers below
  `QUALIFIED_HISTORICAL_WALLET`. 100 x $20k buys that never double is
  `QUALIFIED_HISTORICAL_WALLET` and explicitly *not* proven: volume is not skill.
- **The registry survives a quiet feed.** `merge_registry` never deletes. A
  wallet that stops appearing keeps its entry, its evidence and its `last_seen`
  and is marked `active_now=False`. `active_now` is metadata about visibility,
  never a filter on inclusion. A `current_feed_observed` flag distinguishes "this
  wallet left the feed" from "nobody looked", without which a stale
  `active_now=True` would persist forever.
- **Unmeasurable metrics are `None`, never `0.0`.** A fabricated `0.0` reads as
  "measured, and it was zero". `drawdown` is always `None`: no daily equity
  curve is stored. `realized_win_rate` is `None` unless *every* entry closed. Both
  gaps, and the semantic caveats on the metrics that are reported, are recorded
  in `UNSUPPORTED_METRICS` / `METRIC_CAVEATS` so a reader can tell a deliberate
  gap from an oversight.
- **`mae_pct` is caveated, not trusted.** `whv` computes MAE as the lowest price
  inside the observed post-entry window, so when a wallet's only post-entry
  observation is its exit, trough == peak and MAE comes out positive and equal to
  MFE. It is reported, with coverage counts and an explicit warning that it is
  never downside risk or drawdown. `whv` was not modified.

Boundaries held
- No change to `scanner.py`, `confluence_engine.py`, `risk_engine.py`,
  `trade_readiness.py`, `paper_trading.py`, `wallet_intel_gate.py`, or any
  threshold. `wallet_quality_engine` is touched by nothing; profiles remain 86
  and the gate still reports live exchange execution DISABLED.
- No protected module imports `historical_discovery`, `proven_wallet_registry`,
  `zerion_history` or `zerion_layer`. Asserted by AST inspection in the tests, so
  a future edit cannot quietly connect a source to scoring.
- The registry reuses `wallet_quality_engine.build_profiles` per wallet rather than
  reimplementing it, and per-wallet scoring was verified equal to whole-dataset
  scoring (15 wallets, 0 mismatches).
- `gmgn_wallet_history.json` byte-identical (md5 `ef1e23b980ae3b4cb0ad239267cbbae4`).
  `historical_replay.json` was regenerated while validating and reverted: only its
  `generated_at` stamp moved.

Tests
- `test_historical_discovery.py` 52 pass; `test_zerion_history.py` 45;
  `test_zerion_layer.py` 99; `test_wallet_history_validation.py` 44.
- `historical_replay.py`, `final_integration_gate.py` (27 modules, live execution
  DISABLED) and `e2e_validate.py` all PASS.

Blockers / open items
- The registry is an interface with no populated artifact. Populating it is a
  separate decision, because doing so commits a data file that then has to be
  regenerated and re-gated on every run.
- Nansen still has no committed archive, so its adapter is exercised by fixtures
  only. Hyperliquid now has a working public client but has never been pointed at
  a real wallet, so it is likewise unexercised against the live API. Neither
  blocks the foundation.
- `ZERION_API_KEY` is absent locally but *is* configured as a GitHub Actions
  secret, and live authentication against it has now been verified. It returns no
  usable transaction history; see "Zerion live transaction retrieval" below. Every
  figure in this section remains fixture- or dataset-derived, not live.
- `requests` is absent locally, so the wallet-history suites run against the
  established blocking stub.
- The repo still has no `.gitignore`, so `__pycache__/` shows up untracked after
  any local test run. Untracked only; never committed.

Recommended next task
Superseded on 2026-09-30 by the "Recommended next task (revised)" section below.
The original text was: "Run the multi-source half end to end against a real
Zerion fetch: backfill one tracked wallet, merge it through `merge_sources`, and
confirm `len(merged) >= len(gmgn_rows)` with the corroborated count above zero and
the PROVEN count unmoved." That is blocked — see "Zerion live transaction
retrieval" for the verified evidence. Do not attempt it until the transaction
source returns data.

## Zerion live transaction retrieval (2026-09-30) — BLOCKED

Status: authentication verified, transaction retrieval NOT verified. The
Zerion-dependent half of the multi-source pipeline cannot be validated yet.

This section supersedes the "Recommended next task" note in the foundation
section above, which required a real Zerion backfill.

What was run
Five read-only `Zerion API Smoke Test` dispatches against three distinct
wallets that are all already tracked by this repository. The smallest safe
probe was used every time: chain `eth`, `page[size]=2`, `max_pages=2`, one API
request per run, and for four of the five runs the default 90-day window.

| Wallet | Repository evidence | Window | Result |
| --- | --- | --- | --- |
| `0x3D457D0B…` (default) | tracked in repo, quiet | 90d | HTTP 200, 0 tx |
| `0x43605d68…` | 190 `eth` rows, 288 hashes | 90d, then 10y | HTTP 200, 0 tx |
| `0xdee657bf…` | 228 `eth` rows, 136 hashes, 8 artifacts | 90d | HTTP 200, 0 tx |

The strongest candidate was chosen deliberately, not at random:
`0xdee657bf65fb0da9e75c9c5c78a6881888b5a641` has 228 stored GMGN rows that are
**all** explicitly `eth` (no empty-chain attribution), a naturally complete
history well below the 500-row per-wallet cap, 136 distinct valid
`0x`+64-hex `transaction_hash` values, trades as recent as 0.5 days before the
run, and corroborating references in `wallet_quality.json`,
`wallet_clusters.json`, `wallet_radar.json`, `wallet_signal_profiles.json`,
`trade_readiness.json`, `paper_trades.json`, `wallet_paper_feedback.json` and
`wallet_performance_memory.json`. A 90-day window returning zero for that
wallet cannot be explained by a quiet feed, by chain ambiguity, by truncated
history, or by the window being too narrow.

What is verified
- **Authentication works.** Every run returned `HTTP 200: ok` with a parsed
  envelope. The credential is live, valid and not being rejected.
- **No key leakage.** `Authorization: bearer …` and any key-bearing query
  string appear zero times across the full run log bundle; the secret renders as
  `***` via the Actions secret store.
- **No repository mutation during the investigation.** The workflow runs with
  `permissions: contents: read` and commits nothing. `HEAD` stayed at
  `c542152` and `gmgn_wallet_history.json` stayed byte-identical
  (md5 `ef1e23b980ae3b4cb0ad239267cbbae4`).
- **No backfill was performed**, so no merge, no registry change and no
  threshold change resulted from this investigation.

What is NOT verified
- **Non-zero transaction retrieval is NOT verified.** Every tested wallet
  returned `transactions: 0` and `normalized: 0 row(s)`.
- **Normalization and pagination remain unverified.** With 0 transactions there
  is nothing to normalize and no `links.next` cursor to follow. The smoke
  script reports `RESULT: PASS` in this case, but its own footnote defines that
  as only "authorized and parsed, window quiet" — it is not evidence that
  normalization or cursor traversal works.

The blocker, stated precisely
The current API key / account / endpoint combination provides **no usable
transaction history for any wallet tested**. Authentication succeeds, the
request shape matches the documented contract (endpoint, `filter[chain_ids]`
chain names, 13-digit millisecond `filter[min_mined_at]`,
`filter[operation_types]`, `filter[trash]`, `currency`, `page[size]`), the
response envelope parses, and the data array is empty every time.

This is **not** a claim that Zerion is broken, and it is **not** evidence of a
defect in this repository's request construction — the same conclusion holds for
a strong and a weak wallet, over a 90-day and a 10-year window, which argues the
cause sits upstream of this code (account entitlement or plan tier, an empty-data
rather than error response for unindexed addresses, or an environment
difference). Diagnosing that is a separate task and needs a decision about
whether to probe account limits.

Boundaries held
- No change to `scanner.py`, `confluence_engine.py`, `risk_engine.py`,
  `trade_readiness.py`, `paper_trading.py`, `wallet_intel_gate.py`, any
  threshold, or any architecture. `wallet_history_validation` still owns PROVEN
  and its `3 / 2 / 60% / 2x` criteria are untouched.
- No Hyperliquid connection was made during the *Zerion* investigation, and no
  wallet has been queried against Hyperliquid at any point. Its client is
  implemented and fixture-tested only.
- No live trading. The integration gate still reports live exchange execution
  DISABLED and profiles remain 86.
- No automatic retries and no further wallet probes without explicit
  instruction.
- `PROJECT_CONTROL.md` records the limitation; no code changed.

## Hyperliquid public read-only integration (2026-09-30)

Status: implemented, validated, committed. Read-only. No account was used and no
credentials exist. **No wallet has been queried yet.**

What this stage adds
- `hyperliquid_layer.py` is a client for Hyperliquid's public `Info` endpoint,
  `POST https://api.hyperliquid.xyz/info`, using the documented `userFillsByTime`
  and `userFills` request types. It is the fetch and the row preparation only:
  every observation is still produced by the shared `historical_discovery`
  pipeline, so Hyperliquid did not become a second identity, dedup or scoring
  path.
- `read_hyperliquid_archive` was added to `historical_discovery` and registered
  in `READERS`, giving Hyperliquid the same offline archive path GMGN, Zerion and
  Nansen already had.
- `test_hyperliquid_layer.py` covers the client in 69 tests with an injected
  transport, so the suite performs no network I/O.

No account, no credentials, no connection
- Hyperliquid's `Info` endpoint is an unauthenticated `POST`. The request body
  carries exactly `type`, `user` and (for the by-time form) `startTime` /
  `endTime`. There is no key, signature, nonce or wallet connection anywhere in
  the path, and the client reads no secret from the environment -- asserted
  directly by tests that scan the request body and the module source.
- `auth_required` is reported as `False` on every result, and a `401` is
  surfaced as an error rather than treated as a prompt for a credential.

Design decisions worth keeping
- **The notional is derived, not invented.** Hyperliquid reports `px` and `sz`
  and no USD value at all. The client multiplies the two, which is arithmetic on
  reported fields, and leaves the result `None` whenever either is missing or
  unparseable. It is never `0.0`, because a fabricated zero reads as "measured,
  and it was zero". The shared normalizer was left alone, so a fixture with no
  USD still reports `no_usd_value` rather than gaining a backdoor.
- **The TWAP placeholder hash is dropped, and stays auditable.** Hyperliquid
  returns an all-zero `hash` for TWAP slice fills. Storing it would collapse
  every TWAP fill of every wallet onto one bogus transaction id, so the client
  nulls it. The original payload is preserved in the observation's `raw`, so the
  dropped value is still recoverable and the drop is counted in
  `zero_hash_dropped`.
- **`tid` is the fill identity.** It is carried as `source_record_id`, and
  intra-response dedup keys on it together with hash and time, so a provider
  that ever reused a `tid` could not silently collapse two different fills.
- **"Incomplete" means "we know we are short", not "the data looks short".** A
  first version flagged `coverage_incomplete` whenever the oldest returned fill
  was later than the requested `startTime`. That is wrong: a wallet with no fills
  for the first hour of a requested day is perfectly complete, and the flag
  would have cried wolf on nearly every healthy response. It now fires only on
  real truncation -- a full page at the page limit, a cursor that cannot
  advance, or the retained-fill ceiling.
- **The retained-history limit is stated, never inferred.** Hyperliquid keeps
  only the 10,000 most recent fills per user, and no single reply can reveal
  whether an older window has already aged out. That is reported as a standing
  property of the source (`provider_history_bounded`,
  `retained_fills_ceiling`) rather than guessed per response, so a caller can
  never mistake a short history for a complete one.
- **Spot and HIP-3 asset ids are kept verbatim.** `coin` arrives as `@107` for
  spot and `xyz:XYZ100` for HIP-3. These are real identities, so they are
  preserved rather than rewritten into a ticker guess.
- **Terminal statuses are not retried.** `400/401/403/404/405/422` fail
  immediately; only `429/5xx` are retried, with `Retry-After` honoured and the
  per-run request budget acting as a hard stop.

Boundaries held
- No change to `scanner.py`, `confluence_engine.py`, `risk_engine.py`,
  `trade_readiness.py`, `paper_trading.py`, `wallet_intel_gate.py`, any
  threshold, or any architecture. `wallet_history_validation` still owns PROVEN
  and its `3 / 2 / 60% / 2x` criteria are untouched.
- `gmgn_wallet_history.json` byte-identical (md5 `ef1e23b980ae3b4cb0ad239267cbbae4`).
  Re-read after the change: 2,215 wallets / 182,588 rows, unchanged.
- No wallet was queried, no backfill was run, and no archive was downloaded.
- No live trading. The integration gate still reports live exchange execution
  DISABLED and profiles remain 86.
- The AST guards still hold, including
  `test_no_protected_module_imports_discovery_or_zerion`: `hyperliquid_layer` is
  imported *by* discovery and by nothing protected.

Tests
- `test_hyperliquid_layer.py` 69 pass. `test_historical_discovery.py` 52;
  `test_zerion_history.py` 45; `test_zerion_layer.py` 99;
  `test_wallet_history_validation.py` 44.
- `historical_replay.py`, `final_integration_gate.py` (27 modules, live execution
  DISABLED) and `e2e_validate.py` all PASS.

## Hyperliquid live verification (2026-09-30) — SUCCEEDED, EMPTY RESULT

Status: the client has now been pointed at the real public API. One request, one
already-tracked wallet, read-only, no credentials, nothing written to disk.

What was sent
- Wallet: `0xdee657bf…`, already tracked in this repository -- 228 GMGN rows, all
  explicitly `eth`, last trade 0.7 days before the query, and referenced in nine
  project artifacts including `wallet_quality.json` and `wallet_clusters.json`.
- Body, exactly as emitted by `hyperliquid_layer.build_fill_request`:
  `{"type": "userFillsByTime", "user": "0xdee657bf…", "startTime": …,
  "endTime": …}` over a 90-day window. No key, signature, nonce, cookie or
  `Authorization` header; the client reads no secret from the environment.

What came back
- **The request succeeded**: `ok=true`, HTTP 2xx, one page, `requests_made=1`,
  zero retries, empty error string. The documented `Info` contract holds for a
  real request: the address and body are accepted, and the response envelope
  parses.
- **0 fills returned.** The `data` array was empty, so there was nothing to
  normalize and nothing to attribute.
- This is the expected shape of the answer rather than a failure. The chosen
  wallet is an Ethereum mainnet trader whose entire committed history is `eth`;
  it has no Hyperliquid perpetual history to return. An empty account is a real
  answer, and the client reports it as a success with an empty row list, not as
  an error.

What this proves, and what it does not
- **Proved:** the endpoint is reachable unauthenticated; the request builder
  produces a body the real API accepts; transport, error handling and the
  empty-response path all behave as documented. The "auth_required: false" claim
  is now observed rather than merely asserted.
- **Not proved:** normalization, provenance, `tid` handling, TWAP-hash dropping
  and timestamp conversion against a *real* response. With 0 rows, those paths
  remain fixture-verified only. Do not report this stage as end-to-end proven.
- One defect was found and fixed by this run: a successful fetch reported **no
  HTTP status at all**, because `status` was only assigned on the failure path
  and absent from the initial result. A success with no status cannot be told
  apart from a request that never reached the provider -- precisely the
  distinction a verification exists to make. Both paths now report it, covered
  by two new tests.

Boundaries held
- No backfill, no archive download, no second wallet, no historical dataset
  modified. `gmgn_wallet_history.json` byte-identical (md5
  `ef1e23b980ae3b4cb0ad239267cbbae4`), re-checked after the run.
- No identity, dedup, scoring or conviction rule touched; no credential created
  or exposed. No live trading: the gate still reports live exchange execution
  DISABLED and profiles remain 86.

Blockers / open items
- **Normalization and provenance are still unverified against real data.** The
  one wallet available from repository evidence had no Hyperliquid fills to
  return. Closing this needs an address that is demonstrably an active
  Hyperliquid perp trader, which this repository does not currently hold --
  sourcing one is a separate decision, and a random address is not an acceptable
  substitute. Zerion is the cautionary precedent in the other direction: there,
  authentication verified perfectly while the data was empty for every wallet
  tried.
- `chain` is still empty for every Hyperliquid observation, so those rows report
  `chain_unknown` and `identity: unknown` in `confidence`. Hyperliquid trades on
  HyperEVM, so the value is arguably known, but setting it would change
  `asset_identity` and the dedup key for this source. That is a deliberate
  decision for the owner, not a side effect to slip in here.
- No fill archive is committed yet, so `read_hyperliquid_archive` currently reads
  an empty directory by design.

## PROVEN_WALLET_REGISTRY populated (2026-09-30) — COMPLETE

Status: the registry is no longer "an interface with no populated artifact".
`proven_wallet_registry.json` is committed, generated from the committed GMGN
archive, and reproducible.

What was built
- `build_proven_wallet_registry.py` — generator. Calls
  `historical_discovery.normalize_rows` then `proven_wallet_registry.build_registry`.
  No new classifier, no new threshold, no network, no provider import.
- `proven_wallet_registry.json` — 2,215 entries, 5.5 MB.
- `test_proven_wallet_registry.py` — 26 focused tests. The module previously had
  no dedicated test file at all; it was only touched indirectly by
  `test_historical_discovery.py` (an import and two AST guards).

Numbers
- Wallets considered: **2,215** (every wallet in the archive; none skipped).
- Admitted as PROVEN: **7**.
- Rejected: **2,208** — `HISTORICAL_ACTIVITY_BUT_UNPROVEN` 1,926 and
  `NO_HISTORY` 282.
- `orders_enabled: false`; `mode: PROVEN_WALLET_REGISTRY_READ_ONLY`.

Criteria used — the existing ones, unchanged and not restated anywhere in the
generator: `observed_entries >= 3`, `successful_entries >= 2`,
`win_rate >= 60.0`, target multiple 2.0x, all from
`wallet_history_validation.reconstruct_wallet`. Every one of the 7 was
re-checked against those four numbers independently: 0 violations.

Rejection reasons, by which criterion actually failed
| pattern | wallets |
| --- | --- |
| `observed<3` + `win_rate<60%` + `successes<2` | 923 |
| `win_rate<60%` + `successes<2` | 910 |
| `win_rate<60%` only | 353 |
| `observed<3` + `successes<2` | 22 |

The 353 that fail on win rate alone are the real near-misses: they clear 3
observed entries *and* 2 successful entries, and are rejected only because the
win rate lands under 60%. They are not "almost proven" in a way that any
threshold change should rescue, and none was made.

The 7 admitted wallets, with the evidence that admitted each
| wallet | observed | successful | win rate | chains |
| --- | --- | --- | --- | --- |
| `0x65b44b28…7c6` | 6 | 4 | 66.7% | eth |
| `0x68b48ffe…e50` | 3 | 2 | 66.7% | eth |
| `0xaecff07c…1e0` | 5 | 4 | 80.0% | eth |
| `0xf13176ec…3fe4` | 3 | 2 | 66.7% | (unstamped) |
| `2ecsMTKR…KhYkT` | 3 | 2 | 66.7% | sol |
| `77n6X7Lt…9yg9` | 3 | 2 | 66.7% | sol |
| `FozzUrGV…cBfUx` | 3 | 2 | 66.7% | sol |

Determinism
`generated_at` is the only wall-clock field, and it is declared as such in the
artifact's own `determinism` block. `determinism.payload_sha256` is a SHA-256
over the entire envelope minus `generated_at` and minus the digest itself.
Verified three ways: two builds in one process agree on everything but
`generated_at`; a separate rebuild reproduces the committed digest exactly
(`python3 build_proven_wallet_registry.py --verify`); and the committed file
minus its single `generated_at` line is byte-identical to an independent
rebuild's canonical serialization.

Two real bugs were found and fixed while establishing that guarantee, both in
the generator and both caught by the tests rather than by inspection:
- The digest was computed before the `determinism` block existed, so it hashed
  an envelope that did not yet contain it and the stored value failed its own
  verification on reload.
- `payload_sha256` is *nested* inside `determinism`, so the top-level exclusion
  never removed it. The build hashed the empty placeholder while a reload
  hashed the real value, so the artifact could never verify itself. A digest
  that cannot check its own file is worse than none, because it looks like a
  guarantee.

Honest limitations, recorded rather than smoothed over
- **`historical_trades` understates activity.** Of 182,588 archive rows,
  112,071 normalize to `kind=TRADE` and 70,517 to `kind=UNKNOWN` because the
  GMGN CLI omits `kind` on most rows. No row is dropped — all 182,588 survive
  normalization and all 2,215 wallets are retained. `historical_trades` is
  simply a narrower count than "rows".
- **`qualified_trades` is 0 for the PROVEN wallets** and low overall. It uses
  the engine's existing `QUALIFIED_TRADE_USD` of $5,000; only 3,133 of 182,588
  rows clear it, and the 7 admitted wallets trade below it. Correct, not a bug,
  but it means this registry does not by itself identify large-size activity.
- **Normalization was required for truthful provenance, and it is not a
  reclassification.** The archive stores raw collector rows with no `kind` and
  no `sources`, so a registry built straight from them would have reported
  `sources: []` and `historical_trades: 0` for all 2,215 wallets. Routing them
  through the existing shared normalizer fixes that — and leaves the
  classification *identical*: same 7 PROVEN, same 1,926 / 282 split. Both
  orderings are asserted in the test suite so this cannot drift unnoticed.
- **`active_now` is `null` for every wallet**, because no live feed was
  consulted. `current_feed_observed: false` records that. `null` means "not
  known", which is different from "left the feed" and must not be read as it.
- Six metrics stay `null` for every entry (`drawdown`, `pre_pump_rate`,
  `first_entry_rate`, `historical_quality_score`, `realized_win_rate`,
  `active_now`). Each entry names them in `provenance.unknown_metrics` rather
  than leaving a silent gap.

Boundaries held
- `gmgn_wallet_history.json` byte-identical: md5 `ef1e23b980ae3b4cb0ad239267cbbae4`,
  104,839,390 bytes, blob `62038d69e4b6d2357ecc66efb47cd4877e11402f`, unchanged
  before, during and after. The generator hashes the archive before and after the
  build and refuses to write if it moved.
- `proven_wallet_registry.py` itself is **unmodified**. Only the generator, the
  artifact and the test file are new. No threshold, no classifier, no
  identity/dedup rule, no scoring, no conviction logic.
- Not wired to anything: `scanner`, `confluence_engine`, `trade_readiness` and
  `paper_trading` all still have zero references to the registry, and live
  exchange execution remains DISABLED.
- No external API called. The generator imports no `requests`, `urllib`,
  `http`, `socket`, or any provider layer, asserted by test.

Tests
- `test_proven_wallet_registry.py` 26 pass. `test_historical_discovery.py` 52;
  `test_wallet_history_validation.py` 44; `test_zerion_history.py` 45;
  `test_zerion_layer.py` 99; `test_hyperliquid_layer.py` 69.
- `historical_replay.py` exit 0, `orders_enabled: false`;
  `final_integration_gate.py` PASS (27 modules, live exchange execution
  DISABLED); `e2e_validate.py` PASS.
- `historical_replay.json` was regenerated by the replay run and reverted: the
  only delta was `generated_at`, per the existing convention.

## Recommended next task (revised 2026-09-30, after the registry population)

**Prove the registry refresh and merge path against the now-populated real
artifact, instead of only against synthetic fixtures.**

Source state at this point:

| Source | State |
| --- | --- |
| GMGN | complete -- real committed archive, 2,215 wallets / 182,588 rows; registry populated from it (7 PROVEN) |
| Zerion | blocked -- authentication verified, zero transactions for every wallet tested |
| Hyperliquid | live-verified -- real request accepted and parsed, 0 fills for the only repo-tracked wallet; normalization/provenance still fixture-only |
| Nansen | no committed archive; fixtures only |

Why this is next
- The registry was built from scratch once. The operation that will actually run
  repeatedly is the *refresh*: build a new envelope and `merge_registry` it into
  the committed one. That path is currently exercised only on small synthetic
  overlays.
- The invariants that matter for a statement of record -- output is never smaller
  than the base, a wallet missing from a new feed is never deleted, a quiet feed
  cannot retire a proven wallet -- are exactly the ones that only become
  meaningful at 2,215 wallets, where a merge bug would silently drop proven
  evidence.
- The populated artifact makes a genuine regression testable: merge the committed
  registry with a rebuild of itself, and with a deliberately truncated feed, and
  assert `proven_count` never falls and no PROVEN wallet disappears. That test
  does not exist yet.
- It needs no credentials, no network and no new provider, and it changes no
  threshold, no classifier and no scoring rule.

Guardrails: the merge must be proven non-destructive *before* any refresh is
treated as routine; do not silently accept a `proven_count` that moves -- report
the number and the entries that changed, exactly as was done when the registry
was first populated; keep `gmgn_wallet_history.json` byte-identical; keep
`orders_enabled: false`; and do not wire the registry into any consumer as part
of this work.

Also open, and deliberately not next
- **Verify Hyperliquid normalization against a real response.** Blocked on
  sourcing, not on code: it needs an address that is demonstrably an active
  Hyperliquid perp trader, and this repository holds none. Guessing an address is
  not acceptable, and a second arbitrary wallet is not evidence either. This is a
  decision for the owner about where such an address may legitimately come from.
- **Zerion transaction retrieval** stays blocked as recorded above.
- **Consuming the registry** -- surfacing the 7 PROVEN wallets in the read-only
  report, or **wiring discovery into the forward evaluation** -- and **setting
  `chain="hyperliquid"`** on Hyperliquid observations, are owner decisions and
  must not be slipped in as a side effect of another task. The first would
  intentionally relax the AST guard that `test_historical_discovery.py` installs
  against protected modules importing `historical_discovery`,
  `proven_wallet_registry`, `zerion_history` or `zerion_layer`; the second would
  change `asset_identity` and the dedup key for this source.
- **`historical_trades` and `qualified_trades` understate the dataset**, as
  recorded above: 70,517 of 182,588 rows carry no `kind`, and the $5,000
  qualified threshold is cleared by only 3,133. Fixing either would mean changing
  the shared normalizer or the engine's existing threshold, neither of which is
  authorized here. Recorded as a known limitation, not silently patched.

## Communication
Every completed stage must leave a concise repository-based handoff containing: status, commit SHA, changed files, tests/results, blockers, and next task.
