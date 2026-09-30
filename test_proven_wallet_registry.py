#!/usr/bin/env python3
"""Focused tests for the PROVEN_WALLET_REGISTRY and its committed artifact.

Covers the two things the registry must never get wrong: that ``proven_status``
comes from the existing classifier and nowhere else, and that the committed
artifact is a truthful, reproducible statement of the GMGN dataset.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import historical_discovery as hd  # noqa: E402
import proven_wallet_registry as pwr  # noqa: E402
import wallet_history_validation as whv  # noqa: E402

ARTIFACT = HERE / "proven_wallet_registry.json"
SOURCE = HERE / "gmgn_wallet_history.json"
SOURCE_MD5 = "ef1e23b980ae3b4cb0ad239267cbbae4"


def buy(wallet: str, coin: str, ts: int, usd: float, price: float = 1.0) -> dict:
    return {
        "wallet": wallet,
        "address": coin,
        "symbol": coin.upper(),
        "chain": "eth",
        "side": "buy",
        "amount_usd": usd,
        "price_usd": price,
        "price_change": None,
        "trade_timestamp": ts,
    }


def winning_wallet(wallet: str, ts: int) -> list[dict]:
    """Three distinct assets that each reach 2x inside the 14-day window.

    The classifier only calls an entry observed when a later row inside the
    window carries a price above the entry, and only a *win* when that peak is
    at least 2x. A bare buy with no follow-up price is UNKNOWN, never a win, so
    the second row is what makes this fixture genuinely PROVEN.
    """
    rows: list[dict] = []
    for i in range(3):
        start = ts + i * 30 * 86_400
        coin = f"c{i}-{wallet[:4]}"
        rows.append(buy(wallet, coin, start, 1_000.0, 1.0))
        peak = dict(buy(wallet, coin, start + 86_400, 1_000.0, 2.5))
        peak["side"] = "sell"
        rows.append(peak)
    return rows


def losing_wallet(wallet: str, ts: int) -> list[dict]:
    """Three distinct assets that are observed but never reach 2x."""
    rows: list[dict] = []
    for i in range(3):
        start = ts + i * 30 * 86_400
        coin = f"d{i}-{wallet[:4]}"
        rows.append(buy(wallet, coin, start, 1_000.0, 1.0))
        peak = dict(buy(wallet, coin, start + 86_400, 1_000.0, 1.1))
        peak["side"] = "sell"
        rows.append(peak)
    return rows


class TestProvenSource(unittest.TestCase):
    def test_proven_states_are_reused_verbatim_from_the_classifier(self):
        # A third vocabulary here would let the registry and the report disagree.
        self.assertIs(pwr.PROVEN_STATES, whv.CLASSES)

    def test_proven_status_comes_from_the_existing_classifier(self):
        rows = winning_wallet("0xproven", 1_700_000_000)
        entry = pwr.build_entry("0xproven", rows)
        direct = whv.reconstruct_wallet("0xproven", rows)
        self.assertEqual(entry["proven_status"], direct["history_class"])
        self.assertEqual(entry["proven_status"], whv.PROVEN)

    def test_the_thresholds_are_the_existing_ones_and_are_not_restated(self):
        criteria = pwr.build_entry("0xproven", winning_wallet("0xproven", 1_700_000_000))
        evidence = criteria["provenance"]["evidence"]["criteria"]
        self.assertEqual(evidence["min_observed_entries"], whv.MIN_OBSERVED_ENTRIES)
        self.assertEqual(evidence["min_successful_entries"], whv.MIN_SUCCESSFUL_ENTRIES)
        self.assertEqual(evidence["min_win_rate_pct"], whv.MIN_WIN_RATE_PCT)
        self.assertEqual(evidence["target_multiple"], whv.TARGET_MULTIPLE)
        self.assertEqual(whv.MIN_OBSERVED_ENTRIES, 3)
        self.assertEqual(whv.MIN_SUCCESSFUL_ENTRIES, 2)
        self.assertEqual(whv.MIN_WIN_RATE_PCT, 60.0)
        self.assertEqual(whv.TARGET_MULTIPLE, 2.0)

    def test_a_losing_wallet_is_never_recorded_as_proven(self):
        entry = pwr.build_entry("0xlose", losing_wallet("0xlose", 1_700_000_000))
        self.assertNotEqual(entry["proven_status"], whv.PROVEN)
        self.assertEqual(entry["proven_status"], whv.ACTIVITY_BUT_UNPROVEN)

    def test_a_wallet_with_no_rows_is_cold_start_not_proven(self):
        entry = pwr.build_entry("0xempty", [])
        self.assertEqual(entry["proven_status"], whv.NO_HISTORY)
        self.assertFalse(any(e["proven_status"] == whv.PROVEN for e in [entry]))


class TestRegistryEnvelope(unittest.TestCase):
    def test_the_registry_never_enables_execution(self):
        registry = pwr.build_registry({"0xproven": winning_wallet("0xproven", 1_700_000_000)})
        self.assertFalse(registry["orders_enabled"])
        self.assertEqual(registry["mode"], pwr.MODE)

    def test_proven_count_equals_the_number_of_proven_entries(self):
        history = {
            "0xa": winning_wallet("0xa", 1_700_000_000),
            "0xb": winning_wallet("0xb", 1_700_000_000),
            "0xc": losing_wallet("0xc", 1_700_000_000),
            "0xd": [],
        }
        registry = pwr.build_registry(history)
        actual = sum(1 for e in registry["wallets"].values() if e["proven_status"] == whv.PROVEN)
        self.assertEqual(registry["proven_count"], actual)

    def test_validate_reports_nothing_for_a_well_formed_registry(self):
        registry = pwr.build_registry({"0xa": winning_wallet("0xa", 1_700_000_000)})
        self.assertEqual(pwr.validate_registry(registry), [])

    def test_validate_catches_an_unknown_status_and_an_enabled_registry(self):
        entry = pwr.build_entry("0xa", winning_wallet("0xa", 1_700_000_000))
        entry["proven_status"] = "VERY_PROVEN"
        self.assertTrue(any("unknown proven_status" in p for p in pwr.validate_entry(entry)))
        bad = pwr.build_registry({"0xa": winning_wallet("0xa", 1_700_000_000)})
        bad["orders_enabled"] = True
        self.assertTrue(pwr.validate_registry(bad))

    def test_unfilled_fields_are_named_rather_than_left_as_silent_gaps(self):
        entry = pwr.build_entry("0xa", winning_wallet("0xa", 1_700_000_000))
        unknown = entry["provenance"]["unknown_metrics"]
        self.assertIn("drawdown", unknown)
        for field in unknown:
            self.assertIsNone(entry[field], f"{field} listed as unknown but populated")

    def test_a_wallet_missing_from_the_current_feed_keeps_its_entry(self):
        history = {"0xa": winning_wallet("0xa", 1_700_000_000)}
        registry = pwr.build_registry(history, current_wallets=[])
        self.assertIn(hd.wallet_key("0xa"), registry["wallets"])
        self.assertFalse(registry["wallets"][hd.wallet_key("0xa")]["active_now"])
        self.assertTrue(registry["current_feed_observed"])

    def test_without_a_feed_active_now_is_unknown_not_false(self):
        registry = pwr.build_registry({"0xa": winning_wallet("0xa", 1_700_000_000)})
        self.assertIsNone(registry["wallets"][hd.wallet_key("0xa")]["active_now"])
        self.assertFalse(registry["current_feed_observed"])


class TestNormalizationIsNotAReclassification(unittest.TestCase):
    def test_normalizing_the_observed_shape_does_not_change_any_classification(self):
        rows = winning_wallet("0xa", 1_700_000_000)
        normalized = hd.normalize_rows(rows, hd.SOURCE_GMGN, wallet="0xa", fetched_at=None)
        self.assertEqual(
            pwr.build_entry("0xa", rows)["proven_status"],
            pwr.build_entry("0xa", normalized)["proven_status"],
        )

    def test_normalization_is_what_makes_provenance_truthful(self):
        # Raw collector rows carry no kind/sources, so a registry built straight
        # from them would claim zero trades and no source at all.
        raw = pwr.build_entry("0xa", winning_wallet("0xa", 1_700_000_000))
        normalized = hd.normalize_rows(
            winning_wallet("0xa", 1_700_000_000), hd.SOURCE_GMGN, wallet="0xa", fetched_at=None
        )
        observed = pwr.build_entry("0xa", normalized)
        self.assertEqual(observed["sources"], [hd.SOURCE_GMGN])
        self.assertGreater(observed["historical_trades"], 0)


class TestCommittedArtifact(unittest.TestCase):
    """Assertions about the committed artifact, not about synthetic fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.registry = pwr.load_registry(ARTIFACT)
        if not cls.registry:
            raise unittest.SkipTest("no committed registry artifact yet")

    def test_the_artifact_exists_and_validates(self):
        self.assertTrue(ARTIFACT.is_file(), "proven_wallet_registry.json is not committed")
        self.assertEqual(pwr.validate_registry(self.registry), [])

    def test_the_artifact_never_enables_execution(self):
        self.assertIs(self.registry["orders_enabled"], False)

    def test_proven_count_equals_the_actual_proven_entries(self):
        actual = sum(
            1 for e in self.registry["wallets"].values() if e["proven_status"] == whv.PROVEN
        )
        self.assertEqual(self.registry["proven_count"], actual)
        self.assertGreater(actual, 0, "a populated registry with zero PROVEN wallets is empty")

    def test_every_proven_wallet_actually_meets_the_existing_criteria(self):
        for wallet, entry in self.registry["wallets"].items():
            if entry["proven_status"] != whv.PROVEN:
                continue
            with self.subTest(wallet=wallet):
                evidence = entry["provenance"]["evidence"]
                self.assertGreaterEqual(evidence["observed_entries"], whv.MIN_OBSERVED_ENTRIES)
                self.assertGreaterEqual(evidence["successful_entries"], whv.MIN_SUCCESSFUL_ENTRIES)
                self.assertGreaterEqual(entry["win_rate"], whv.MIN_WIN_RATE_PCT)

    def test_no_non_proven_wallet_claims_proven_without_the_evidence(self):
        for wallet, entry in self.registry["wallets"].items():
            if entry["proven_status"] == whv.PROVEN:
                continue
            with self.subTest(wallet=wallet):
                self.assertNotEqual(entry["proven_status"], "PROVEN_LITE")

    def test_the_artifact_declares_the_source_archive_it_came_from(self):
        source = self.registry["source"]
        self.assertEqual(source["artifact"], SOURCE.name)
        self.assertEqual(source["md5"], SOURCE_MD5)
        self.assertEqual(source["provider"], hd.SOURCE_GMGN)
        self.assertEqual(source["wallets_in_source"], self.registry["wallet_count"])

    def test_generated_at_is_the_only_non_deterministic_field(self):
        self.assertEqual(self.registry["determinism"]["non_deterministic_fields"], ["generated_at"])
        self.assertIn("generated_at", self.registry)

    def test_the_committed_digest_matches_its_own_payload(self):
        import build_proven_wallet_registry as gen
        self.assertEqual(self.registry["determinism"]["payload_sha256"], gen.payload_digest(self.registry))

    def test_rebuilding_from_the_archive_reproduces_the_committed_digest(self):
        import build_proven_wallet_registry as gen
        rebuilt = gen.build()
        self.assertEqual(
            rebuilt["determinism"]["payload_sha256"],
            self.registry["determinism"]["payload_sha256"],
        )
        self.assertEqual(rebuilt["proven_count"], self.registry["proven_count"])
        self.assertEqual(rebuilt["wallet_count"], self.registry["wallet_count"])

    def test_the_source_archive_is_untouched_by_the_registry(self):
        import build_proven_wallet_registry as gen
        self.assertEqual(gen.file_md5(SOURCE), SOURCE_MD5)


class TestMergeAgainstTheRealRegistry(unittest.TestCase):
    """The refresh path, exercised against the committed 2,215-wallet artifact.

    Synthetic fixtures proved the merge shape on three wallets. That is not the
    operation that runs in practice: the merge is what protects a statement of
    record covering 2,215 wallets and 7 PROVEN entries, so it is asserted here
    against that artifact rather than against a toy.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = pwr.load_registry(ARTIFACT)
        if not cls.base:
            raise unittest.SkipTest("no committed registry artifact yet")
        cls.proven_keys = [
            w for w, e in cls.base["wallets"].items() if e["proven_status"] == whv.PROVEN
        ]

    def incoming(self, wallets: dict, *, feed_observed: bool = True) -> dict:
        """A minimal refresh envelope, the shape a feed-based refresh produces."""
        return {
            "schema_version": pwr.SCHEMA_VERSION,
            "mode": pwr.MODE,
            "orders_enabled": False,
            "current_feed_observed": feed_observed,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "wallets": wallets,
        }

    def full_refresh(self) -> dict:
        """A refresh that re-reports every base entry, unchanged."""
        return {w: dict(e) for w, e in self.base["wallets"].items()}

    # -- the base artifact itself ------------------------------------------
    def test_the_base_is_the_expected_populated_registry(self):
        self.assertEqual(self.base["wallet_count"], len(self.base["wallets"]))
        self.assertEqual(self.base["wallet_count"], 2215)
        self.assertEqual(self.base["proven_count"], len(self.proven_keys))
        self.assertGreater(self.base["proven_count"], 0)
        self.assertEqual(pwr.validate_registry(self.base), [])

    # -- invariant: output is never smaller than the base ------------------
    def test_merge_output_is_never_smaller_than_the_base(self):
        cases = {
            "identical full refresh": self.full_refresh(),
            "empty feed": {},
            "one wallet": {self.proven_keys[0]: dict(self.base["wallets"][self.proven_keys[0]])},
            "half the proven wallets": {
                w: dict(self.base["wallets"][w]) for w in self.proven_keys[:3]
            },
        }
        for name, feed in cases.items():
            with self.subTest(case=name):
                merged = pwr.merge_registry(self.base, self.incoming(feed))
                self.assertGreaterEqual(merged["wallet_count"], self.base["wallet_count"])
                self.assertGreaterEqual(merged["proven_count"], self.base["proven_count"])
                self.assertEqual(merged["wallet_count"], len(merged["wallets"]))

    # -- invariant: nothing is deleted -------------------------------------
    def test_no_base_wallet_is_ever_deleted(self):
        for name, feed in (
            ("empty feed", {}),
            ("one wallet", {self.proven_keys[0]: dict(self.base["wallets"][self.proven_keys[0]])}),
            ("no feed observed", self.full_refresh()),
        ):
            with self.subTest(case=name):
                observed = name != "no feed observed"
                merged = pwr.merge_registry(self.base, self.incoming(feed, feed_observed=observed))
                self.assertEqual(set(merged["wallets"]), set(self.base["wallets"]))

    # -- invariant: a quiet feed cannot retire a proven wallet -------------
    def test_an_empty_feed_does_not_retire_a_proven_wallet(self):
        merged = pwr.merge_registry(self.base, self.incoming({}))
        self.assertEqual(merged["wallet_count"], self.base["wallet_count"])
        self.assertEqual(merged["proven_count"], self.base["proven_count"])
        for wallet in self.proven_keys:
            with self.subTest(wallet=wallet):
                self.assertIn(wallet, merged["wallets"])
                self.assertEqual(merged["wallets"][wallet]["proven_status"], whv.PROVEN)
        self.assertEqual(pwr.validate_registry(merged), [])

    # -- invariant: proven survives absence from the new feed --------------
    def test_a_proven_wallet_absent_from_the_new_feed_stays_proven(self):
        feed = {w: dict(self.base["wallets"][w]) for w in self.proven_keys[:2]}
        absent = [w for w in self.proven_keys if w not in feed]
        self.assertTrue(absent, "fixture must actually omit some proven wallets")

        merged = pwr.merge_registry(self.base, self.incoming(feed))
        for wallet in absent:
            with self.subTest(wallet=wallet):
                self.assertIn(wallet, merged["wallets"])
                self.assertEqual(merged["wallets"][wallet]["proven_status"], whv.PROVEN)
                # Gone from the feed, so not active -- but still recorded.
                self.assertIs(merged["wallets"][wallet]["active_now"], False)
        self.assertEqual(merged["proven_count"], self.base["proven_count"])

    # -- invariant: identity, provenance and history survive ---------------
    def test_identity_provenance_and_history_survive_a_merge(self):
        merged = pwr.merge_registry(self.base, self.incoming(self.full_refresh()))
        for wallet, entry in self.base["wallets"].items():
            with self.subTest(wallet=wallet):
                after = merged["wallets"][wallet]
                self.assertEqual(after["wallet"], entry["wallet"])
                self.assertEqual(after["chains"], entry["chains"])
                self.assertEqual(after["sources"], entry["sources"])
                self.assertEqual(after["historical_trades"], entry["historical_trades"])
                self.assertEqual(after["proven_status"], entry["proven_status"])
                self.assertEqual(after["last_seen"], entry["last_seen"])
                self.assertEqual(
                    after["provenance"]["evidence"]["observed_entries"],
                    entry["provenance"]["evidence"]["observed_entries"],
                )

    def test_a_sparse_reread_cannot_blank_a_known_score_or_drop_a_field(self):
        # A re-read that reports almost nothing must not erase what is known,
        # and must not delete keys either: a missing key and an explicit null
        # are different to validate_entry.
        target = self.proven_keys[0]
        sparse = {target: {"wallet": target, "proven_status": None, "active_now": None,
                           "last_seen": None, "win_rate": None}}
        merged = pwr.merge_registry(self.base, self.incoming(sparse))
        after = merged["wallets"][target]
        self.assertEqual(after["win_rate"], self.base["wallets"][target]["win_rate"])
        self.assertEqual(after["proven_status"], whv.PROVEN)
        for field in pwr.REGISTRY_FIELDS:
            self.assertIn(field, after, f"merge dropped the key {field}")
        self.assertEqual(pwr.validate_registry(merged), [])

    def test_last_seen_never_moves_backwards(self):
        target = max(self.base["wallets"], key=lambda w: self.base["wallets"][w]["last_seen"] or 0)
        older = dict(self.base["wallets"][target])
        older["last_seen"] = 0
        merged = pwr.merge_registry(self.base, self.incoming({target: older}))
        self.assertEqual(
            merged["wallets"][target]["last_seen"],
            self.base["wallets"][target]["last_seen"],
        )

    # -- invariant: proven_count matches reality ---------------------------
    def test_proven_count_always_equals_the_actual_proven_entries(self):
        cases = {
            "full refresh": self.full_refresh(),
            "empty feed": {},
            "proven only": {w: dict(self.base["wallets"][w]) for w in self.proven_keys},
        }
        for name, feed in cases.items():
            with self.subTest(case=name):
                merged = pwr.merge_registry(self.base, self.incoming(feed))
                actual = sum(
                    1 for e in merged["wallets"].values()
                    if e["proven_status"] == whv.PROVEN
                )
                self.assertEqual(merged["proven_count"], actual)
                self.assertEqual(actual, self.base["proven_count"])

    def test_merge_never_promotes_or_demotes_on_its_own(self):
        # The merge is a container operation. It must not reclassify anyone, or a
        # refresh would silently change conviction with no new evidence.
        merged = pwr.merge_registry(self.base, self.incoming(self.full_refresh()))
        for wallet, entry in self.base["wallets"].items():
            self.assertEqual(
                merged["wallets"][wallet]["proven_status"], entry["proven_status"]
            )

    # -- invariant: deterministic -------------------------------------------
    def test_the_merge_is_deterministic_apart_from_the_wall_clock_stamp(self):
        feed = self.full_refresh()
        first = pwr.merge_registry(self.base, self.incoming(feed))
        second = pwr.merge_registry(self.base, self.incoming(feed))

        def payload(reg):
            return {k: v for k, v in reg.items() if k != "generated_at"}

        self.assertEqual(payload(first), payload(second))
        self.assertEqual(first["wallet_count"], second["wallet_count"])
        self.assertEqual(first["proven_count"], second["proven_count"])

    def test_an_identical_refresh_leaves_the_base_wallets_untouched(self):
        # A no-op refresh must be a genuine no-op for the wallet payload, not a
        # silent rewrite. The envelope metadata does legitimately move:
        # `current_feed_observed` flips to True because this refresh consulted a
        # feed, where the committed base was built without one.
        merged = pwr.merge_registry(self.base, self.incoming(self.full_refresh()))
        self.assertEqual(merged["wallets"], self.base["wallets"])
        self.assertEqual(merged["wallet_count"], self.base["wallet_count"])
        self.assertEqual(merged["proven_count"], self.base["proven_count"])
        self.assertTrue(merged["current_feed_observed"])
        self.assertFalse(self.base["current_feed_observed"])
        for field in ("schema_version", "mode", "entry_fields", "unsupported_metrics",
                      "metric_caveats", "proven_criteria_source"):
            self.assertEqual(merged[field], self.base[field], f"{field} changed")

    # -- the merge must not weaken the safety posture ----------------------
    def test_a_merge_never_enables_execution(self):
        for feed in ({}, self.full_refresh()):
            with self.subTest(feed_size=len(feed)):
                merged = pwr.merge_registry(self.base, self.incoming(feed))
                self.assertIs(merged["orders_enabled"], False)
                self.assertEqual(merged["mode"], pwr.MODE)

    def test_merging_onto_an_empty_base_produces_only_the_incoming_wallets(self):
        merged = pwr.merge_registry(None, self.incoming({self.proven_keys[0]:
            dict(self.base["wallets"][self.proven_keys[0]])}))
        self.assertEqual(merged["wallet_count"], 1)
        self.assertEqual(merged["proven_count"], 1)

    def test_a_generator_built_refresh_preserves_the_audit_blocks(self):
        # A real refresh carries source/determinism. Because merge_registry
        # takes its envelope from `incoming`, dropping those blocks here would
        # quietly discard the archive md5 and the reproducibility digest.
        import build_proven_wallet_registry as gen
        refresh = gen.build()
        self.assertIn("source", refresh)
        self.assertIn("determinism", refresh)
        merged = pwr.merge_registry(self.base, copy.deepcopy(refresh))
        self.assertEqual(merged["source"]["md5"], self.base["source"]["md5"])
        self.assertEqual(
            merged["determinism"]["payload_sha256"],
            gen.payload_digest(merged),
            "merged digest must still describe the merged envelope",
        )


class TestRegistryStaysOffExecutionPaths(unittest.TestCase):
    def test_the_registry_imports_no_execution_or_scoring_path(self):
        source = (HERE / "proven_wallet_registry.py").read_text(encoding="utf-8")
        for forbidden in ("create_order", "place_order", "LIVE_TRADING"):
            self.assertNotIn(forbidden, source)

    def test_the_generator_calls_no_external_api(self):
        source = (HERE / "build_proven_wallet_registry.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests", "urllib", "http://", "https://", "socket",
            "gmgn_layer", "zerion_layer", "hyperliquid_layer",
        ):
            self.assertNotIn(forbidden, source, f"{forbidden} must stay out of the generator")


if __name__ == "__main__":
    unittest.main(verbosity=2)
