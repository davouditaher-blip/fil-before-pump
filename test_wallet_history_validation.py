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


def test_wallet_identity_folds_evm_case_and_preserves_base58():
    # EVM addresses are case-insensitive: the same account in any casing.
    lower = "0x3d457d0b79efac77ed38f37870c713d0244479ea"
    mixed = "0x3D457D0B79EFAC77ed38F37870C713D0244479EA"
    assert whv.wallet_identity(mixed) == lower
    assert whv.wallet_identity(lower) == lower
    # Chain-prefixed forms resolve to the same wallet.
    assert whv.wallet_identity("eth:" + mixed) == lower
    assert whv.wallet_identity("eth-mainnet:" + mixed) == lower
    assert whv.wallet_identity("bsc:" + mixed) == lower
    # Base58 Solana addresses ARE case-sensitive, so they must be preserved
    # byte for byte. Lowercasing them would be corruption, not normalisation.
    base58 = "3d4K8PGBcnJ4sApcx1VX6yiWjnx9oCYYjjgSB4hZPJFY"
    assert whv.wallet_identity(base58) == base58
    # Blank / junk never becomes a usable key.
    assert whv.wallet_identity("") == ""
    assert whv.wallet_identity(None) == ""
    assert whv.wallet_identity("   ") == ""


def test_wallet_lookup_is_case_insensitive_in_both_directions():
    lower = "0x3d457d0b79efac77ed38f37870c713d0244479ea"
    mixed = "0x3D457D0B79EFAC77ed38F37870C713D0244479EA"
    # lowercase stored key, mixed-case lookup
    assert len(whv.rows_for_wallet({lower: [{"a": 1}]}, mixed)) == 1
    # mixed-case stored key, lowercase lookup
    assert len(whv.rows_for_wallet({mixed: [{"a": 1}]}, lower)) == 1
    # A wallet stored under both casings is ONE wallet, and a lookup in either
    # direction returns the complete merged history rather than one slice.
    both = {lower: [{"a": 1}], mixed: [{"a": 2}]}
    assert len(whv.rows_for_wallet(both, lower)) == 2
    assert len(whv.rows_for_wallet(both, mixed)) == 2
    # Casing alone must never duplicate a wallet.
    assert len(whv.wallet_index(both)) == 1
    # ...and normalising the lookup must not delete any stored key.
    assert len(both) == 2
    # A different wallet is still a different wallet.
    other = "0x98feae3174b130f06cad43e7d5d9d3e146f4dd14"
    assert len(whv.rows_for_wallet({lower: [{"a": 1}]}, other)) == 0
    # Missing/blank wallet yields no rows rather than raising.
    assert whv.rows_for_wallet({lower: [{"a": 1}]}, None) == []


def test_wallet_identity_never_uses_symbol():
    # Wallet identity is an address. A symbol is a label, and 1975 of 7396 stored
    # symbols map to more than one contract, so it can never be a wallet key.
    assert whv.wallet_identity("SIGNULL") == "SIGNULL"
    assert whv.wallet_identity("0xa") == "0xa"
    # Two tokens sharing a symbol remain distinguishable by contract address.
    identity_a = ("eth", "0x1111111111111111111111111111111111111111")
    identity_b = ("eth", "0x2222222222222222222222222222222222222222")
    assert identity_a != identity_b


def test_history_update_keeps_every_record_beyond_1000_rows():
    """The ``[-1000:]`` truncation must not come back.

    It used to slice off the OLDEST rows of any wallet that crossed 1000
    records, which is the worst direction to lose history for a forward-return
    reconstruction. A backfill deep enough to exceed the cap would have erased
    exactly the early entries it was collecting.
    """
    import gmgn_layer

    history = {}
    now = T0
    # 1,500 distinct transactions for one wallet, oldest first.
    for i in range(1500):
        gmgn_layer.update_history([{
            "maker": "0x3d457d0b79efac77ed38f37870c713d0244479ea",
            "base_address": f"0xasset{i:04d}",
            "transaction_hash": f"tx{i:05d}",
            "timestamp": T0 + i * 60,
            "side": "buy",
            "amount_usd": 1000.0,
            "price_usd": 1.0,
        }], history)

    rows = history["0x3d457d0b79efac77ed38f37870c713d0244479ea"]
    # Nothing is dropped and nothing is invented.
    assert len(rows) == 1500, f"expected 1500 retained rows, got {len(rows)}"
    # The oldest record is still present, which is what the old cap destroyed.
    assert rows[0]["transaction_hash"] == "tx00000"
    assert rows[-1]["transaction_hash"] == "tx01499"
    # Chronological order is preserved.
    stamps = [r["trade_timestamp"] for r in rows]
    assert stamps == sorted(stamps)
    # Re-running the same transaction updates it instead of duplicating it.
    gmgn_layer.update_history([{
        "maker": "0x3D457D0B79EFAC77ed38F37870C713D0244479EA",
        "base_address": "0xasset0000",
        "transaction_hash": "tx00000",
        "timestamp": T0,
        "side": "buy",
        "amount_usd": 1000.0,
        "price_usd": 1.0,
    }], history)
    # The mixed-case write resolved to the SAME stored key (one wallet, no
    # duplicate created by casing) and still updated in place.
    assert len(history) == 1
    assert len(next(iter(history.values()))) == 1500


# --------------------------------------------------------------------------
# 11. Cross-provider event identity
#
# `_dedupe` used to key strictly on transaction_hash. A hashless GMGN row and a
# hashed Zerion row for one on-chain event therefore counted as two, and the
# reconstructed entry was built from duplicated evidence. These tests pin the
# shared `same_event` rule and, just as importantly, pin that it does not
# collapse genuinely distinct trades.
# --------------------------------------------------------------------------
def test_two_different_transaction_hashes_stay_two_events():
    """Same token, same second, same size, same price, different trades.

    The hash is the only thing separating these. If dedupe ever stops consulting
    it, a real second entry disappears and a wallet's history is understated.
    """
    a = _row(T0, 1.0, transaction_hash="0xfirst")
    b = _row(T0, 1.0, transaction_hash="0xsecond")
    assert whv.same_event(a, b) is False
    assert len(whv.dedupe([a, b])) == 2


def test_hashless_gmgn_and_hashed_zerion_of_one_event_are_one_event():
    """The defect this change exists to fix."""
    gmgn = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa")
    assert "transaction_hash" not in gmgn
    zerion = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                  transaction_hash="0xonchain", source="zerion")
    assert whv.event_hash(gmgn) == ""
    assert whv.same_event(gmgn, zerion) is True
    assert whv.same_event(zerion, gmgn) is True
    assert len(whv.dedupe([gmgn, zerion])) == 1


def test_two_hashless_rows_of_one_event_are_one_event():
    """The pre-existing rule is preserved: same core, no hashes, one event."""
    a = _row(T0, 1.0)
    b = _row(T0, 1.0)
    assert whv.same_event(a, b) is True
    assert len(whv.dedupe([a, b])) == 1


def test_a_row_always_matches_an_identical_copy_of_itself():
    """Self-identity underpins the merge's replay/idempotence guarantee."""
    for row in (_row(T0, 1.0), _row(T0, 1.0, transaction_hash="0xh")):
        assert whv.same_event(row, dict(row)) is True
        assert len(whv.dedupe([row, dict(row), dict(row)])) == 1


def test_differing_timestamp_price_usd_side_or_asset_stay_separate_events():
    """Every field the identity depends on must actually be load-bearing."""
    base = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                transaction_hash="0xh")
    del base["transaction_hash"]
    other = dict(base, transaction_hash="0xzerion")
    assert whv.same_event(base, other) is True
    for field, changed in (
        ("trade_timestamp", T0 + 1),
        ("price_usd", 2.5),
        ("amount_usd", 250.0),
        ("side", "sell"),
        ("address", "0xbbb"),
        ("chain", "sol"),
    ):
        candidate = dict(base, **{field: changed}, transaction_hash="0xzerion")
        assert whv.same_event(base, candidate) is False, field


def test_repeated_replay_collapses_to_one_event():
    """Replaying the same cross-provider data many times adds nothing."""
    gmgn = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa")
    zerion = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                  transaction_hash="0xonchain", source="zerion")
    payload = [gmgn, zerion, gmgn, zerion, gmgn]
    assert len(whv.dedupe(payload)) == 1
    assert len(whv.dedupe(whv.dedupe(payload) * 4)) == 1


def test_a_zerion_only_event_is_preserved():
    """Dedupe must never drop an event just because one provider saw it."""
    zerion = _row(T0 + DAY, 3.0, side="buy", usd=900.0, chain="eth",
                  address="0xccc", transaction_hash="0xonlyzerion",
                  source="zerion")
    gmgn = _row(T0, 1.0, chain="eth", address="0xaaa", transaction_hash="0xaaa1")
    clean = whv.dedupe([gmgn, zerion])
    assert len(clean) == 2
    assert any(r.get("source") == "zerion" for r in clean)


def test_a_hashless_row_without_a_trade_timestamp_is_not_guessed_at():
    """An observation time is not evidence of a trade time.

    Legacy GMGN rows carry no `trade_timestamp`, so their event time falls back
    to when they were *seen*. Matching one of those against a hashed row on that
    basis alone would collapse two different trades that happened to be observed
    in the same second, which is why the cross-provider rule requires a real
    trade timestamp on both sides.
    """
    legacy = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa")
    del legacy["trade_timestamp"]
    assert "transaction_hash" not in legacy
    zerion = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                  transaction_hash="0xonchain")
    assert whv.has_trade_timestamp(legacy) is False
    assert whv.has_trade_timestamp(zerion) is True
    assert whv.same_event(legacy, zerion) is False
    assert len(whv.dedupe([legacy, zerion])) == 2


def test_a_zerion_tx_hash_field_is_read_as_the_hash():
    """Zerion reports the hash as `tx_hash`, not `transaction_hash`.

    If the accessor only knew GMGN's field name, every Zerion row would look
    hashless to the identity rule and cross-provider matching would silently
    never fire.
    """
    gmgn = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa")
    zerion = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                  tx_hash="0xonchain", source="zerion")
    assert "transaction_hash" not in zerion
    assert whv.event_hash(zerion) == "0xonchain"
    assert whv.same_event(gmgn, zerion) is True
    assert len(whv.dedupe([gmgn, zerion])) == 1
    # And two different tx_hash values are still two trades.
    other = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                 tx_hash="0xdifferent", source="zerion")
    assert len(whv.dedupe([zerion, other])) == 2


def test_cross_provider_merge_does_not_depend_on_which_row_arrives_first():
    """The result must be order-independent, since scans arrive in any order."""
    gmgn = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa")
    zerion = _row(T0, 2.0, side="buy", usd=200.0, chain="eth", address="0xaaa",
                  transaction_hash="0xonchain", source="zerion")
    for order in ([gmgn, zerion], [zerion, gmgn], [zerion, gmgn, gmgn],
                  [gmgn, gmgn, zerion], [zerion, zerion, gmgn]):
        assert len(whv.dedupe(order)) == 1, order
    assert whv.same_event(gmgn, zerion) == whv.same_event(zerion, gmgn)


def test_dedupe_still_drops_rows_with_no_usable_identity():
    assert whv.dedupe([{"side": "buy"}, "junk", None, _row(T0, 1.0)]) == [
        _row(T0, 1.0)
    ]


def test_reconstruction_is_unaffected_by_the_shared_dedup_rule():
    """The reconstruction path must use the same rule the merge wrote with."""
    gmgn = _row(T0, 1.0, chain="eth", address="0xaaa", usd=1000.0)
    zerion = _row(T0, 1.0, chain="eth", address="0xaaa", usd=1000.0,
                  transaction_hash="0xonchain", source="zerion")
    later = _row(T0 + DAY, 2.0, chain="eth", address="0xaaa", usd=1000.0,
                 transaction_hash="0xlater", side="sell")
    record = whv.reconstruct_asset(whv.dedupe([gmgn, zerion, later]))
    assert record is not None
    assert record["entry_price"] == 1.0
    assert record["peak_multiple"] == 2.0


def test_the_committed_history_dedup_count_is_unchanged_by_this_rule():
    """The rule must not alter a single count in the committed dataset.

    Every existing hashless-vs-hashed pair in `gmgn_wallet_history.json` is a
    legacy GMGN row with no `trade_timestamp`, so the cross-provider branch must
    decline all of them.
    """
    import json as _json

    with open("gmgn_wallet_history.json") as fh:
        stored = _json.load(fh)

    def strict(rows):
        """The previous strict hash-keyed rule, for comparison."""
        seen, kept = set(), []
        for index, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            identity = whv.asset_identity(row)
            if identity is None:
                continue
            ts = whv.event_ts(row)
            if ts <= 0:
                continue
            key = (identity, ts, whv.event_side(row),
                   round(whv.event_usd(row), 6), round(whv.event_price(row), 12),
                   str(row.get("transaction_hash") or ""))
            if key in seen:
                continue
            seen.add(key)
            kept.append((index, row))
        kept.sort(key=lambda pair: (whv.event_ts(pair[1]), pair[0]))
        return [row for _, row in kept]

    before = after = 0
    for bucket in stored.values():
        if not isinstance(bucket, list):
            continue
        before += len(strict(bucket))
        after += len(whv.dedupe(bucket))
    assert before == after
    assert before > 0


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"Wallet history validation tests: PASS ({len(TESTS)} tests)")
