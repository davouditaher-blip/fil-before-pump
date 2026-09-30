"""Tests for the Zerion -> wallet-history adapter.

Every fixture is hand-written so each test pins one rule of the merge rather
than whatever the committed dataset happens to hold. The central property under
test is that a second provider must never inflate a count: a Zerion row that
describes an event GMGN already holds has to leave the trade count alone.
"""
import json
import tempfile
from pathlib import Path

import wallet_history_validation as whv
import wallet_quality_engine as wqe
import zerion_history as zh
import zerion_layer as zl

T0 = 1_700_000_000
DAY = 86400
# Lowercase: wallet_history_validation.wallet_identity canonicalizes, and both
# merge_sources and the archive loader key their output by that form.
WALLET = "0x3d457d0b79efac77ed38f37870c713d0244479ea"
ADDR = "0x4e67db19044549ff420860834c91b45bad298722"


def _gmgn(ts, side="buy", price=2.0, usd=200.0, hashed=None, trade_ts="keep",
          chain="eth", address=ADDR, symbol="AAA"):
    """A GMGN-shaped row. ``trade_ts`` is 'keep'/'none'/'value'."""
    row = {
        "timestamp": ts,
        "chain": chain,
        "address": address,
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "price_usd": price,
    }
    if trade_ts == "keep":
        row["trade_timestamp"] = ts
    elif trade_ts == "value":
        row["trade_timestamp"] = trade_ts_value(ts)
    if hashed:
        row["transaction_hash"] = hashed
    return row


def trade_ts_value(ts):
    return ts


def _zerion(ts, side="buy", price=2.0, usd=200.0, hashed="0xzonchain",
            chain="eth", address=ADDR, symbol="AAA"):
    """A Zerion-shaped row: always hashed, always a real trade time."""
    return {
        "source": "zerion",
        "timestamp": ts + 60,          # observation lags the trade
        "trade_timestamp": ts,
        "transaction_hash": hashed,
        "chain": chain,
        "address": address,
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "price_usd": price,
    }


# --------------------------------------------------------------------------
# 1. Dedup rules
# --------------------------------------------------------------------------
def test_hashless_legacy_gmgn_plus_hashed_zerion_same_event_is_one_event():
    """The collision that motivated this module.

    A legacy GMGN row has no hash and no trade time. Zerion describes the same
    trade with both. The event count must stay at one.
    """
    gmgn = _gmgn(T0, trade_ts="none")
    assert whv.event_hash(gmgn) == ""
    assert whv.has_trade_timestamp(gmgn) is False
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [_zerion(T0)]})
    rows = merged[WALLET]
    assert len(rows) == 1, "a second trade was created for one on-chain event"
    # GMGN provenance is retained; the row was enriched, not replaced.
    assert "gmgn" in zh.provenance_of(rows[0])
    assert "zerion" in zh.provenance_of(rows[0])
    # Zerion supplied what GMGN could not.
    assert rows[0]["transaction_hash"] == "0xzonchain"
    assert rows[0]["trade_timestamp"] == T0
    assert rows[0]["trade_time_known"] is True


def test_hashless_legacy_gmgn_plus_zerion_different_event_is_two_events():
    """A different time, size, price, side or asset is a different trade."""
    gmgn = _gmgn(T0, trade_ts="none")
    for field, value in (
        ("trade_timestamp", T0 + 9),
        ("price_usd", 2.5),
        ("amount_usd", 250.0),
        ("side", "sell"),
        ("address", "0xdeadbeef00000000000000000000000000000001"),
    ):
        zerion = _zerion(T0)
        zerion[field] = value
        merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [zerion]})
        assert len(merged[WALLET]) == 2, f"{field} should have stayed a second trade"


def test_hashed_gmgn_plus_zerion_same_hash_is_one_event():
    gmgn = _gmgn(T0, hashed="0xsame")
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [_zerion(T0, hashed="0xsame")]})
    assert len(merged[WALLET]) == 1
    row = merged[WALLET][0]
    # The event is one, but the evidence behind it records both providers.
    assert zh.provenance_of(row) == ["gmgn", "zerion"]
    assert zh.has_corroboration(row) is True
    assert zh._corroborations(row)[0]["transaction_hash"] == "0xsame"


def test_corroboration_does_not_overwrite_the_stored_row():
    """A second provider's read must not rewrite the stored row's fields."""
    gmgn = {"timestamp": T0 + 900, "trade_timestamp": T0, "transaction_hash": "0xsame",
            "chain": "eth", "address": ADDR, "symbol": "AAA", "side": "buy",
            "amount_usd": 20000.0, "price_usd": 2.0}
    zerion = dict(gmgn, source="zerion", timestamp=T0 + 930,
                  chain="")            # Zerion saw it, with its own lag
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [zerion]})
    row = merged[WALLET][0]
    # The GMGN values survive verbatim; only the evidence trail is added.
    assert row["amount_usd"] == 20000.0
    assert row["price_usd"] == 2.0
    assert row["timestamp"] == T0 + 900, "the stored observation time was rewritten"
    assert row["chain"] == "eth"
    assert zh.provenance_of(row) == ["gmgn", "zerion"]


def test_a_gmgn_only_row_is_not_marked_as_corroborated():
    merged = zh.merge_sources({WALLET: [_gmgn(T0, hashed="0xonly")]}, {WALLET: []})
    assert zh.provenance_of(merged[WALLET][0]) == ["gmgn"]
    assert zh.has_corroboration(merged[WALLET][0]) is False


def test_hashed_gmgn_plus_zerion_different_hash_is_two_events():
    """Two real trades that happen to agree on everything but the hash."""
    gmgn = _gmgn(T0, hashed="0xaaa")
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [_zerion(T0, hashed="0xbbb")]})
    assert len(merged[WALLET]) == 2
    hashes = {whv.event_hash(r) for r in merged[WALLET]}
    assert hashes == {"0xaaa", "0xbbb"}


def test_both_hashless_same_event_keeps_existing_semantics():
    """The pre-existing rule for two hashless rows is unchanged."""
    a, b = _gmgn(T0, trade_ts="none"), _gmgn(T0, trade_ts="none")
    assert whv.same_event(a, b) is True
    assert len(whv.dedupe([a, b])) == 1
    assert len(zh.merge_sources({WALLET: [a]}, {WALLET: [b]})[WALLET]) == 1


def test_zerion_only_event_is_added_exactly_once():
    gmgn = _gmgn(T0, hashed="0xgmgnonly")
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [_zerion(T0 + DAY, hashed="0xnew")]})
    assert len(merged[WALLET]) == 2
    added = [r for r in merged[WALLET] if zh.provenance_of(r) == ["zerion"]]
    assert len(added) == 1


def test_repeated_zerion_ingestion_adds_no_duplicates():
    gmgn = [_gmgn(T0 + i * 60, trade_ts="none") for i in range(3)]
    zerion = [_zerion(T0 + i * 60) for i in range(3)]
    once = zh.merge_sources({WALLET: list(gmgn)}, {WALLET: list(zerion)})
    assert len(once[WALLET]) == 3
    # Ingesting the same Zerion batch again, twice over, changes nothing.
    twice = zh.merge_sources(once, {WALLET: zerion + zerion})
    assert len(twice[WALLET]) == 3
    thrice = zh.merge_sources(twice, {WALLET: zerion})
    assert len(thrice[WALLET]) == 3


def test_enrichment_does_not_mutate_the_input_history():
    gmgn = {WALLET: [_gmgn(T0, trade_ts="none")]}
    before = json.dumps(gmgn, sort_keys=True)
    zh.merge_sources(gmgn, {WALLET: [_zerion(T0)]})
    assert json.dumps(gmgn, sort_keys=True) == before
    assert "transaction_hash" not in gmgn[WALLET][0]


def test_evidence_that_does_not_prove_identity_keeps_both_rows():
    """A lost trade is worse than a duplicate; the safe branch keeps both."""
    gmgn = _gmgn(T0, trade_ts="none", price=2.0)
    zerion = _zerion(T0, price=2.0000004, usd=200.02)   # rounds apart, not equal
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [zerion]})
    assert len(merged[WALLET]) == 2


def test_a_zerion_row_without_a_trade_time_is_not_used_to_enrich():
    """Without a real trade time the evidence is not strong enough to merge."""
    gmgn = _gmgn(T0, trade_ts="none")
    zerion = _zerion(T0)
    zerion["trade_timestamp"] = None
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [zerion]})
    assert len(merged[WALLET]) == 2


# --------------------------------------------------------------------------
# 2. Legacy protection: the real 17,487-pair collision class
# --------------------------------------------------------------------------
def test_the_stored_legacy_collision_class_is_never_collapsed():
    """Reproduce the stored data's own shape and prove it survives.

    A hashless GMGN row and a hashed GMGN row that share a core are the 17,487
    pairs found in the committed dataset. They must stay two records.
    """
    legacy = {
        "timestamp": T0,
        "chain": "",                       # 73% of stored rows have no chain
        "address": ADDR,
        "symbol": "AAA",
        "side": "buy",
        "amount_usd": 140.26,
        "price_usd": 7.4e-05,
    }
    enriched = {
        "timestamp": T0 + 14,              # observation lag
        "trade_timestamp": T0,
        "transaction_hash": "0xabc",
        "chain": "",
        "address": ADDR,
        "symbol": "AAA",
        "side": "buy",
        "amount_usd": 140.26,
        "price_usd": 7.4e-05,
    }
    assert whv.same_event(legacy, enriched) is False
    merged = zh.merge_sources({WALLET: [legacy, enriched]}, {WALLET: []})
    assert len(merged[WALLET]) == 2, "GMGN-vs-GMGN legacy pairs must not collapse"
    assert whv.validate_history({WALLET: merged[WALLET]})["wallets_evaluated"] == 1


def test_the_committed_history_row_count_cannot_decrease():
    """A merge is additive-or-neutral, never subtractive."""
    stored = json.loads(Path("gmgn_wallet_history.json").read_text(encoding="utf-8"))
    sample = {
        wallet: rows for wallet, rows in list(stored.items())[:200]
        if isinstance(rows, list)
    }
    before = sum(len(r) for r in sample.values())
    merged = zh.merge_sources(sample, {})
    after = sum(len(r) for r in merged.values())
    assert after >= before
    assert after == before, "an empty Zerion input changed the GMGN row count"


def test_legacy_enrichment_does_not_inflate_the_committed_dataset():
    """Simulate the full legacy class for 50 stored wallets and count it.

    Every hashless legacy GMGN row that a Zerion row can corroborate must be
    enriched in place, never appended.
    """
    stored = json.loads(Path("gmgn_wallet_history.json").read_text(encoding="utf-8"))
    wallets = [
        (w, r) for w, r in stored.items()
        if isinstance(r, list) and r
    ][:50]
    gmgn = {w: json.loads(json.dumps(r)) for w, r in wallets}
    zerion = {}
    for wallet, rows in wallets:
        built = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if zh._legacy_match_key(row) is None:
                continue
            identity = whv.asset_identity(row)
            if identity is None:
                continue
            built.append({
                "source": "zerion",
                "timestamp": whv.event_ts(row) + 30,
                "trade_timestamp": whv.event_ts(row),
                "transaction_hash": f"0x{abs(hash((wallet, whv.event_core(row)))):064x}",
                "chain": identity[0] or "sol",
                "address": identity[1],
                "symbol": row.get("symbol", ""),
                "side": whv.event_side(row),
                "amount_usd": whv.event_usd(row),
                "price_usd": whv.event_price(row),
            })
        if built:
            zerion[wallet] = built

    before = sum(len(r) for r in gmgn.values())
    merged = zh.merge_sources(gmgn, zerion)
    after = sum(len(r) for r in merged.values())
    assert before > 0
    assert after == before, f"{after - before} legacy events were double counted"
    enriched = [
        row for rows in merged.values() for row in rows
        if "zerion" in zh.provenance_of(row)
    ]
    assert enriched, "the fixture produced no enrichment at all"


# --------------------------------------------------------------------------
# 3. Trade time
# --------------------------------------------------------------------------
def test_trade_timestamp_wins_over_observation_time():
    assert wqe._ts({"trade_timestamp": T0, "timestamp": T0 + 900}) == T0
    assert zh.to_quality_row(_zerion(T0))["timestamp"] == T0


def test_observation_time_is_the_fallback():
    assert wqe._ts({"timestamp": T0}) == T0
    assert wqe._ts({"time": T0}) == T0
    assert wqe._ts({"ts": T0}) == T0
    assert wqe._ts({"trade_timestamp": None, "timestamp": T0}) == T0
    assert wqe._ts({"trade_timestamp": 0, "timestamp": T0}) == T0
    assert wqe._ts({"trade_timestamp": "", "timestamp": T0}) == T0


def test_millisecond_normalization_is_unchanged():
    ms = T0 * 1000
    assert wqe._ts({"trade_timestamp": ms}) == T0
    assert wqe._ts({"timestamp": ms}) == T0
    assert wqe._ts({"trade_timestamp": ms + 500}) == T0      # truncated, as before
    assert whv.event_ts({"trade_timestamp": ms}) == T0
    assert zh.to_quality_row({"trade_timestamp": ms, "timestamp": 1,
                              "chain": "eth", "address": ADDR,
                              "side": "buy"})["timestamp"] == T0


def test_the_quality_fix_works_for_gmgn_rows_too():
    gmgn = {"timestamp": T0 + 900, "trade_timestamp": T0, "chain": "eth",
            "address": ADDR, "symbol": "AAA", "side": "buy",
            "amount_usd": 100.0, "price_usd": 1.0}
    assert wqe._ts(gmgn) == T0


# --------------------------------------------------------------------------
# 4. No inflation of quality figures
# --------------------------------------------------------------------------
def _qualified(data):
    profiles = wqe.build_profiles(data)
    return {
        "profiles": len(profiles),
        "qualified_buys": sum(p["qualified_buys"] for p in profiles.values()),
        "qualified_buy_usd": round(sum(p["qualified_buy_usd"] for p in profiles.values()), 2),
    }


def test_a_duplicated_event_does_not_raise_qualified_buy_usd():
    """The headline property: same event from two providers, same numbers."""
    gmgn = [
        {"timestamp": T0 + 900, "chain": "eth", "address": ADDR, "symbol": "AAA",
         "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0},
        {"timestamp": T0 + 900 + 30, "trade_timestamp": T0 + 900, "chain": "eth",
         "address": ADDR, "symbol": "AAA", "side": "sell",
         "amount_usd": 24000.0, "price_usd": 2.4},
    ]
    zerion = [{
        "source": "zerion", "timestamp": T0 + 930, "trade_timestamp": T0 + 900,
        "transaction_hash": "0xdup", "chain": "eth", "address": ADDR,
        "symbol": "AAA", "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0,
    }]
    before = _qualified({WALLET: json.loads(json.dumps(gmgn))})
    after = _qualified(zh.merge_sources({WALLET: gmgn}, {WALLET: zerion}))
    assert after["qualified_buys"] == before["qualified_buys"]
    assert after["qualified_buy_usd"] == before["qualified_buy_usd"]


def test_a_duplicated_event_does_not_raise_qualified_buy_count():
    gmgn = [
        {"timestamp": T0 + 900, "chain": "eth", "address": ADDR, "symbol": "AAA",
         "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0},
        {"timestamp": T0 + 1200, "chain": "eth", "address": ADDR, "symbol": "AAA",
         "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0},
    ]
    zerion = [{
        "source": "zerion", "timestamp": T0 + 930, "trade_timestamp": T0 + 900,
        "transaction_hash": "0xdup", "chain": "eth", "address": ADDR,
        "symbol": "AAA", "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0,
    }]
    before = _qualified({WALLET: json.loads(json.dumps(gmgn))})
    after = _qualified(zh.merge_sources({WALLET: gmgn}, {WALLET: zerion}))
    assert after["qualified_buys"] == before["qualified_buys"] == 2


def test_a_genuinely_new_zerion_event_is_counted_once():
    gmgn = [
        {"timestamp": T0 + 900, "chain": "eth", "address": ADDR, "symbol": "AAA",
         "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0},
    ]
    new = {
        "source": "zerion", "timestamp": T0 + DAY, "trade_timestamp": T0 + DAY,
        "transaction_hash": "0xbrandnew", "chain": "eth",
        "address": "0x00000000000000000000000000000000000000ff",
        "symbol": "BBB", "side": "buy", "amount_usd": 30000.0, "price_usd": 3.0,
    }
    before = _qualified({WALLET: json.loads(json.dumps(gmgn))})
    after = _qualified(zh.merge_sources({WALLET: gmgn}, {WALLET: [new]}))
    assert after["qualified_buys"] == before["qualified_buys"] + 1
    assert after["qualified_buy_usd"] == before["qualified_buy_usd"] + 30000.0
    # and not twice
    twice = _qualified(zh.merge_sources(
        zh.merge_sources({WALLET: gmgn}, {WALLET: [new]}), {WALLET: [new]}
    ))
    assert twice == after


# --------------------------------------------------------------------------
# 5. Quality row projection
# --------------------------------------------------------------------------
def test_quality_row_carries_the_full_row_contract():
    row = zh.to_quality_row(_zerion(T0))
    for field in zh.QUALITY_FIELDS:
        assert field in row, field
    assert row["chain"] == "eth"
    assert row["address"] == ADDR
    assert row["side"] == "buy"
    assert row["amount_usd"] == 200.0
    assert row["price_usd"] == 2.0
    assert row["source"] == "zerion"
    assert row["sources"] == ["zerion"]


def test_quality_row_never_fabricates_gmgn_only_fields():
    row = zh.to_quality_row(_zerion(T0))
    for field in zh.GMGN_ONLY_FIELDS:
        assert field not in row, f"{field} must be absent, not defaulted"
    # Absent, not zero: is_open_or_close=0 would read as a position open.
    assert "is_open_or_close" not in row
    assert "maker_tags" not in row


def test_quality_row_rejects_rows_with_no_usable_identity():
    assert zh.to_quality_row(None) is None
    assert zh.to_quality_row("junk") is None
    assert zh.to_quality_row({"timestamp": T0}) is None
    assert zh.to_quality_row({"chain": "eth", "side": "buy"}) is None
    assert zh.quality_rows([{"chain": "eth"}]) == []


def test_quality_rows_projects_a_whole_wallet():
    rows = zh.quality_rows([_zerion(T0), _zerion(T0 + DAY, side="sell")])
    assert [r["side"] for r in rows] == ["buy", "sell"]


def test_quality_rows_are_accepted_by_the_quality_engine():
    merged = zh.merge_sources(
        {WALLET: [_gmgn(T0, trade_ts="none", usd=20000.0)]},
        {WALLET: [_zerion(T0, usd=20000.0),
                  _zerion(T0 + DAY, side="sell", usd=20000.0, hashed="0xsell")]},
    )
    rows = zh.quality_rows(merged[WALLET])
    assert len(rows) == 2
    assert wqe._usd(rows[0]) >= wqe.THRESHOLD_USD
    profiles = wqe.build_profiles({WALLET: rows})
    assert WALLET in profiles
    assert profiles[WALLET]["qualified_buys"] == 1


# --------------------------------------------------------------------------
# 6. Archive
# --------------------------------------------------------------------------
def _fetch_result(rows, raw, fetched_at=T0, wallet=WALLET):
    return {
        "wallet": wallet, "endpoint": zl.TRANSACTIONS_PATH,
        "fetched_at": fetched_at, "coverage": {"from": None, "to": None},
        "ok": True, "error": None, "truncated": False, "pages": 1,
        "transactions": len(raw), "rows": rows, "raw_transactions": raw,
    }


def test_archive_keeps_the_raw_transaction_payload():
    """The fetch already collected these; they used to be dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        raw = [{"id": "0x1", "attributes": {"hash": "0x1", "mined_at": "2023-11-14T22:13:20+00:00"}}]
        rows = zl.normalize_transaction(raw[0], now_ts=T0)
        path = zl.write_archive(_fetch_result(rows, raw), tmp)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["raw_transactions"] == raw
        assert stored["summary"]["raw_transactions"] == 1
        assert stored["records"] == rows


def test_the_fetch_actually_keeps_its_raw_transactions():
    """End to end through the paginator, so the field is really populated."""
    payload = {"data": [{
        "type": "transactions", "id": "0xfeed",
        "attributes": {
            "operation_type": "trade", "hash": "0xfeed", "status": "confirmed",
            "mined_at": "2023-11-14T22:13:20+00:00",
            "transfers": [{
                "fungible_info": {"symbol": "AAA",
                                  "implementations": [{"chain_id": "ethereum", "address": ADDR,
                                                       "decimals": 18}]},
                "direction": "in",
                "quantity": {"float": 10.0, "numeric": "10", "int": "10", "decimals": 18},
                "price": 2.0, "value": 20.0,
            }],
        },
        "relationships": {"chain": {"data": {"id": "ethereum"}}},
    }], "links": {"self": "https://api.zerion.io/v1/wallets/x/transactions/"}}

    def fake_get(url, params, headers, auth, timeout):
        return type("R", (), {
            "status_code": 200, "headers": {}, "text": "",
            "json": lambda self=None: payload,
        })()

    result = zl.fetch_wallet_transactions(
        WALLET, api_key="test-key", http_get=fake_get, sleep=lambda _s: None,
        now_ts=T0,
    )
    assert result["ok"] is True, result.get("error")
    assert result["raw_transactions"] == payload["data"]
    assert result["rows"], "the raw capture broke normalization"
    assert len(result["raw_transactions"]) == 1


def test_two_fetches_of_one_wallet_do_not_overwrite_each_other():
    with tempfile.TemporaryDirectory() as tmp:
        first = zl.write_archive(_fetch_result([], [], fetched_at=T0), tmp)
        second = zl.write_archive(_fetch_result([], [], fetched_at=T0 + 3600), tmp)
        assert first != second
        assert first.exists() and second.exists()
        assert len(list(Path(tmp).glob("*.json"))) == 2
        # the first snapshot is untouched
        assert json.loads(first.read_text(encoding="utf-8"))["fetched_at"] == T0


def test_two_fetches_in_the_same_second_still_keep_both():
    with tempfile.TemporaryDirectory() as tmp:
        first = zl.write_archive(_fetch_result([], [], fetched_at=T0), tmp)
        second = zl.write_archive(_fetch_result([], [], fetched_at=T0), tmp)
        assert first != second
        assert first.exists() and second.exists()


def test_the_loader_discovers_every_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        rows_a = [_zerion(T0, hashed="0xone")]
        rows_b = [_zerion(T0 + DAY, hashed="0xtwo", side="sell")]
        zl.write_archive(_fetch_result(rows_a, [], fetched_at=T0), tmp)
        zl.write_archive(_fetch_result(rows_b, [], fetched_at=T0 + 3600), tmp)
        zl.write_archive(_fetch_result(rows_a, [], fetched_at=T0 + 7200), tmp)
        history = zh.load_zerion_history(tmp)
        assert len(history) == 1
        wallet = next(iter(history))
        assert len(history[wallet]) == 2, "overlapping snapshots must not duplicate"
        hashes = {whv.event_hash(r) for r in history[wallet]}
        assert hashes == {"0xone", "0xtwo"}


def test_the_loader_preserves_trade_time_and_never_uses_fetch_time():
    with tempfile.TemporaryDirectory() as tmp:
        zl.write_archive(_fetch_result([_zerion(T0)], [], fetched_at=T0 + 9999), tmp)
        rows = next(iter(zh.load_zerion_history(tmp).values()))
        assert len(rows) == 1
        assert rows[0]["trade_timestamp"] == T0
        assert whv.event_ts(rows[0]) == T0
        assert rows[0]["trade_time_known"] is True
        assert rows[0]["source"] == "zerion"
        assert rows[0]["sources"] == ["zerion"]


def test_the_loader_flags_a_row_with_no_trade_time_instead_of_guessing():
    with tempfile.TemporaryDirectory() as tmp:
        row = _zerion(T0)
        row["trade_timestamp"] = None
        zl.write_archive(_fetch_result([row], [], fetched_at=T0), tmp)
        loaded = next(iter(zh.load_zerion_history(tmp).values()))[0]
        assert loaded["trade_time_known"] is False
        assert loaded["trade_timestamp"] is None


def test_the_loader_survives_a_corrupt_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        zl.write_archive(_fetch_result([_zerion(T0)], [], fetched_at=T0), tmp)
        (Path(tmp) / "garbage.json").write_text("{not json", encoding="utf-8")
        history = zh.load_zerion_history(tmp)
        assert len(next(iter(history.values()))) == 1


def test_the_loader_on_a_missing_directory_is_empty_not_an_error():
    assert zh.load_zerion_history("wallet_archive/raw/zerion/does-not-exist") == {}


def test_a_round_trip_through_the_archive_preserves_the_merge_outcome():
    """fetch -> archive -> load -> merge must be the same as fetch -> merge."""
    gmgn = [_gmgn(T0, trade_ts="none"), _gmgn(T0 + DAY, side="sell", trade_ts="none")]
    zerion_rows = [_zerion(T0), _zerion(T0 + DAY, side="sell", hashed="0xsell")]
    direct = zh.merge_sources({WALLET: gmgn}, {WALLET: zerion_rows})
    with tempfile.TemporaryDirectory() as tmp:
        zl.write_archive(_fetch_result(zerion_rows, [], fetched_at=T0), tmp)
        loaded = zh.load_zerion_history(tmp)
    assert len(direct[WALLET]) == 2
    assert len(loaded[WALLET]) == 2
    round_trip = zh.merge_sources({WALLET: gmgn}, loaded)
    assert len(round_trip[WALLET]) == 2
    assert whv.event_hash(round_trip[WALLET][0]) == "0xzonchain"


# --------------------------------------------------------------------------
# 7. This adapter cannot reach scoring
# --------------------------------------------------------------------------
def test_no_scoring_or_trading_module_imports_this_adapter():
    """The adapter is storage-only; wiring it into a decision path is a
    separate, explicit change."""
    import ast
    for name in ("scanner.py", "confluence_engine.py", "risk_engine.py",
                 "trade_readiness.py", "paper_trading.py",
                 "wallet_quality_engine.py", "wallet_signal_profiles.py",
                 "wallet_intel_gate.py", "wallet_conviction_signals"):
        path = Path(name)
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "zerion_history" for a in node.names), name
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "zerion_history", name


def test_this_module_ships_no_order_path():
    """No execution surface: no orders, no account, no live market calls."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(zh))
    code_only = [
        node for node in ast.walk(tree)
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Expr, ast.Constant, ast.Assign,
                                 ast.AnnAssign, ast.arg))
    ]
    names = {getattr(n, "id", None) for n in code_only}
    attrs = {n.attr for n in code_only if isinstance(n, ast.Attribute)}
    for banned in ("create_order", "place_order", "submit_order", "create_market_order",
                   "ccxt", "watch_ticker", "fetch_ticker", "orders_enabled",
                   "api_secret", "private_key", "score_wallet", "apply_score"):
        assert banned not in names and banned not in attrs, banned
    # Nothing in the module may be marked as a decision input.
    assert not hasattr(zh, "score")
    assert not hasattr(zh, "is_tradeable")
    assert not hasattr(zh, "run")


def test_this_module_does_not_reimplement_the_identity_helpers():
    """It must compose whv's helpers, not carry a second copy of the rule."""
    import inspect
    source = inspect.getsource(zh)
    for name in ("same_event", "event_core", "event_hash", "has_trade_timestamp",
                 "event_usd", "event_price", "event_side", "event_ts",
                 "asset_identity", "wallet_identity"):
        assert f"def {name}(" not in source, f"{name} was reimplemented"
    assert "whv.same_event" in source
    assert "whv.event_core" in source
    assert "whv.event_hash" in source


def test_the_only_local_identity_extension_delegates_to_the_shared_rule():
    """The chain allowance must start from the shared rule, never replace it."""
    import inspect
    assert "whv.same_event(row, other)" in inspect.getsource(zh.same_event_tolerant)
    # A hash is mandatory; without one the extension must not fire.
    row = _gmgn(T0, hashed="0xaaa", trade_ts="none", chain="")
    other = dict(row, chain="eth", transaction_hash="0xbbb")
    assert zh.same_event_tolerant(row, other) is False
    assert zh.same_event_tolerant(row, dict(row, chain="eth")) is True


def test_a_real_chain_disagreement_is_never_overruled_by_a_matching_hash():
    """Two chains in one transaction are two transfers, not one event."""
    left = _gmgn(T0, hashed="0xsame", trade_ts="keep", chain="eth")
    right = dict(left, chain="sol")
    assert whv.same_event(left, right) is False
    assert zh.same_event_tolerant(left, right) is False
    # an absent chain is tolerated, a contradicting one is not
    assert zh.same_event_tolerant(left, dict(left, chain="")) is True


def test_a_hashless_row_that_knows_its_trade_time_is_still_matched():
    """Regression: a hashless row with a real trade time and no chain label.

    It missed both rules. The legacy rule needs no trade time, and the hash rule
    needs equal hashes, while a Zerion row always names a real chain so the two
    cores could never be equal. Every such row became a duplicate.
    """
    gmgn = _gmgn(T0, hashed=None, trade_ts="keep", chain="")
    assert whv.event_hash(gmgn) == ""
    assert whv.has_trade_timestamp(gmgn) is True
    merged = zh.merge_sources({WALLET: [gmgn]}, {WALLET: [_zerion(T0)]})
    assert len(merged[WALLET]) == 1
    assert zh.provenance_of(merged[WALLET][0]) == ["gmgn", "zerion"]


def test_ingestion_is_idempotent_over_the_whole_committed_dataset():
    """Re-running a full backfill must not grow the history, ever.

    This is the invariant a batch job depends on, and it is the one that caught
    the missing rule above.
    """
    stored = json.loads(Path("gmgn_wallet_history.json").read_text(encoding="utf-8"))
    gmgn = {w: [r for r in rows if isinstance(r, dict)]
            for w, rows in stored.items() if isinstance(rows, list)}
    zerion = {}
    for wallet, rows in gmgn.items():
        built = []
        for row in rows:
            identity = whv.asset_identity(row)
            if identity is None:
                continue
            built.append({
                "source": "zerion", "timestamp": whv.event_ts(row) + 30,
                "trade_timestamp": whv.event_ts(row),
                # a real Zerion row always carries a hash and a real chain
                "transaction_hash": whv.event_hash(row) or f"0x{wallet[-6:]:0<58}",
                "chain": identity[0] or "eth", "address": identity[1],
                "symbol": row.get("symbol", ""), "side": whv.event_side(row),
                "amount_usd": whv.event_usd(row), "price_usd": whv.event_price(row),
            })
        if identity:
            built.append({
                "source": "zerion", "timestamp": T0, "trade_timestamp": T0,
                "transaction_hash": f"0xnew{wallet[-6:]:0<60}",
                "chain": identity[0] or "eth", "address": identity[1],
                "symbol": "NEWCOIN", "side": "buy",
                "amount_usd": 1234.0, "price_usd": 1.23,
            })
        zerion[wallet] = built

    before = sum(len(v) for v in gmgn.values())
    merged = zh.merge_sources(gmgn, zerion)
    after = sum(len(v) for v in merged.values())
    # exactly one new event per wallet, nothing else
    assert after == before + len(zerion), f"{after - before - len(zerion)} extra rows"
    for _ in range(2):
        again = zh.merge_sources(merged, zerion)
        assert sum(len(v) for v in again.values()) == after, "re-ingestion grew"
        merged = again
    # and the stored GMGN dedupe count never drops
    assert sum(len(whv.dedupe(v)) for v in merged.values()) >= \
        sum(len(whv.dedupe(v)) for v in gmgn.values())


def test_a_hashed_row_with_no_chain_does_not_become_a_second_trade():
    """The regression this tolerance exists to prevent."""
    gmgn = [_gmgn(T0, hashed="0xreal", trade_ts="keep", chain="")]
    zerion = _zerion(T0, hashed="0xreal", chain="eth")
    merged = zh.merge_sources({WALLET: gmgn}, {WALLET: [zerion]})
    assert len(merged[WALLET]) == 1
    assert zh.provenance_of(merged[WALLET][0]) == ["gmgn", "zerion"]
    # the stored row keeps its own value
    assert merged[WALLET][0]["chain"] == ""


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"Zerion history adapter tests: PASS ({len(TESTS)} tests)")
