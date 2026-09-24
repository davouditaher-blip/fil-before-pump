# Fil Before Pump v2 — Autonomous Wallet Intelligence & Pre-Pump Engine

## Mission
Build a research-first, multi-layer crypto opportunity engine that detects early accumulation and pre-pump conditions, validates them historically, and only promotes new logic after out-of-sample testing and paper trading.

## Non-negotiable principles
- Wallet/Smart Money is the primary signal family.
- A large buy alone is not a strong signal.
- Wallet quality, first-mover behavior, pre-pump history, position state, and convergence matter.
- Technical analysis is context/confirmation, never the primary gate.
- New features are additive; they do not silently remove existing capabilities.
- No look-ahead bias, survivorship bias, wallet double counting, or future-information leakage.
- Failed and successful projects must both be represented in research.
- Safety can hard-block a trade.
- No live trading until backtest + out-of-sample validation + paper trading gates pass.
- Every strategy change is versioned and rollback-capable.

## Engines
### Wallet Intelligence
- >=$5K qualifying buys are a discovery threshold, not proof of quality.
- Classify New Entry / Add / Reduce / Full Exit.
- Wallet Quality: win rate, ROI, PnL, profit factor, trade count, drawdown, average win/loss, holding time, diversification.
- Pre-Pump History and First-Mover Score.
- Current position/exit state.

### Wallet Convergence / Clustering
- Shared wallets across assets.
- Weight by wallet quality; 1–2 highly credible wallets may matter.
- Detect likely related wallets to reduce double counting.

### Project Intelligence
- Historical project similarity and leading indicators: users, TVL, fees, liquidity, holders, DEX activity, development/ecosystem activity, valuation.
- Use successful and failed project cohorts.
- HYPE is a historical reference, not a hard template.

### Volume Intelligence
- 1D, 3D, 7D, 14D internally.
- Output can emphasize 24h / prior day / 2 days prior.
- Expansion, acceleration, accumulation-vs-price timing and futures volume.

### Market Intelligence
- BTC/ETH trend and volume.
- BTC dominance, Fear & Greed, altcoin regime and reliable ETF-flow context.
- Futures OI, funding, long/short, liquidations and squeeze context.
- Context/risk adjustment rather than automatic rejection.

### Safety Intelligence
- Honeypot, mint/freeze authority, LP/liquidity, dev/insider wallets, holder concentration, wallet clusters, bundles, unlocks and rug-risk indicators.
- Hard-block unsafe assets.

### Technical Intelligence
- RSI 5m/15m/1h, EMA 5/13/20/50/200, VWAP, MACD, Ichimoku, higher-low/compression, resistance and breakout context.
- Confirmation/context only unless explicitly validated otherwise.

## Opportunity classes
- A: Wallet-led
- B: Project-led
- C: Confluence (Wallet + Project + Volume)

## Confluence engine
Return explainable component scores, confidence, data freshness, missing-data penalties, safety status and evidence. Do not silently hard-reject a strong wallet candidate because of technical indicators.

## Research engine
Research -> hypothesis -> historical data -> backtest -> out-of-sample validation -> paper trading -> compare against current version -> promote only if evidence improves the defined objective without unacceptable risk.

## Backtest
Run 30/90/180-day windows when data permits with $100 reference capital and report final balance, ROI, win rate, wins/losses, gross profit/loss, fees, slippage, max drawdown, profit factor, average win/loss, best/worst trade, trade count, concurrent positions, detection precision/recall-style metrics and time-to-move distributions where measurable.

## Anti-leakage
At signal timestamp T, only data timestamped <= T may be used. Freeze features at T. Do not use future wallet PnL. Do not double-count repeated snapshots. Distinguish Add from New Entry. Avoid survivorship bias by reconstructing the eligible universe as of each historical timestamp where possible. Model execution delay, fees and slippage.

## Paper/live progression
Backtest -> OOS -> paper trading -> controlled live.
Live mode must include position sizing, SL/TP, trailing, exposure limits, kill switch, exchange/API error handling, audit log and emergency exit. No withdrawal permission.

## Versioning
Every strategy version records change, rationale, evidence, datasets, backtest, OOS, paper results, decision and rollback target.

## Repository integration
Preserve/refactor the existing scanner, GMGN, wallet, Telegram and Actions components rather than discarding them. Migrate large JSON histories to a more suitable analytical store only with reproducibility preserved.

## Definition of done
1. Normalize required data.
2. Produce explainable candidates.
3. Reconstruct historical decisions without future leakage.
4. Backtest 30/90/180 days when data permits.
5. Produce OOS results.
6. Run paper trading.
7. Record every strategy version.
8. Fail safely on stale/missing data.
9. Expose health/error state.
10. Keep live trading disabled until explicit promotion criteria pass.
