"""Tests for the historical wallet discovery foundation.

These tests pin the properties the architecture depends on, not the contents of
any committed dataset. Every fixture is hand-written.

The two properties that matter most, and that the rest of this file exists to
protect:

1. Adding a second provider must never inflate a count. A Zerion event that
   describes a trade GMGN already holds corroborates it; it does not become a
   second trade. Re-ingesting the same feed must change nothing.
2. PROVEN stays a statement about measured pre-pump evidence. A wallet with
   enormous volume and no target reached is not proven, and a wallet that
   disappears from the current feed keeps its record instead of being deleted.
"""
import ast
import json
import tempfile
import unittest
from pathlib import Path

import historical_discovery as hd
import proven_wallet_registry as pwr
import wallet_history_validation as whv
import wallet_quality_engine as wqe
import zerion_history as zh

T0 = 1_700_000_000
DAY = 86400
WALLET = "0x3d457d0b79efac77ed38f37870c713d0244479ea"
WALLET_B = "0xaa11a401d7a41b15f25c95224bc87d9a85852b0a1"
ADDR = "0x4e67db19044549ff420860834c91b45bad298722"
ADDR_B = "0x00000000000000000000000000000000000000b1"


def gmgn(ts, side="buy", price=2.0, usd=20000.0, address=ADDR, chain="eth",
         symbol="AAA", hashed=None, trade_ts=True):
    """A GMGN-shaped row, as gmgn_layer would emit it."""
    row = {
        "source": "gmgn",
        "wallet": WALLET,
        "chain": chain,
        "address": address,
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "price_usd": price,
        "timestamp": ts + 900,
    }
    if trade_ts:
        row["trade_timestamp"] = ts
    if hashed:
        row["transaction_hash"] = hashed
    return row


def zerion(ts, side="buy", price=2.0, usd=20000.0, address=ADDR, chain="eth",
           symbol="AAA", hashed=None):
    """A Zerion-shaped row, as zerion_layer would emit it."""
    row = {
        "source": "zerion",
        "wallet": WALLET,
        "chain": chain,
        "address": address,
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "price_usd": price,
        "timestamp": ts + 930,
        "trade_timestamp": ts,
    }
    if hashed:
        row["transaction_hash"] = hashed
    return row


def nansen(ts, address=ADDR, chain="ethereum", balance=100.0, price=2.0):
    """A Nansen-shaped historical balance record."""
    return {
        "wallet": WALLET,
        "chain": chain,
        "token_address": address,
        "token_symbol": "AAA",
        "balance": balance,
        "token_price": price,
        "balance_value": round(balance * price, 2),
        "timestamp": ts,
    }


def hyperliquid(ts, coin="BTC", side="B", px=60000.0, sz=0.5, tid=1, h="0xhl1"):
    """A Hyperliquid-shaped fill, per the future schema."""
    return {
        "wallet": WALLET,
        "coin": coin,
        "side": side,
        "px": px,
        "sz": sz,
        "time": ts,
        "hash": h,
        "tid": tid,
    }


def proven_history(wallet=WALLET, base=T0, tag="a"):
    """Three entries that each clear the 2x target: a genuinely PROVEN wallet."""
    rows = []
    for i in range(3):
        ts = base + i * 30 * DAY
        address = f"0xab{i:038x}"
        rows.append({
            "source": "gmgn", "wallet": wallet, "chain": "eth", "address": address,
            "symbol": "AAA", "side": "buy", "amount_usd": 20000.0, "price_usd": 2.0,
            "timestamp": ts + 900, "trade_timestamp": ts,
            "transaction_hash": f"0x{tag}{i}",
        })
        rows.append({
            "source": "gmgn", "wallet": wallet, "chain": "eth", "address": address,
            "symbol": "AAA", "side": "sell", "amount_usd": 40000.0, "price_usd": 8.0,
            "timestamp": ts + DAY + 900, "trade_timestamp": ts + DAY,
            "transaction_hash": f"0x{tag}x{i}",
        })
    return hd.normalize_rows(rows, "gmgn", wallet=wallet)


class TestChainCanonicalization(unittest.TestCase):
    """Providers disagree on chain names; the repository does not."""

    def test_nansen_ethereum_maps_to_repo_eth(self):
        self.assertEqual(hd.canonical_chain("ethereum"), "eth")
        self.assertEqual(hd.canonical_chain("Ethereum"), "eth")

    def test_aliases_for_the_known_universe(self):
        for raw in ("binance-smart-chain", "bnb", "bsc-mainnet"):
            self.assertEqual(hd.canonical_chain(raw), "bsc")
        for raw in ("solana", "sol", "spl"):
            self.assertEqual(hd.canonical_chain(raw), "sol")
        self.assertEqual(hd.canonical_chain("hyperevm"), "hyperliquid")

    def test_unknown_chain_is_kept_not_guessed(self):
        # An unmapped chain stays visible rather than being folded into a
        # neighbouring one, which would merge two different assets.
        self.assertEqual(hd.canonical_chain("polygon"), "polygon")

    def test_absent_chain_stays_empty(self):
        # 73% of stored GMGN rows carry no chain. Inventing one would attribute
        # history the data does not support.
        self.assertEqual(hd.canonical_chain(None), "")
        self.assertEqual(hd.canonical_chain(""), "")

    def test_one_token_is_one_identity_across_providers(self):
        """The bug this guards: 'ethereum' and 'eth' becoming two assets."""
        g = hd.normalize_observation(gmgn(T0), "gmgn")
        n = hd.normalize_observation(nansen(T0), "nansen")
        self.assertEqual(whv.asset_identity(g), whv.asset_identity(n))


class TestSourceNeutralNormalization(unittest.TestCase):
    def test_every_source_yields_the_same_shape(self):
        rows = {
            "gmgn": gmgn(T0, hashed="0xg1"),
            "zerion": zerion(T0, hashed="0xz1"),
            "nansen": nansen(T0),
            "hyperliquid": hyperliquid(T0),
        }
        required = {
            "wallet", "chain", "address", "symbol", "side", "kind",
            "timestamp", "trade_timestamp", "trade_time_known",
            "amount_usd", "price_usd", "transaction_hash", "raw",
            "source", "sources", "source_record_id", "confidence",
        }
        for source, row in rows.items():
            with self.subTest(source=source):
                out = hd.normalize_observation(row, source)
                self.assertTrue(required.issubset(out), required - set(out))
                self.assertEqual(out["wallet"], WALLET)
                # The provider's own payload survives untouched.
                self.assertEqual(out["raw"], row)

    def test_nansen_snapshot_is_not_a_trade(self):
        """A balance is not an execution. Side and trade time must stay null."""
        out = hd.normalize_observation(nansen(T0), "nansen")
        self.assertEqual(out["kind"], hd.KIND_BALANCE_SNAPSHOT)
        self.assertIsNone(out["side"])
        self.assertIsNone(out["trade_timestamp"])
        self.assertIsNone(out["transaction_hash"])
        self.assertFalse(out["trade_time_known"])
        self.assertEqual(out["amount_usd"], 200.0)

    def test_nansen_balance_does_not_inflate_trade_counts(self):
        snapshots = [nansen(T0 + i * DAY) for i in range(20)]
        rec = hd.evaluate_wallet(WALLET, hd.normalize_rows(snapshots, "nansen"))
        self.assertEqual(rec["trades"], 0)
        self.assertEqual(rec["qualified_trades"], 0)
        self.assertEqual(rec["discovery_tier"], hd.TIER_ACTIVITY)
        self.assertFalse(rec["proven"])

    def test_hyperliquid_normalizes_to_the_shared_shape(self):
        out = hd.normalize_observation(hyperliquid(T0), "hyperliquid")
        self.assertEqual(out["kind"], hd.KIND_TRADE)
        self.assertEqual(out["side"], "buy")          # B = buy
        self.assertEqual(out["trade_timestamp"], T0)  # ms? no, seconds here
        self.assertEqual(out["transaction_hash"], "0xhl1")
        self.assertEqual(out["address"], "BTC", "the perp asset is the observation's address slot")
        # No usd in the payload, and that is reported rather than invented.
        self.assertIsNone(out["amount_usd"])
        self.assertIn("no_usd_value", out["confidence"]["reasons"])

    def test_hyperliquid_sell_side(self):
        out = hd.normalize_observation(hyperliquid(T0, side="A"), "hyperliquid")
        self.assertEqual(out["side"], "sell")

    def test_observations_are_usable_by_the_existing_identity_layer(self):
        """Discovery must not require a parallel identity implementation."""
        out = hd.normalize_observation(gmgn(T0, hashed="0xg1"), "gmgn")
        self.assertIsNotNone(whv.event_core(out))
        self.assertEqual(whv.event_ts(out), T0)
        self.assertTrue(whv.same_event(gmgn(T0, hashed="0xg1"), out))

    def test_trade_time_known_is_a_bool_not_a_tuple(self):
        for source, row in (
            ("gmgn", gmgn(T0, hashed="0xg1")),
            ("zerion", zerion(T0, hashed="0xz1")),
            ("hyperliquid", hyperliquid(T0)),
            ("nansen", nansen(T0)),
        ):
            with self.subTest(source=source):
                out = hd.normalize_observation(row, source)
                self.assertIsInstance(out["trade_time_known"], bool)


class TestMultiSourceMerge(unittest.TestCase):
    def setUp(self):
        self.gmgn_rows = hd.normalize_rows(
            [gmgn(T0, hashed=None), gmgn(T0 + DAY, side="sell", hashed=None),
             gmgn(T0 + 2 * DAY, hashed="0xthird")],
            "gmgn", wallet=WALLET,
        )
        self.zerion_rows = hd.normalize_rows(
            [zerion(T0, hashed="0xz1"), zerion(T0 + DAY, side="sell", hashed="0xz2")],
            "zerion", wallet=WALLET,
        )
        self.base = {WALLET: self.gmgn_rows}

    def test_a_second_provider_corroborates_instead_of_inflating(self):
        merged = hd.merge_sources(self.base, {WALLET: self.zerion_rows})[WALLET]
        self.assertEqual(len(self.gmgn_rows), 3)
        self.assertEqual(len(self.zerion_rows), 2)
        self.assertEqual(len(merged), 3, "two providers, three real events")

    def test_provenance_records_both_providers(self):
        merged = hd.merge_sources(self.base, {WALLET: self.zerion_rows})[WALLET]
        corroborated = [r for r in merged if set(zh.provenance_of(r)) >= {"gmgn", "zerion"}]
        self.assertEqual(len(corroborated), 2)
        self.assertTrue(all("gmgn" in zh.provenance_of(r) for r in corroborated))
        self.assertTrue(all(r.get("corroborated_by") for r in corroborated))

    def test_repeated_ingestion_is_idempotent(self):
        once = hd.merge_sources(self.base, {WALLET: self.zerion_rows})
        twice = hd.merge_sources(once, {WALLET: self.zerion_rows})
        self.assertEqual(len(twice[WALLET]), 3)
        third = hd.merge_sources(twice, {WALLET: self.zerion_rows + self.zerion_rows})
        self.assertEqual(len(third[WALLET]), 3)

    def test_a_genuinely_new_event_is_still_added(self):
        new = hd.normalize_rows(
            [zerion(T0 + 3 * DAY, hashed="0xbrandnew")], "zerion", wallet=WALLET
        )
        merged = hd.merge_sources(
            hd.merge_sources(self.base, {WALLET: self.zerion_rows}), {WALLET: new}
        )
        self.assertEqual(len(merged[WALLET]), 4)

    def test_legacy_hashless_gmgn_rows_are_never_dropped(self):
        merged = hd.merge_sources(self.base, {WALLET: self.zerion_rows})[WALLET]
        self.assertEqual(len(merged), len(self.gmgn_rows))

    def test_merge_is_additive_against_the_live_dataset(self):
        """The committed GMGN file must survive a Zerion overlay unchanged in
        size: a provider conflict may add evidence, never remove a row."""
        if not Path("gmgn_wallet_history.json").is_file():
            self.skipTest("gmgn_wallet_history.json not present")
        raw = json.loads(Path("gmgn_wallet_history.json").read_text(encoding="utf-8"))
        wallets = raw.get("wallets", raw)  # flat wallet -> rows
        wallet = next(w for w, rows in wallets.items() if rows)
        base = {w: hd.normalize_rows(rows, "gmgn", wallet=w)
                for w, rows in list(wallets.items())[:40]}
        before = sum(len(v) for v in base.values())
        overlay = {w: hd.normalize_rows(rows[:2], "zerion", wallet=w)
                   for w, rows in list(wallets.items())[:40]}
        after = sum(len(v) for v in hd.merge_sources(base, overlay).values())
        self.assertGreaterEqual(after, before)


class TestDiscoveryTiers(unittest.TestCase):
    """Activity, candidate, qualified and proven are four different claims."""

    def test_high_volume_without_the_target_is_not_proven(self):
        """Volume is not skill. 100 x $20k buys that never double qualify as
        candidates and are explicitly not proven."""
        rows = [gmgn(T0 + i * 60, address=f"0x{i:040x}") for i in range(100)]
        rec = hd.evaluate_wallet(WALLET, hd.normalize_rows(rows, "gmgn", wallet=WALLET))
        self.assertEqual(rec["trades"], 100)
        self.assertEqual(rec["qualified_trades"], 100)
        self.assertFalse(rec["proven"])
        self.assertEqual(rec["discovery_tier"], hd.TIER_QUALIFIED)

    def test_large_buys_on_one_asset_are_not_proven(self):
        rows = [gmgn(T0 + i * 3600, usd=500000.0) for i in range(50)]
        rec = hd.evaluate_wallet(WALLET, hd.normalize_rows(rows, "gmgn", wallet=WALLET))
        self.assertFalse(rec["proven"])
        self.assertEqual(rec["assets_traded"], 1)

    def test_thin_activity_is_not_a_candidate(self):
        rows = [gmgn(T0, usd=100.0, address=f"0x{i:040x}") for i in range(2)]
        rec = hd.evaluate_wallet(WALLET, hd.normalize_rows(rows, "gmgn", wallet=WALLET))
        self.assertEqual(rec["discovery_tier"], hd.TIER_ACTIVITY)
        self.assertEqual(rec["qualified_trades"], 0)

    def test_real_pre_pump_evidence_is_proven(self):
        rec = hd.evaluate_wallet(WALLET, proven_history())
        self.assertTrue(rec["proven"])
        self.assertEqual(rec["discovery_tier"], hd.TIER_PROVEN)
        self.assertEqual(rec["proven_status"], whv.PROVEN)

    def test_proven_requires_the_existing_criteria_unchanged(self):
        self.assertEqual(whv.MIN_OBSERVED_ENTRIES, 3)
        self.assertEqual(whv.MIN_SUCCESSFUL_ENTRIES, 2)
        self.assertEqual(whv.MIN_WIN_RATE_PCT, 60.0)
        self.assertEqual(whv.TARGET_MULTIPLE, 2.0)

    def test_two_good_entries_are_not_enough(self):
        """MIN_SUCCESSFUL_ENTRIES is 2 but MIN_OBSERVED_ENTRIES is 3; the
        stricter one must win."""
        rows = []
        for i in range(2):
            ts = T0 + i * 30 * DAY
            address = f"0xcd{i:038x}"
            rows += [
                gmgn(ts, address=address, hashed=f"0x{i}"),
                gmgn(ts + DAY, side="sell", price=8.0, address=address, hashed=f"0xx{i}"),
            ]
        rec = hd.evaluate_wallet(WALLET, hd.normalize_rows(rows, "gmgn", wallet=WALLET))
        self.assertFalse(rec["proven"])

    def test_candidate_ordering_is_stable_and_evidence_first(self):
        history = {WALLET: proven_history()}
        other = hd.normalize_rows(
            [gmgn(T0 + i * DAY, address=f"0x{i:040x}") for i in range(10)],
            "gmgn", wallet=WALLET_B,
        )
        history[WALLET_B] = other
        first = [r["wallet"] for r in hd.discover_candidates(history)]
        second = [r["wallet"] for r in hd.discover_candidates(history)]
        self.assertEqual(first, second)
        self.assertEqual(first[0], WALLET, "PROVEN should lead the queue")
        self.assertNotIn("__missing__", first)

    def test_summary_counts_do_not_double_count(self):
        history = {WALLET: proven_history(), WALLET_B: hd.normalize_rows(
            [gmgn(T0, address=f"0x{i:040x}") for i in range(5)], "gmgn", wallet=WALLET_B)}
        summary = hd.discovery_summary(history)
        self.assertEqual(summary["wallets"], 2)
        self.assertEqual(summary["wallets_by_tier"][hd.TIER_PROVEN], 1)
        self.assertEqual(sum(summary["wallets_by_tier"].values()), 2)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.history = {WALLET: proven_history(WALLET, T0, "aa"),
                        WALLET_B: proven_history(WALLET_B, T0 + 10 * DAY, "bb")}

    def test_entry_has_every_field(self):
        entry = pwr.build_entry(WALLET, self.history[WALLET], active_now=True)
        for field in pwr.REGISTRY_FIELDS:
            self.assertIn(field, entry)
        self.assertEqual(pwr.validate_entry(entry), [])

    def test_proven_status_is_copied_not_decided(self):
        entry = pwr.build_entry(WALLET, self.history[WALLET], active_now=True)
        direct = whv.reconstruct_wallet(WALLET, self.history[WALLET])
        self.assertEqual(entry["proven_status"], direct["history_class"])
        self.assertEqual(entry["proven_status"], whv.PROVEN)

    def test_unverified_wallet_is_recorded_as_unproven(self):
        thin = hd.normalize_rows(
            [gmgn(T0, address=f"0x{i:040x}") for i in range(3)], "gmgn", wallet=WALLET_B
        )
        entry = pwr.build_entry(WALLET_B, thin, active_now=True)
        self.assertNotEqual(entry["proven_status"], whv.PROVEN)
        self.assertEqual(entry["qualified_trades"], 3)

    def test_wallet_survives_leaving_the_current_feed(self):
        first = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        self.assertEqual(first["wallet_count"], 2)
        # Next run only one wallet is visible in the live feed.
        second = pwr.merge_registry(
            first, pwr.build_registry(self.history, current_wallets=[WALLET])
        )
        self.assertEqual(second["wallet_count"], 2, "a quiet wallet must not be deleted")
        self.assertIn(WALLET_B, second["wallets"])
        self.assertFalse(second["wallets"][WALLET_B]["active_now"])
        self.assertEqual(second["wallets"][WALLET_B]["proven_status"], whv.PROVEN)
        self.assertTrue(second["wallets"][WALLET]["active_now"])

    def test_wallet_survives_vanishing_from_history_entirely(self):
        first = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        trimmed = pwr.build_registry({WALLET: self.history[WALLET]}, current_wallets=[WALLET])
        merged = pwr.merge_registry(first, trimmed)
        self.assertIn(WALLET_B, merged["wallets"])
        self.assertEqual(merged["proven_count"], 2)

    def test_a_stale_active_flag_cannot_resurrect_a_wallet(self):
        first = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        merged = pwr.merge_registry(
            first, pwr.build_registry({WALLET: self.history[WALLET]}, current_wallets=[WALLET])
        )
        self.assertIs(merged["wallets"][WALLET_B]["active_now"], False)

    def test_active_now_is_unknown_without_a_feed(self):
        registry = pwr.build_registry(self.history)
        self.assertIsNone(registry["wallets"][WALLET]["active_now"])

    def test_sparse_reread_does_not_blank_a_known_score(self):
        first = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        keep = first["wallets"][WALLET]["historical_quality_score"]
        sparse = pwr.build_registry({WALLET: []}, current_wallets=[WALLET])
        merged = pwr.merge_registry(first, sparse)
        self.assertEqual(merged["wallets"][WALLET]["historical_quality_score"], keep)

    def test_unmeasurable_metrics_are_none_not_zero(self):
        thin = hd.normalize_rows([nansen(T0)], "nansen", wallet=WALLET_B)
        entry = pwr.build_entry(WALLET_B, thin, active_now=False)
        self.assertIsNone(entry["drawdown"], "no equity curve exists to measure")
        self.assertEqual(entry["historical_trades"], 0, "a snapshot is not a trade")
        self.assertIsNone(entry["win_rate"], "nothing was observed to win or lose")
        self.assertIn("drawdown", entry["provenance"]["unknown_metrics"])

    def test_realized_win_rate_needs_every_entry_closed(self):
        open_rows = []
        for i in range(3):
            ts = T0 + i * 30 * DAY
            open_rows.append(gmgn(ts, address=f"0xef{i:038x}", hashed=f"0x{i}"))
        entry = pwr.build_entry(WALLET, hd.normalize_rows(open_rows, "gmgn", wallet=WALLET))
        self.assertIsNone(entry["realized_win_rate"], "unclosed entries would flatter it")

    def test_mae_is_caveated_as_not_drawdown(self):
        entry = pwr.build_entry(WALLET, self.history[WALLET], active_now=True)
        caveat = entry["provenance"]["metric_caveats"]["mae_pct"]
        self.assertIn("not as downside risk", caveat.replace("never as", "not as"))
        # Coverage is reported so the reader can judge the number.
        evidence = entry["provenance"]["evidence"]
        self.assertEqual(evidence["mae_sample"], evidence["reconstructed_entries"])

    def test_registry_never_enables_execution(self):
        registry = pwr.build_registry(self.history, current_wallets=[WALLET])
        self.assertFalse(registry["orders_enabled"])
        merged = pwr.merge_registry(registry, pwr.build_registry({}))
        self.assertFalse(merged["orders_enabled"])
        tampered = dict(registry, orders_enabled=True)
        self.assertTrue(pwr.validate_registry(tampered))

    def test_round_trips_through_disk(self):
        registry = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            pwr.save_registry(registry, path)
            loaded = pwr.load_registry(path)
        self.assertEqual(loaded["wallets"].keys(), registry["wallets"].keys())
        self.assertEqual(loaded["wallet_count"], 2)
        self.assertEqual(pwr.validate_registry(loaded), [])

    def test_load_of_a_missing_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pwr.load_registry(Path(tmp) / "nope.json"), {})

    def test_proven_count_matches_the_entries(self):
        registry = pwr.build_registry(self.history, current_wallets=[WALLET, WALLET_B])
        counted = sum(1 for e in registry["wallets"].values()
                      if e["proven_status"] == whv.PROVEN)
        self.assertEqual(registry["proven_count"], counted)

    def test_registry_reuses_the_engine_scores_verbatim(self):
        """The registry must report the engine's published numbers, not a
        reimplementation of them."""
        rows = [gmgn(T0 + i * 4 * DAY, address=f"0x11{i:038x}", hashed=f"0x{i}")
                for i in range(6)]
        rows += [gmgn(T0 + i * 4 * DAY + DAY, side="sell", price=6.0,
                      address=f"0x11{i:038x}", hashed=f"0xs{i}") for i in range(6)]
        normalized = hd.normalize_rows(rows, "gmgn", wallet=WALLET)
        entry = pwr.build_entry(WALLET, normalized, active_now=True)
        profile = wqe.build_profiles({WALLET: normalized}).get(WALLET)
        self.assertIsNotNone(profile)
        self.assertEqual(entry["historical_quality_score"], profile["quality_score"])
        self.assertEqual(entry["pre_pump_rate"], profile["pre_pump_24h_10pct_rate"])
        self.assertEqual(entry["first_entry_rate"], profile["pre_pump_first_entry_rate"])


class TestArchitectureBoundaries(unittest.TestCase):
    """Static guarantees. These are the constraints a future edit could break
    silently, so they are checked by reading the source rather than by running
    it."""

    PROTECTED = (
        "scanner.py",
        "wallet_quality_engine.py",
        "confluence_engine.py",
        "risk_engine.py",
        "trade_readiness.py",
        "paper_trading.py",
        "wallet_intel_gate.py",
    )

    @staticmethod
    def _imports(path):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        return {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }

    def test_no_protected_module_imports_discovery_or_zerion(self):
        for name in self.PROTECTED:
            path = Path(name)
            if not path.is_file():
                continue
            with self.subTest(module=name):
                imports = self._imports(name)
                self.assertNotIn("historical_discovery", imports)
                self.assertNotIn("proven_wallet_registry", imports)
                self.assertNotIn("zerion_history", imports)
                self.assertNotIn("zerion_layer", imports)

    def test_discovery_does_not_import_execution_or_scoring_paths(self):
        imports = self._imports("historical_discovery.py")
        for forbidden in ("paper_trading", "confluence_engine", "trade_readiness",
                          "risk_engine", "scanner", "gmgn_layer", "zerion_layer"):
            self.assertNotIn(forbidden, imports, f"{forbidden} must stay out of discovery")

    def test_registry_does_not_import_execution_paths(self):
        imports = self._imports("proven_wallet_registry.py")
        for forbidden in ("paper_trading", "confluence_engine", "trade_readiness",
                          "risk_engine", "scanner"):
            self.assertNotIn(forbidden, imports, f"{forbidden} must stay out of the registry")

    def test_registry_reuses_the_existing_proven_classifier(self):
        self.assertIn("wallet_history_validation", self._imports("proven_wallet_registry.py"))

    def test_no_module_enables_live_execution(self):
        for name in ("historical_discovery.py", "proven_wallet_registry.py"):
            with self.subTest(module=name):
                source = Path(name).read_text(encoding="utf-8")
                self.assertNotIn("create_order", source)
                self.assertNotIn("place_order", source)
                self.assertNotIn("LIVE_TRADING", source)


class TestSourceReaders(unittest.TestCase):
    def test_nansen_reader_handles_chain_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical_balances" / "ethereum" / f"{WALLET}.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": 1, "source": "nansen", "wallet": WALLET,
                "chain": "ethereum", "coverage": "30d", "fetched_at": T0,
                "records": [nansen(T0 + i * DAY) for i in range(3)],
            }), encoding="utf-8")
            history = hd.read_nansen_archive(path)
        rows = history[WALLET]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["chain"] == "eth" for r in rows))
        self.assertTrue(all(r["kind"] == hd.KIND_BALANCE_SNAPSHOT for r in rows))
        self.assertTrue(all(r["wallet"] == WALLET for r in rows))

    def test_gmgn_reader_uses_the_stored_chain_and_wallet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gmgn_wallet_history.json"
            path.write_text(json.dumps({WALLET: [gmgn(T0, hashed="0xg1")]}), encoding="utf-8")
            history = hd.read_gmgn_history(path)
        rows = history[WALLET]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wallet"], WALLET)
        self.assertEqual(zh.provenance_of(rows[0]), ["gmgn"])

    def test_hyperliquid_reader_normalizes_without_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hyperliquid_fills.json"
            path.write_text(json.dumps({"fills": [hyperliquid(T0, tid=1),
                                                  hyperliquid(T0 + 60, side="A", tid=2,
                                                              h="0xhl2")]},
                                       ensure_ascii=False), encoding="utf-8")
            rows = hd.read_hyperliquid_rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["side"] for r in rows], ["buy", "sell"])
        self.assertTrue(all(r["kind"] == hd.KIND_TRADE for r in rows))

    def test_missing_archive_yields_nothing_rather_than_raising(self):
        self.assertEqual(hd.read_nansen_archive(Path("/nonexistent/x.json")), {})
        self.assertEqual(hd.read_gmgn_history(Path("/nonexistent/x.json")), {})
        self.assertEqual(hd.read_hyperliquid_rows(Path("/nonexistent/x.json")), [])

    def test_every_adapter_declares_a_kind_and_timestamp_source(self):
        for name, adapter in hd.ADAPTERS.items():
            with self.subTest(source=name):
                self.assertIn(adapter.kind, (hd.KIND_TRADE, hd.KIND_BALANCE_SNAPSHOT))
                self.assertTrue(adapter.trade_time or adapter.observed_time,
                                "an adapter must say where its timestamps come from")
                self.assertTrue(adapter.address, "an adapter must locate the asset")
                self.assertTrue(adapter.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
