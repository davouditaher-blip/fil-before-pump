#!/usr/bin/env python3
"""Focused tests for Reverse Historical Discovery.

Two jobs. First, prove the +20% logic actually works, on fixtures where a move is
known by construction -- otherwise "0 candidates" would be indistinguishable from
"broken". Second, assert the properties that must hold on the real artifact and
in the real run, including the ones that keep discovery from becoming conviction.
"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import historical_discovery as hd  # noqa: E402
import proven_wallet_registry as pwr  # noqa: E402
import reverse_historical_discovery as rhd  # noqa: E402
import wallet_history_validation as whv  # noqa: E402

ARTIFACT = HERE / "reverse_historical_discovery.json"
SOURCE = HERE / "gmgn_wallet_history.json"
REGISTRY = HERE / "proven_wallet_registry.json"
SOURCE_MD5 = "ef1e23b980ae3b4cb0ad239267cbbae4"
DAY = 86_400


def buy(wallet, address, ts, price, usd=1_000.0, chain="eth", symbol="TOK"):
    return {
        "wallet": wallet, "chain": chain, "address": address, "symbol": symbol,
        "side": "buy", "amount_usd": usd, "price_usd": price,
        "timestamp": ts, "trade_timestamp": ts,
    }


def sell(wallet, address, ts, price, usd=1_000.0, chain="eth", symbol="TOK"):
    row = buy(wallet, address, ts, price, usd, chain, symbol)
    row["side"] = "sell"
    return row


class TestMoveDetection(unittest.TestCase):
    def test_a_twenty_percent_rise_is_detected(self):
        series = [(1_000, 1.00), (2_000, 1.10), (3_000, 1.20)]
        moves = rhd.find_moves(series)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["move_start_ts"], 1_000)
        self.assertEqual(moves[0]["confirmation_ts"], 3_000)
        self.assertEqual(moves[0]["confirmation_price"], 1.20)
        self.assertAlmostEqual(moves[0]["price_change_pct"], 20.0, places=4)

    def test_exactly_twenty_percent_counts_and_just_under_does_not(self):
        self.assertEqual(len(rhd.find_moves([(1, 1.0), (2, 1.2)])), 1)
        self.assertEqual(len(rhd.find_moves([(1, 1.0), (2, 1.1999)])), 0)

    def test_a_decline_is_not_a_move(self):
        self.assertEqual(rhd.find_moves([(1, 2.0), (2, 1.0), (3, 0.5)]), [])

    def test_a_move_needs_a_later_observation(self):
        # A single high point with nothing after it cannot be confirmed.
        self.assertEqual(rhd.find_moves([(1, 1.0)]), [])

    def test_one_continuous_rise_is_one_event_not_one_per_step(self):
        # 1.00 -> 1.10 -> 1.20 -> 1.30 -> 1.40 is a single continuous rise and
        # must collapse to one event, not to four overlapping +20% claims.
        series = [(i * 1_000, price) for i, price in enumerate([1.0, 1.1, 1.2, 1.3, 1.4])]
        moves = rhd.find_moves(series)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["confirmation_ts"], 2_000)

    def test_two_separated_moves_are_two_events(self):
        # A rise, a fall back below the new start, then a second rise.
        series = [
            (1_000, 1.0), (2_000, 1.5),          # move 1 confirmed at 2_000
            (3_000, 1.0), (4_000, 1.4),          # move 2 confirmed at 4_000
        ]
        moves = rhd.find_moves(series)
        self.assertEqual(len(moves), 2)
        self.assertEqual([m["confirmation_ts"] for m in moves], [2_000, 4_000])

    def test_detection_is_deterministic(self):
        series = [(i * 100, 1.0 + (i % 5) * 0.3) for i in range(40)]
        self.assertEqual(rhd.find_moves(series), rhd.find_moves(series))


class TestPreMoveEntryOrdering(unittest.TestCase):
    def test_an_entry_before_the_move_is_captured(self):
        move = {"move_start_ts": 1_000, "confirmation_ts": 5_000,
                "move_start_price": 1.0, "confirmation_price": 1.25}
        entries = [{"wallet": "0xa", "ts": 2_000, "price": 1.05}]
        hits = rhd.pre_move_entries(move, entries)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["wallet"], "0xa")

    def test_an_entry_at_the_move_start_counts(self):
        move = {"move_start_ts": 1_000, "confirmation_ts": 5_000}
        self.assertEqual(len(rhd.pre_move_entries(move, [{"wallet": "0xa", "ts": 1_000}])), 1)

    def test_an_entry_at_confirmation_is_excluded(self):
        # The look-ahead guard. Buying at the confirming price is already +20%;
        # calling that "pre-move" would reward hindsight.
        move = {"move_start_ts": 1_000, "confirmation_ts": 5_000}
        self.assertEqual(rhd.pre_move_entries(move, [{"wallet": "0xa", "ts": 5_000}]), [])

    def test_an_entry_after_confirmation_is_excluded(self):
        move = {"move_start_ts": 1_000, "confirmation_ts": 5_000}
        self.assertEqual(rhd.pre_move_entries(move, [{"wallet": "0xa", "ts": 9_000}]), [])

    def test_every_reported_event_strictly_precedes_its_confirmation(self):
        # The invariant stated once, checked on the real artifact too.
        artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        for wallet, entry in artifact["candidates"].items():
            for event in entry["events"]:
                self.assertTrue(event["entry_precedes_confirmation"])
                self.assertLess(event["entry_ts"], event["confirmation_ts"])
                self.assertGreaterEqual(event["entry_ts"], event["move_start_ts"])
                self.assertGreaterEqual(event["confirmation_price"],
                                        event["move_start_price"] * (1 + rhd.MOVE_THRESHOLD) - 1e-9)


class TestIndependentEventCounting(unittest.TestCase):
    def history_with_one_move(self, wallet, buys=3):
        """A wallet buying repeatedly into a single continuous rise.

        The rise must actually reach +20%, or there is no move to be early for
        and the test would pass for the wrong reason.
        """
        address = "AssetOne"
        rows = []
        for i in range(buys):
            rows.append(buy(wallet, address, 1_000 + i * 100, 1.0 + i * 0.10))
        return rows

    def test_repeated_buyers_in_one_move_count_as_one_event(self):
        history = {"0xaaa": self.history_with_one_move("0xaaa", buys=4)}
        observations = rhd.asset_observations(history)
        entries = rhd.collect_entries(history)
        events, _ = rhd.build_events(observations, entries, min_history_days=0)
        self.assertEqual(len(events), 1, "one move must not yield one event per buy")
        self.assertEqual(events[0]["wallet"], "0xaaa")

    def test_a_wallet_can_never_appear_twice_for_one_event_id(self):
        history = {"0xaaa": self.history_with_one_move("0xaaa", buys=6)}
        observations = rhd.asset_observations(history)
        entries = rhd.collect_entries(history)
        events, _ = rhd.build_events(observations, entries, min_history_days=0)
        pairs = [(e["wallet"], e["event_id"]) for e in events]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_distinct_moves_for_one_wallet_count_separately(self):
        address = "AssetTwo"
        rows = [
            buy("0xbbb", address, 1_000, 1.0), sell("0xbbb", address, 1_500, 1.4),
            buy("0xbbb", address, 10_000, 1.0), sell("0xbbb", address, 10_500, 1.4),
        ]
        history = {"0xbbb": rows}
        observations = rhd.asset_observations(history)
        entries = rhd.collect_entries(history)
        events, _ = rhd.build_events(observations, entries, min_history_days=0)
        wallets, stats = rhd.build_wallets(events, {})
        self.assertEqual(stats["wallets_with_repeated_independent_events"], 1)
        entry = wallets["0xbbb"]
        self.assertGreaterEqual(entry["independent_pre_pump_events"], 2)
        self.assertGreaterEqual(entry["distinct_assets"], 1)

    def test_two_assets_are_counted_as_two_distinct_assets(self):
        rows = [
            buy("0xccc", "A1", 1_000, 1.0), sell("0xccc", "A1", 1_500, 1.4),
            buy("0xccc", "B1", 10_000, 1.0), sell("0xccc", "B1", 10_500, 1.4),
        ]
        history = {"0xccc": rows}
        events, _ = rhd.build_events(rhd.asset_observations(history),
                                     rhd.collect_entries(history), min_history_days=0)
        wallets, _ = rhd.build_wallets(events, {})
        self.assertEqual(wallets["0xccc"]["distinct_assets"], 2)

    def test_identical_timestamp_price_observations_collapse(self):
        # Several wallets filling at one price in one second is one observation.
        history = {
            "0x1": [buy("0x1", "S", 1_000, 1.0)],
            "0x2": [buy("0x2", "S", 1_000, 1.0)],
            "0x3": [buy("0x3", "S", 1_000, 1.0)],
        }
        observations = rhd.asset_observations(history)
        series = list(observations.values())[0]
        self.assertEqual(len(series), 1)

    def test_a_single_success_does_not_make_a_repeated_wallet(self):
        history = {"0xddd": [
            buy("0xddd", "Solo", 1_000, 1.0), sell("0xddd", "Solo", 1_400, 1.3),
        ]}
        events, _ = rhd.build_events(rhd.asset_observations(history),
                                     rhd.collect_entries(history), min_history_days=0)
        wallets, stats = rhd.build_wallets(events, {})
        self.assertEqual(wallets["0xddd"]["independent_pre_pump_events"], 1)
        self.assertEqual(stats["wallets_with_repeated_independent_events"], 0)


class TestHistoryGateAndEmptyInput(unittest.TestCase):
    def test_the_gate_rejects_an_asset_with_insufficient_history(self):
        history = {"0xeee": [buy("0xeee", "G", 1_000, 1.0), sell("0xeee", "G", 2_000, 1.5)]}
        observations = rhd.asset_observations(history)
        entries = rhd.collect_entries(history)
        _, stats = rhd.build_events(observations, entries, min_history_days=90)
        self.assertEqual(stats["assets_eligible"], 0)
        self.assertEqual(stats["assets_rejected_short_history"], 1)
        self.assertEqual(stats["moves_found"], 0)

    def test_the_gate_admits_an_asset_with_enough_history(self):
        # Starts at DAY, not 0: a zero timestamp is treated as missing evidence,
        # so a 0 -> 100*DAY span would be rejected for the wrong reason.
        history = {"0xfff": [
            buy("0xfff", "L", DAY, 1.0), sell("0xfff", "L", 101 * DAY, 1.5),
        ]}
        _, stats = rhd.build_events(rhd.asset_observations(history),
                                   rhd.collect_entries(history), min_history_days=90)
        self.assertEqual(stats["assets_eligible"], 1)
        self.assertEqual(stats["moves_found"], 1)

    def test_empty_history_produces_an_empty_result(self):
        events, stats = rhd.build_events({}, {}, min_history_days=90)
        self.assertEqual(events, [])
        self.assertEqual(stats["assets_seen"], 0)
        wallets, wstats = rhd.build_wallets([], {})
        self.assertEqual(wallets, {})
        self.assertEqual(wstats["wallets_observed_before_moves"], 0)

    def test_rows_without_a_price_are_not_usable_evidence(self):
        history = {"0xg": [{"wallet": "0xg", "chain": "eth", "address": "N", "side": "buy",
                            "amount_usd": 100, "timestamp": 1_000}]}
        self.assertEqual(rhd.asset_observations(history), {})

    def test_rows_without_an_asset_address_are_not_attributed(self):
        history = {"0xh": [{"wallet": "0xh", "chain": "eth", "address": "", "side": "buy",
                            "price_usd": 1.0, "timestamp": 1_000}]}
        self.assertEqual(rhd.asset_observations(history), {})


class TestWalletIdentityAndProvenance(unittest.TestCase):
    def test_evm_wallet_identity_is_case_canonicalised(self):
        # A real 40-hex address. A short "0xAbCdEf" is not an EVM address, so
        # whv.wallet_identity deliberately leaves it alone and this would test
        # nothing.
        mixed = "0xAbCdEf0123456789aBcDeF0123456789aBcDeF01"
        history = {mixed: [buy(mixed, "I", 1_000, 1.0), sell(mixed, "I", 1_500, 1.4)]}
        events, _ = rhd.build_events(rhd.asset_observations(history),
                                     rhd.collect_entries(history), min_history_days=0)
        self.assertEqual([e["wallet"] for e in events], [mixed.lower()])

    def test_events_record_the_provenance_source(self):
        history = {"0xi": [buy("0xi", "P", 1_000, 1.0), sell("0xi", "P", 1_500, 1.4)]}
        events = rhd.attach_exits(
            rhd.build_events(rhd.asset_observations(history),
                             rhd.collect_entries(history), min_history_days=0)[0], history)
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["source"], hd.SOURCE_GMGN)
            self.assertTrue(event["event_id"])
            self.assertTrue(event["asset_address"])
            self.assertIn("exit_ts", event)

    def test_exit_is_recorded_only_when_the_archive_has_a_sell(self):
        history = {"0xj": [buy("0xj", "Q", 1_000, 1.0), sell("0xj", "Q", 1_400, 1.3)]}
        events = rhd.attach_exits(
            rhd.build_events(rhd.asset_observations(history),
                             rhd.collect_entries(history), min_history_days=0)[0], history)
        self.assertTrue(events)
        self.assertIsNotNone(events[0]["exit_ts"])

    def test_no_exit_means_null_never_an_invented_one(self):
        # The candidate only ever buys. Another wallet's trade supplies the price
        # that confirms the move, so the move exists but the candidate has no
        # recorded sell, and exit_ts must be null rather than invented. The sell
        # sits under the *other* wallet's key: a sell filed under the candidate's
        # key would be that candidate's own exit.
        history = {
            "0xk": [buy("0xk", "R", 1_000, 1.0)],
            "0xother": [sell("0xother", "R", 1_400, 1.3)],
        }
        events = rhd.attach_exits(
            rhd.build_events(rhd.asset_observations(history),
                             rhd.collect_entries(history), min_history_days=0)[0],
            history)
        self.assertTrue(events)
        self.assertEqual(events[0]["wallet"], "0xk")
        self.assertIsNone(events[0]["exit_ts"])


class TestDiscoveryNeverBecomesConviction(unittest.TestCase):
    def test_proven_status_is_copied_from_the_registry_and_never_written(self):
        before = pwr.load_registry(REGISTRY)
        status = rhd.load_proven_status()
        after = pwr.load_registry(REGISTRY)
        self.assertEqual(before, after, "loading PROVEN standing must not mutate the registry")
        self.assertEqual(len(status), before["wallet_count"])
        for wallet, value in status.items():
            self.assertIn(value, whv.CLASSES)

    def test_the_registry_artifact_is_never_opened_for_writing(self):
        source = Path("reverse_historical_discovery.py").read_text(encoding="utf-8")
        self.assertNotIn('REGISTRY.write_text', source)
        self.assertNotIn('save_registry', source)
        self.assertNotIn('pwr.save', source)

    def test_discovery_defines_no_proven_threshold_of_its_own(self):
        # Walk the AST rather than grepping source text. The module documents in
        # prose that these belong to the classifier and are deliberately unused,
        # so a substring search would match its own explanation of the rule it is
        # obeying. Names appearing in code are what would actually be a violation.
        forbidden = {"MIN_OBSERVED_ENTRIES", "MIN_SUCCESSFUL_ENTRIES",
                     "MIN_WIN_RATE_PCT", "TARGET_MULTIPLE"}
        tree = ast.parse(Path("reverse_historical_discovery.py").read_text(encoding="utf-8"))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertEqual(
            sorted(used & forbidden), [],
            f"discovery must not reference the classifier's thresholds: {sorted(used & forbidden)}")
        self.assertEqual(rhd.MOVE_THRESHOLD, 0.20)

    def test_a_candidate_is_not_labelled_proven(self):
        history = {"0xl": [buy("0xl", "S1", 1_000, 1.0), sell("0xl", "S1", 1_500, 1.4),
                           buy("0xl", "S2", 10_000, 1.0), sell("0xl", "S2", 10_500, 1.4)]}
        events, _ = rhd.build_events(rhd.asset_observations(history),
                                     rhd.collect_entries(history), min_history_days=0)
        wallets, _ = rhd.build_wallets(events, {"0xl": "NOT_IN_REGISTRY"})
        entry = wallets["0xl"]
        self.assertEqual(entry["proven_status"], "NOT_IN_REGISTRY")
        self.assertGreaterEqual(entry["independent_pre_pump_events"], 2)

    def test_a_candidate_absent_from_the_registry_is_marked_as_such(self):
        events = [{"wallet": "0xzz", "event_id": "e1", "chain": "eth", "asset_address": "A",
                   "symbol": None, "entry_ts": 1, "entry_price": 1.0, "entry_usd": 1.0,
                   "entry_from_trade_timestamp": True, "move_start_ts": 1,
                   "move_start_price": 1.0, "confirmation_ts": 2, "confirmation_price": 1.2,
                   "price_change_pct": 20.0, "lead_time_seconds": 1,
                   "entry_precedes_confirmation": True, "source": "gmgn", "exit_ts": None}]
        wallets, _ = rhd.build_wallets(events, {})
        self.assertEqual(wallets["0xzz"]["proven_status"], "NOT_IN_REGISTRY")


class TestSafetyAndIntegrity(unittest.TestCase):
    def test_no_execution_or_scoring_import(self):
        source = Path("reverse_historical_discovery.py").read_text(encoding="utf-8")
        for forbidden in ("paper_trading", "confluence_engine", "trade_readiness",
                          "risk_engine", "scanner", "create_order", "place_order",
                          "LIVE_TRADING"):
            self.assertNotIn(forbidden, source)

    def test_no_network_access(self):
        source = Path("reverse_historical_discovery.py").read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "http://", "https://", "socket"):
            self.assertNotIn(forbidden, source)

    def test_orders_enabled_is_false(self):
        self.assertFalse(rhd.build({}, {}).get("orders_enabled", False))

    def test_the_gmgn_archive_is_unchanged(self):
        self.assertEqual(rhd.file_md5(SOURCE), SOURCE_MD5)

    def test_digest_excludes_only_the_wall_clock_field(self):
        envelope = rhd.build({}, {}, min_history_days=0)
        stored = envelope["determinism"]["payload_sha256"]
        self.assertEqual(rhd.payload_digest(envelope), stored)
        moved = dict(envelope)
        moved["generated_at"] = "1999-01-01T00:00:00+00:00"
        self.assertEqual(rhd.payload_digest(moved), stored)

    def test_building_twice_is_deterministic_apart_from_the_stamp(self):
        history = {"0xm": [buy("0xm", "T", 1_000, 1.0), sell("0xm", "T", 1_500, 1.4)]}
        a = rhd.build(history, {}, min_history_days=0)
        b = rhd.build(history, {}, min_history_days=0)
        strip = lambda r: {k: v for k, v in r.items() if k != "generated_at"}  # noqa: E731
        self.assertEqual(strip(a), strip(b))
        self.assertEqual(a["determinism"]["payload_sha256"],
                         b["determinism"]["payload_sha256"])


class TestCommittedArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ARTIFACT.exists():
            raise unittest.SkipTest("no committed reverse discovery artifact yet")
        cls.artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    def test_the_artifact_is_valid_json_with_a_schema_and_provenance(self):
        for field in ("schema_version", "generated_at", "source", "totals",
                      "candidates", "limitations", "determinism", "definitions"):
            self.assertIn(field, self.artifact)
        self.assertEqual(self.artifact["source"]["md5"], SOURCE_MD5)
        self.assertFalse(self.artifact["source"]["proven_status_mutated"])
        self.assertIs(self.artifact["orders_enabled"], False)

    def test_the_stored_digest_describes_the_stored_envelope(self):
        self.assertEqual(self.artifact["determinism"]["payload_sha256"],
                         rhd.payload_digest(self.artifact))
        self.assertEqual(self.artifact["determinism"]["non_deterministic_fields"],
                         ["generated_at"])

    def test_totals_agree_with_the_candidate_blocks(self):
        totals = self.artifact["totals"]
        candidates = self.artifact["candidates"]
        self.assertEqual(totals["total_wallets_observed_before_moves"], len(candidates))
        self.assertEqual(totals["wallets_with_repeated_independent_events"],
                         sum(1 for c in candidates.values()
                             if c["independent_pre_pump_events"] >= 2))
        self.assertEqual(totals["total_pre_move_entries"],
                         sum(c["independent_pre_pump_events"] for c in candidates.values()))

    def test_candidate_event_counts_match_their_event_lists(self):
        for wallet, entry in self.artifact["candidates"].items():
            with self.subTest(wallet=wallet):
                self.assertEqual(entry["wallet"], wallet)
                self.assertEqual(entry["independent_pre_pump_events"],
                                 len(entry["events"]))
                self.assertGreaterEqual(entry["distinct_assets"], 1)
                self.assertGreaterEqual(entry["mean_lead_time_seconds"], 0)
                self.assertIn(entry["proven_status"], tuple(whv.CLASSES) + ("NOT_IN_REGISTRY",))

    def test_every_event_carries_the_required_evidence(self):
        for wallet, entry in self.artifact["candidates"].items():
            for event in entry["events"]:
                with self.subTest(wallet=wallet):
                    for field in ("wallet", "asset_address", "entry_ts", "entry_price",
                                  "move_start_price", "confirmation_ts", "confirmation_price",
                                  "lead_time_seconds", "price_change_pct", "source",
                                  "event_id", "exit_ts"):
                        self.assertIn(field, event)
                    self.assertIsNotNone(event["entry_price"])
                    self.assertEqual(event["source"], hd.SOURCE_GMGN)

    def test_the_history_gate_outcome_is_recorded_honestly(self):
        totals = self.artifact["totals"]
        self.assertEqual(
            totals["assets_rejected_short_history"] + totals["assets_eligible"],
            totals["total_assets_scanned"],
        )
        # The gate result and the measured shortfall must both be present, so the
        # zero cannot be mistaken for "no moves exist".
        self.assertIn("diagnostics", self.artifact)
        self.assertGreater(self.artifact["diagnostics"]["asset_history_span_days"]["max"], 0)

    def test_the_limitations_are_present_and_specific(self):
        limits = self.artifact["limitations"]
        for key in ("no_ohlc_price_series", "provider_move_fields_unusable",
                    "observation_time_fallback", "discovery_is_not_conviction"):
            self.assertIn(key, limits)
            self.assertTrue(limits[key].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
