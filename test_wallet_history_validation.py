"""Deterministic tests for offline historical wallet validation.

Every fixture is a literal hand-written history, so each test pins one rule of
the reconstruction rather than whatever the committed dataset happens to hold.
The properties under test are the project's own historical-validation rules:
canonical chain+contract identity, honest post-entry outcome reconstruction,
UNKNOWN for missing evidence, and an explicit PROVEN threshold.
"""
import wallet_history_validation as whv

DAY = 86400
T0 = 1_700_000_000


def _row(ts, price, side="buy", usd=10000, chain="sol", address="0xasset", symbol="AAA", **extra):
    row = {
        "timestamp": ts,
        "trade_timestamp": ts,
        "chain": chain,
        "address": address,
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "price_usd": price,
    }
    row.update(extra)
    return row


def _triple(address, hit_prices, chain="sol", symbol="AAA"):
    """One asset that entered at 1.0 and later traded at the given prices."""
    rows = [_row(T0, 1.0, address=address, chain=chain, symbol=symbol)]
    for i, price in enumerate(hit_prices):
        rows.append(_row(T0 + DAY * (i + 1), price, address=address, chain=chain, symbol=symbol))
    return rows


# --------------------------------------------------------------------------
# 1. A successful pre-pump wallet
# --------------------------------------------------------------------------
def test_successful_pre_pump_wallet_is_proven():
    """Three genuine 2x pre-pump entries with a 100% rate must be PROVEN."""
    rows = []
    for i, address in enumerate(("0xa", "0xb", "0xc")):
        rows += _triple(address, [2.5, 2.2], symbol=f"S{i}")
    profile = whv.reconstruct_wallet("winner", rows)
    assert profile["history_class"] == whv.PROVEN
    assert profile["proven"] is True
    assert profile["observed_opportunities"] == 3
    assert profile["successful_pre_pump_entries"] == 3
    assert profile["pre_pump_win_rate"] == 100.0
    # The reconstruction must report the real peak multiple, not just a flag.
    example = profile["recent_examples"][0]
    assert example["peak_multiple"] == 2.5
    assert example["target_reached"] is True
    assert example["mfe_pct"] == 150.0
    assert example["days_to_peak"] == 1.0


# --------------------------------------------------------------------------
# 2. A losing / late wallet
# --------------------------------------------------------------------------
def test_losing_wallet_is_not_proven_but_is_not_lost_evidence():
    """Entries that never reached 2x are UNPROVEN, and keep a real win rate."""
    rows = []
    for i, address in enumerate(("0xa", "0xb", "0xc")):
        rows += _triple(address, [1.05, 0.95], symbol=f"L{i}")
    profile = whv.reconstruct_wallet("loser", rows)
    assert profile["history_class"] == whv.ACTIVITY_BUT_UNPROVEN
    assert profile["proven"] is False
    assert profile["observed_opportunities"] == 3
    assert profile["successful_pre_pump_entries"] == 0
    # A real measurement, not the 0% that an UNKNOWN would produce.
    assert profile["pre_pump_win_rate"] == 0.0
    assert profile["unknown_opportunities"] == 0


# --------------------------------------------------------------------------
# 3. Insufficient history / cold start
# --------------------------------------------------------------------------
def test_wallet_with_no_qualifying_entry_is_cold_start():
    profile = whv.reconstruct_wallet("cold", [])
    assert profile["history_class"] == whv.NO_HISTORY
    assert profile["proven"] is False
    # UNKNOWN must never be reported as 0% performance.
    assert profile["pre_pump_win_rate"] is None
    assert profile["observed_opportunities"] == 0


def test_wallet_with_sells_only_has_no_history():
    rows = [_row(T0, 1.0, side="sell"), _row(T0 + DAY, 1.2, side="sell")]
    profile = whv.reconstruct_wallet("seller", rows)
    assert profile["history_class"] == whv.NO_HISTORY
    assert profile["pre_pump_win_rate"] is None


# --------------------------------------------------------------------------
# 4. Missing price
# --------------------------------------------------------------------------
def test_missing_entry_price_is_unknown_not_a_loss():
    """No entry price means no reconstructable outcome: UNKNOWN, never 0%."""
    rows = [_row(T0, 0.0, address="0xa"), _row(T0 + DAY, 9.0, address="0xa")]
    profile = whv.reconstruct_wallet("noprice", rows)
    assert profile["history_class"] == whv.ACTIVITY_BUT_UNPROVEN
    assert profile["proven"] is False
    assert profile["observed_opportunities"] == 0
    assert profile["pre_pump_win_rate"] is None
    assert profile["unknown_opportunities"] >= 1


# --------------------------------------------------------------------------
# 5. Missing exit / current holding
# --------------------------------------------------------------------------
def test_missing_exit_is_reported_as_a_holding_position():
    """No sell inside the window is a current hold, not a failed exit."""
    rows = _triple("0xa", [2.4, 1.9])
    profile = whv.reconstruct_wallet("holder", rows)
    record = profile["all_examples"][0]
    assert record["exit_timestamp"] is None
    assert record["position_state"] == whv.POSITION_HOLDING
    assert record["outcome"] == whv.OUTCOME_SUCCESS
    # The outcome is still measured from the observed peak.
    assert record["peak_multiple"] == 2.4


def test_reconstructed_exit_is_reported():
    rows = [
        _row(T0, 1.0, address="0xa", usd=10000),
        _row(T0 + DAY, 2.4, address="0xa", usd=10000),
        _row(T0 + 2 * DAY, 3.0, side="sell", usd=20000, address="0xa"),
    ]
    profile = whv.reconstruct_wallet("exiter", rows)
    record = profile["all_examples"][0]
    assert record["exit_timestamp"] == T0 + 2 * DAY
    assert record["exit_price"] == 3.0
    assert record["exit_return_pct"] == 200.0
    # Qualified sell value (20k) covers the qualified buy value (20k).
    assert record["position_state"] == whv.POSITION_EXITED


def test_partial_exit_is_reported_as_trimmed():
    """A partial exit is neither 'holding' nor 'exited'."""
    rows = [
        _row(T0, 1.0, address="0xa", usd=10000),
        _row(T0 + DAY, 2.4, address="0xa", usd=10000),
        _row(T0 + 2 * DAY, 3.0, side="sell", usd=5000, address="0xa"),
    ]
    record = whv.reconstruct_wallet("trimmer", rows)["all_examples"][0]
    assert record["position_state"] == whv.POSITION_TRIMMED
    assert record["exit_timestamp"] == T0 + 2 * DAY


# --------------------------------------------------------------------------
# 6. Multiple buys of the same asset
# --------------------------------------------------------------------------
def test_multiple_buys_of_the_same_asset_are_one_position():
    """A re-buy is not a second opportunity; it is a deeper single position."""
    rows = [
        _row(T0, 1.0, address="0xa"),
        _row(T0 + DAY, 1.1, address="0xa"),
        _row(T0 + 2 * DAY, 2.2, address="0xa"),
    ]
    profile = whv.reconstruct_wallet("rebuyer", rows)
    assert profile["opportunities"] == 1
    record = profile["all_examples"][0]
    assert record["repeated_entry"] is True
    assert record["qualified_buy_count"] == 3
    # Entry is the earliest buy, and the peak is measured from that entry.
    assert record["entry_timestamp"] == T0
    assert record["entry_price"] == 1.0
    assert record["peak_multiple"] == 2.2
    # Averaging the re-buys into the entry would understate the real 2.2x.
    assert record["peak_multiple"] == 2.2


def test_repeated_buy_cannot_inflate_the_opportunity_count():
    rows = []
    for address in ("0xa", "0xb", "0xc"):
        rows += [
            _row(T0, 1.0, address=address),
            _row(T0 + DAY, 1.1, address=address),
            _row(T0 + 2 * DAY, 3.0, address=address),
        ]
    profile = whv.reconstruct_wallet("churner", rows)
    assert profile["opportunities"] == 3
    assert profile["observed_opportunities"] == 3


# --------------------------------------------------------------------------
# 7. Same symbol on different contracts / chains
# --------------------------------------------------------------------------
def test_same_symbol_on_different_contracts_stays_separate():
    """A ticker is a label, not identity: two contracts, two positions."""
    rows = _triple("0xaaa", [1.1], symbol="SI")
    rows += _triple("0xbbb", [1.2], symbol="SI")
    profile = whv.reconstruct_wallet("collider", rows)
    assert profile["opportunities"] == 2
    assert profile["successful_pre_pump_entries"] == 0


def test_same_symbol_different_chain_stays_separate():
    rows = _triple("0xaaa", [1.1], chain="sol", symbol="SI")
    rows += _triple("0xaaa", [9.0], chain="eth", symbol="SI")
    profile = whv.reconstruct_wallet("crosschain", rows)
    # Different chain, therefore a different canonical identity.
    assert profile["opportunities"] == 2
    # The 9x on eth is a real separate success, not a sol win.
    assert profile["successful_pre_pump_entries"] == 1
    assert profile["observed_opportunities"] == 2
    assert profile["pre_pump_win_rate"] == 50.0


def test_chain_scoped_evaluation_keeps_unattributed_rows():
    """Only 17% of stored rows carry a chain; unknown chain is not a mismatch."""
    rows = _triple("0xaaa", [1.1], chain="", symbol="AAA")
    profile = whv.reconstruct_wallet("untagged", rows, chain="eth")
    assert profile["opportunities"] == 1
    assert profile["observed_opportunities"] == 1


def test_chain_scoped_evaluation_drops_a_known_mismatch():
    rows = _triple("0xaaa", [1.1], chain="sol", symbol="AAA")
    rows += _triple("0xbbb", [9.0], chain="eth", symbol="AAA")
    profile = whv.reconstruct_wallet("scoped", rows, chain="sol")
    assert profile["opportunities"] == 1
    assert profile["successful_pre_pump_entries"] == 0


# --------------------------------------------------------------------------
# 8. Duplicate records
# --------------------------------------------------------------------------
def test_duplicate_records_are_deduplicated():
    rows = _triple("0xa", [2.5])
    duplicated = rows + [dict(r) for r in rows]
    profile = whv.reconstruct_wallet("dupe", duplicated)
    assert profile["opportunities"] == 1
    assert profile["observed_opportunities"] == 1
    assert profile["successful_pre_pump_entries"] == 1
    # Dedup must not change the measured outcome.
    assert profile["all_examples"][0]["peak_multiple"] == 2.5


def test_deduplication_preserves_chronological_order():
    rows = [
        _row(T0 + 2 * DAY, 2.5, address="0xa"),
        _row(T0, 1.0, address="0xa"),
        _row(T0 + DAY, 1.2, address="0xa"),
    ]
    clean = whv._dedupe(rows)
    assert [whv.event_ts(r) for r in clean] == [T0, T0 + DAY, T0 + 2 * DAY]
    # And the earliest row must still be chosen as the entry.
    assert whv.reconstruct_wallet("unordered", rows)["all_examples"][0]["entry_timestamp"] == T0


# --------------------------------------------------------------------------
# 9. Historical activity, but not enough evidence
# --------------------------------------------------------------------------
def test_one_success_is_not_enough_for_proven():
    """A single good trade is not a track record: depth is required."""
    rows = _triple("0xa", [2.5, 2.4]) + _triple("0xb", [1.1])
    profile = whv.reconstruct_wallet("onehit", rows)
    assert profile["successful_pre_pump_entries"] == 1
    assert profile["observed_opportunities"] == 2
    assert profile["pre_pump_win_rate"] == 50.0
    assert profile["history_class"] == whv.ACTIVITY_BUT_UNPROVEN
    assert profile["proven"] is False


def test_proven_requires_the_documented_minimum_evidence():
    """Depth, productivity and multiple successes are all conjunctive."""
    # 3 observed, 2 successes, 66.7% -> meets every published bound.
    ok = whv.reconstruct_wallet("ok", _triple("0xa", [2.5]) + _triple("0xb", [2.5]) + _triple("0xc", [1.1]))
    assert ok["history_class"] == whv.PROVEN

    # Only 2 observed entries: fails MIN_OBSERVED_ENTRIES even at 100%.
    shallow = whv.reconstruct_wallet("shallow", _triple("0xa", [2.5]) + _triple("0xb", [2.5]))
    assert shallow["observed_opportunities"] == 2
    assert shallow["pre_pump_win_rate"] == 100.0
    assert shallow["history_class"] == whv.ACTIVITY_BUT_UNPROVEN

    # Enough entries but a 50% rate: fails MIN_WIN_RATE_PCT.
    weak = whv.reconstruct_wallet("weak", _triple("0xa", [2.5]) + _triple("0xb", [2.5]) + _triple("0xc", [1.1]) + _triple("0xd", [1.1]))
    assert weak["observed_opportunities"] == 4
    assert weak["pre_pump_win_rate"] == 50.0
    assert weak["history_class"] == whv.ACTIVITY_BUT_UNPROVEN


def test_proven_threshold_is_documented_on_every_profile():
    profile = whv.reconstruct_wallet("w", _triple("0xa", [2.5]))
    criteria = profile["criteria"]
    assert criteria["min_observed_entries"] == whv.MIN_OBSERVED_ENTRIES
    assert criteria["min_win_rate_pct"] == whv.MIN_WIN_RATE_PCT
    assert criteria["min_successful_entries"] == whv.MIN_SUCCESSFUL_ENTRIES
    assert criteria["target_multiple"] == whv.TARGET_MULTIPLE


# --------------------------------------------------------------------------
# Unknown evidence must never masquerade as performance
# --------------------------------------------------------------------------
def test_unknown_windows_never_lower_a_win_rate():
    """Adding unobservable entries must not change a measured rate."""
    measured = whv.reconstruct_wallet("w", _triple("0xa", [2.5]) + _triple("0xb", [2.5]) + _triple("0xc", [2.5]))
    with_unknown = whv.reconstruct_wallet(
        "w",
        _triple("0xa", [2.5]) + _triple("0xb", [2.5]) + _triple("0xc", [2.5])
        + [_row(T0, 1.0, address="0xd")],
    )
    assert measured["pre_pump_win_rate"] == 100.0
    assert with_unknown["pre_pump_win_rate"] == 100.0
    assert with_unknown["unknown_opportunities"] == 1
    assert with_unknown["history_class"] == whv.PROVEN


def test_out_of_window_observations_are_not_forward_evidence():
    """A price after the 14d window is not evidence about that entry."""
    rows = [_row(T0, 1.0, address="0xa"), _row(T0 + 20 * DAY, 50.0, address="0xa")]
    profile = whv.reconstruct_wallet("late", rows)
    assert profile["observed_opportunities"] == 0
    assert profile["successful_pre_pump_entries"] == 0
    assert profile["pre_pump_win_rate"] is None


def test_observation_window_must_start_after_the_entry():
    """The entry row itself is not a post-entry observation."""
    rows = [_row(T0, 1.0, address="0xa")]
    profile = whv.reconstruct_wallet("single", rows)
    assert profile["observed_opportunities"] == 0
    assert profile["unknown_opportunities"] == 1


# --------------------------------------------------------------------------
# Trade timestamp is the event time
# --------------------------------------------------------------------------
def test_trade_timestamp_is_preferred_over_observation_time():
    """Using the scan time as the trade time shifts every entry forward."""
    row = {"trade_timestamp": T0, "timestamp": T0 + 40 * DAY, "price_usd": 1.0}
    assert whv.event_ts(row) == T0


def test_millisecond_timestamps_are_normalised():
    assert whv.event_ts({"timestamp": T0 * 1000}) == T0


def test_row_without_contract_address_has_no_identity():
    assert whv.asset_identity({"symbol": "AAA", "chain": "sol"}) is None


# --------------------------------------------------------------------------
# Summary reporting
# --------------------------------------------------------------------------
def test_validate_history_summarises_all_three_states():
    data = {
        "proven": _triple("0xa", [2.5]) + _triple("0xb", [2.5]) + _triple("0xc", [2.5]),
        "unproven": _triple("0xa", [1.1, 1.2]),
        "cold": [],
    }
    summary = whv.validate_history(data)
    assert summary["orders_enabled"] is False
    assert summary["wallets_evaluated"] == 3
    assert summary["classification_counts"][whv.PROVEN] == 1
    assert summary["classification_counts"][whv.ACTIVITY_BUT_UNPROVEN] == 1
    assert summary["classification_counts"][whv.NO_HISTORY] == 1
    assert summary["proven_wallet_list"] == ["proven"]
    # The evidence threshold must be published in the artifact.
    assert summary["criteria"]["min_observed_entries"] == whv.MIN_OBSERVED_ENTRIES
    assert "never symbol alone" in summary["criteria"]["identity"]


def test_validate_history_handles_empty_and_malformed_input():
    assert whv.validate_history({})["wallets_evaluated"] == 0
    summary = whv.validate_history({"w": ["not-a-dict", None, 5]})
    assert summary["classification_counts"][whv.NO_HISTORY] == 1
    assert summary["orders_enabled"] is False


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"Wallet history validation tests: PASS ({len(TESTS)} tests)")
