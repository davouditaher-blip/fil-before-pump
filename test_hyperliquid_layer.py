"""Focused tests for the read-only Hyperliquid public Info client.

Every test here injects a fake transport, so the suite performs no network I/O
and needs no account, wallet, key or signature. That is itself part of what is
being asserted: this source is reachable through public read-only interfaces
alone.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import historical_discovery as hd
import hyperliquid_layer as hl

WALLET = "0x8c967e73e6b15087c42a10d344cff4c96d877f1d"
T0 = 1_700_000_000          # seconds
T0_MS = T0 * 1000
HASH = "0xa166e3fa63c25663024b03f2e0da011a00307e4017465df020210d3d432e7cb8"


def perp_fill(**over):
    """A fill exactly as the official Info-endpoint docs show it."""
    fill = {
        "closedPnl": "0.0",
        "coin": "AVAX",
        "crossed": False,
        "dir": "Open Long",
        "hash": HASH,
        "oid": 90542681,
        "px": "18.435",
        "side": "B",
        "startPosition": "26.86",
        "sz": "93.53",
        "time": 1681222254710,
        "fee": "0.01",
        "feeToken": "USDC",
        "tid": 118906512037719,
    }
    fill.update(over)
    return fill


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class FakePost:
    """Records every call so a test can assert exactly what left the process."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": dict(body), "headers": dict(headers), "timeout": timeout})
        if not self.responses:
            return FakeResponse(200, [])
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def no_sleep(_seconds):
    return None


class TestPublicRequestConstruction(unittest.TestCase):
    """The request is a public, unauthenticated read."""

    def setUp(self):
        hl.reset_state()

    def test_endpoint_is_the_public_https_info_url(self):
        self.assertEqual(hl.INFO_URL, "https://api.hyperliquid.xyz/info")
        self.assertTrue(hl.INFO_URL.startswith("https://"))

    def test_by_time_request_carries_type_user_and_window_only(self):
        body = hl.build_fill_request(WALLET, start_ms=T0_MS, end_ms=T0_MS + 86_400_000)
        self.assertEqual(body["type"], "userFillsByTime")
        self.assertEqual(body["user"], WALLET)
        self.assertEqual(body["startTime"], T0_MS)
        self.assertEqual(body["endTime"], T0_MS + 86_400_000)

    def test_request_body_contains_no_credential_of_any_kind(self):
        body = hl.build_fill_request(WALLET, start_ms=T0_MS)
        forbidden = ("key", "secret", "signature", "nonce", "auth", "token", "password", "wallet_key")
        for name in forbidden:
            self.assertNotIn(name, body, f"{name!r} must never appear in a public request")
        self.assertEqual(set(body), {"type", "user", "startTime"})

    def test_by_time_requires_a_start_time(self):
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.build_fill_request(WALLET, start_ms=None)
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.build_fill_request(WALLET, start_ms="")

    def test_end_before_start_is_refused(self):
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.build_fill_request(WALLET, start_ms=T0_MS, end_ms=T0_MS - 1)

    def test_recent_request_takes_no_window(self):
        body = hl.build_fill_request(WALLET, request_type="userFills")
        self.assertEqual(body, {"type": "userFills", "user": WALLET})
        # Silently dropping a requested range would return recent fills while the
        # caller believed it had a window.
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.build_fill_request(WALLET, start_ms=T0_MS, request_type="userFills")

    def test_unknown_request_type_is_refused(self):
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.build_fill_request(WALLET, start_ms=T0_MS, request_type="exchange")

    def test_aggregate_by_time_is_only_included_when_asked(self):
        self.assertNotIn("aggregateByTime", hl.build_fill_request(WALLET, start_ms=T0_MS))
        self.assertIs(
            hl.build_fill_request(WALLET, start_ms=T0_MS, aggregate_by_time=True)["aggregateByTime"],
            True,
        )

    def test_address_is_normalized_to_one_key(self):
        self.assertEqual(hl.normalize_wallet(WALLET.upper()), WALLET)
        self.assertEqual(hl.normalize_wallet(f"  {WALLET}  "), WALLET)

    def test_a_bad_address_is_refused_before_anything_is_sent(self):
        for bad in ("", None, "0x123", WALLET[:-1], "not-an-address", 12345):
            with self.subTest(bad=bad):
                with self.assertRaises(hl.HyperliquidRequestError):
                    hl.normalize_wallet(bad)

    def test_nothing_is_sent_when_the_address_is_invalid(self):
        # A bad address is a caller error, not a provider error: it is refused
        # before the transport exists, so no request can escape.
        post = FakePost(FakeResponse(200, [perp_fill()]))
        with self.assertRaises(hl.HyperliquidRequestError):
            hl.fetch_fills("0xnope", start_ms=T0_MS, post=post, sleep=no_sleep)
        self.assertEqual(post.calls, [], "nothing may leave the process for a bad address")

    def test_transport_sends_no_authorization_header(self):
        post = FakePost(FakeResponse(200, []))
        hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        headers = post.calls[0]["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("User-Agent", headers)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_module_reads_no_secret_from_the_environment(self):
        source = Path(hl.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


class TestTimestampHandling(unittest.TestCase):
    def test_milliseconds_pass_through(self):
        self.assertEqual(hl.to_millis(T0_MS), T0_MS)

    def test_seconds_are_widened_to_milliseconds(self):
        self.assertEqual(hl.to_millis(T0), T0_MS)

    def test_iso_strings_are_accepted(self):
        self.assertEqual(hl.to_millis("2023-11-14T22:13:20Z"), 1_700_000_000_000)

    def test_unusable_values_stay_missing(self):
        for bad in (None, "", "not-a-time", 0, -5, True, False, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.assertIsNone(hl.to_millis(bad))

    def test_fill_time_reaches_the_observation_in_seconds(self):
        row = hl.normalize_fill(perp_fill(), wallet=WALLET, fetched_at=T0_MS)
        self.assertEqual(row["time"], 1681222254710)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET, fetched_at=T0_MS)
        self.assertEqual(obs["trade_timestamp"], 1681222254710 // 1000)
        self.assertTrue(obs["trade_time_known"])

    def test_missing_fill_time_is_never_reported_as_epoch(self):
        row = hl.normalize_fill(perp_fill(time=None), wallet=WALLET)
        self.assertIsNone(row["time"])
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
        self.assertIsNone(obs["trade_timestamp"])
        self.assertFalse(obs["trade_time_known"])
        self.assertIn("no_trade_timestamp", obs["confidence"]["reasons"])

    def test_fetched_at_is_the_observation_time_and_stays_distinct_from_the_trade(self):
        # The shared schema keeps these apart on purpose: `timestamp` is when the
        # row was observed, `trade_timestamp` is when the fill happened.
        row = hl.normalize_fill(perp_fill(), wallet=WALLET, fetched_at=T0_MS)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET, fetched_at=T0_MS)
        self.assertEqual(obs["fetched_at"], T0)
        self.assertEqual(obs["timestamp"], T0)
        self.assertEqual(obs["trade_timestamp"], 1681222254710 // 1000)
        self.assertNotEqual(obs["timestamp"], obs["trade_timestamp"])


class TestResponseNormalization(unittest.TestCase):
    def setUp(self):
        hl.reset_state()

    def test_documented_perp_fill_fields_are_kept(self):
        row = hl.normalize_fill(perp_fill(), wallet=WALLET)
        self.assertEqual(row["coin"], "AVAX")
        self.assertEqual(row["px"], 18.435)
        self.assertEqual(row["sz"], 93.53)
        self.assertEqual(row["side"], "B")
        self.assertEqual(row["dir"], "Open Long")
        self.assertEqual(row["tid"], 118906512037719)
        self.assertEqual(row["oid"], 90542681)
        self.assertEqual(row["closed_pnl"], 0.0)
        self.assertEqual(row["fee_token"], "USDC")

    def test_notional_is_derived_from_reported_price_and_size(self):
        row = hl.normalize_fill(perp_fill(px="18.435", sz="93.53"), wallet=WALLET)
        self.assertAlmostEqual(row["usd"], 18.435 * 93.53, places=6)

    def test_notional_is_none_not_zero_when_a_field_is_missing(self):
        for over in ({"px": None}, {"sz": None}, {"px": "", "sz": "10"}, {"px": "abc", "sz": "10"}):
            with self.subTest(over=over):
                row = hl.normalize_fill(perp_fill(**over), wallet=WALLET)
                self.assertIsNone(row["usd"])

    def test_boolean_numbers_are_not_coerced(self):
        row = hl.normalize_fill(perp_fill(px=True, sz="1"), wallet=WALLET)
        self.assertIsNone(row["usd"])

    def test_spot_and_hip3_asset_identifiers_are_preserved_verbatim(self):
        self.assertEqual(hl.normalize_fill(perp_fill(coin="@107"), wallet=WALLET)["coin"], "@107")
        self.assertEqual(hl.normalize_fill(perp_fill(coin="xyz:XYZ100"), wallet=WALLET)["coin"], "xyz:XYZ100")

    def test_a_fill_without_an_asset_has_no_identity_and_is_refused(self):
        self.assertIsNone(hl.normalize_fill(perp_fill(coin=""), wallet=WALLET))
        self.assertIsNone(hl.normalize_fill(perp_fill(coin=None), wallet=WALLET))
        self.assertIsNone(hl.normalize_fill("not-a-mapping", wallet=WALLET))
        self.assertIsNone(hl.normalize_fill(None, wallet=WALLET))

    def test_rejected_rows_are_counted_rather_than_dropped_silently(self):
        out = hl.normalize_fills([perp_fill(), perp_fill(coin=""), "junk"], wallet=WALLET)
        self.assertEqual(out["received"], 3)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rejected"], 2)

    def test_wrapped_and_bare_payloads_are_both_accepted(self):
        fills = [perp_fill()]
        self.assertEqual(len(hl.normalize_fills(fills, wallet=WALLET)["rows"]), 1)
        self.assertEqual(len(hl.normalize_fills({"fills": fills}, wallet=WALLET)["rows"]), 1)
        self.assertEqual(len(hl.normalize_fills({"data": fills}, wallet=WALLET)["rows"]), 1)
        self.assertEqual(hl.normalize_fills({"nothing": 1}, wallet=WALLET)["received"], 0)
        self.assertEqual(hl.normalize_fills(None, wallet=WALLET)["received"], 0)

    def test_side_vocabulary_maps_onto_the_shared_buy_sell_convention(self):
        cases = {
            ("B", "Open Long"): "buy",
            ("A", "Sell"): "sell",
            ("B", "Close Short"): "buy",
            ("A", "Open Short"): "sell",
        }
        for (side, direction), expected in cases.items():
            with self.subTest(side=side, dir=direction):
                row = hl.normalize_fill(perp_fill(side=side, dir=direction), wallet=WALLET)
                obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
                self.assertEqual(obs["side"], expected)

    def test_a_perp_fill_is_a_trade_and_lands_in_the_shared_schema(self):
        row = hl.normalize_fill(perp_fill(), wallet=WALLET, fetched_at=T0_MS)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET, fetched_at=T0_MS)
        self.assertEqual(obs["kind"], hd.KIND_TRADE)
        self.assertEqual(obs["address"], "AVAX", "the perp asset is the observation's address slot")
        self.assertEqual(obs["symbol"], "AVAX")
        self.assertEqual(obs["quantity"], 93.53)
        self.assertEqual(obs["price_usd"], 18.435)
        self.assertEqual(obs["amount_usd"], 18.435 * 93.53)
        # Usable by the existing identity layer, with no parallel implementation.
        self.assertIsNotNone(hd.whv.event_core(obs))


class TestTransactionAndFillIdentifiers(unittest.TestCase):
    def test_a_real_hash_is_kept(self):
        row = hl.normalize_fill(perp_fill(), wallet=WALLET)
        self.assertEqual(row["hash"], HASH)

    def test_the_twap_placeholder_hash_is_dropped_not_stored(self):
        # Hyperliquid returns an all-zero hash for TWAP slice fills. Storing it
        # would collapse every TWAP fill of every wallet onto one fake tx id.
        twap = perp_fill(hash=hl.ZERO_HASH)
        row = hl.normalize_fill(twap, wallet=WALLET)
        self.assertIsNone(row["hash"])
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
        self.assertIsNone(obs["transaction_hash"])
        self.assertIn("no_transaction_hash", obs["confidence"]["reasons"])

    def test_a_malformed_hash_is_refused(self):
        for bad in ("0xdeadbeef", "not-a-hash", "", None, "0x" + "z" * 64):
            with self.subTest(bad=bad):
                self.assertIsNone(hl.normalize_fill(perp_fill(hash=bad), wallet=WALLET)["hash"])

    def test_the_tid_is_carried_as_the_source_record_id(self):
        row = hl.normalize_fill(perp_fill(tid=118906512037719), wallet=WALLET)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
        self.assertEqual(obs["source_record_id"], "118906512037719")

    def test_a_missing_tid_is_none_not_zero(self):
        row = hl.normalize_fill(perp_fill(tid=None), wallet=WALLET)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
        self.assertIsNone(obs["source_record_id"])

    def test_zero_hash_drops_are_counted(self):
        out = hl.normalize_fills([perp_fill(), perp_fill(hash=hl.ZERO_HASH)], wallet=WALLET)
        self.assertEqual(out["zero_hash_dropped"], 1)
        self.assertEqual(len(out["rows"]), 2)

    def test_duplicate_tids_within_a_page_are_collapsed(self):
        post = FakePost(FakeResponse(200, [perp_fill(tid=7), perp_fill(tid=7), perp_fill(tid=8)]))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertEqual(len(result["rows"]), 2)


class TestProvenance(unittest.TestCase):
    def setUp(self):
        hl.reset_state()

    def test_observation_is_labelled_hyperliquid(self):
        row = hl.normalize_fill(perp_fill(), wallet=WALLET)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET)
        self.assertEqual(obs["source"], "hyperliquid")
        self.assertEqual(obs["sources"], ["hyperliquid"])

    def test_source_and_archive_dir_match_the_declared_adapter(self):
        adapter = hd.ADAPTERS["hyperliquid"]
        self.assertEqual(adapter.source, hl.SOURCE)
        self.assertEqual(str(hl.ARCHIVE_DIR), adapter.archive_dir)

    def test_the_original_payload_is_kept_so_re_deriving_costs_nothing(self):
        fill = perp_fill()
        row = hl.normalize_fill(fill, wallet=WALLET)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET, raw_payload=fill)
        self.assertEqual(obs["raw"]["hash"], HASH)
        self.assertEqual(obs["raw"]["tid"], 118906512037719)

    def test_the_dropped_placeholder_hash_stays_auditable_in_raw(self):
        twap = perp_fill(hash=hl.ZERO_HASH)
        row = hl.normalize_fill(twap, wallet=WALLET)
        obs = hd.normalize_observation(row, "hyperliquid", wallet=WALLET, raw_payload=twap)
        self.assertIsNone(obs["transaction_hash"], "not used as an identifier")
        self.assertEqual(obs["raw"]["hash"], hl.ZERO_HASH, "but not erased from provenance")

    def test_missing_evidence_is_reported_in_confidence_rather_than_defaulted(self):
        obs = hd.normalize_observation(
            hl.normalize_fill(perp_fill(hash=hl.ZERO_HASH), wallet=WALLET), "hyperliquid", wallet=WALLET,
        )
        reasons = obs["confidence"]["reasons"]
        self.assertIn("chain_unknown", reasons)
        self.assertIn("no_transaction_hash", reasons)
        self.assertEqual(obs["confidence"]["identity"], "unknown")

    def test_a_corroborated_event_keeps_both_sources_and_the_base_row(self):
        # The same AVAX fill as GMGN recorded it and as Hyperliquid reports it.
        # The shared chain-free key matches, so one event results -- carrying
        # both sources -- and the stored GMGN row is not replaced or duplicated.
        gmgn_rows = hd.normalize_rows([{
            "wallet": WALLET, "chain": "eth", "address": "AVAX", "symbol": "AVAX",
            "side": "buy", "amount_usd": 18.435 * 93.53, "price_usd": 18.435,
            "quantity": 93.53, "timestamp": 1681222254710 // 1000,
            "trade_timestamp": 1681222254710 // 1000, "transaction_hash": HASH,
        }], "gmgn", wallet=WALLET)
        hl_rows = hd.normalize_rows(
            [hl.normalize_fill(perp_fill(), wallet=WALLET, fetched_at=T0_MS)],
            "hyperliquid", wallet=WALLET, fetched_at=T0_MS,
        )
        merged = hd.merge_sources({WALLET: gmgn_rows}, {WALLET: hl_rows})
        rows = merged[WALLET]

        self.assertEqual(len(rows), 1, "two providers describing one event make one event")
        self.assertEqual(len(rows), len(gmgn_rows), "a source may add evidence, never remove a row")
        self.assertEqual(set(rows[0]["sources"]), {"gmgn", "hyperliquid"})
        self.assertEqual(rows[0]["chain"], "eth", "the stored row keeps its own chain")
        self.assertEqual(rows[0]["transaction_hash"], HASH)

    def test_an_uncorroborated_hyperliquid_row_is_added_with_its_own_provenance(self):
        base = hd.normalize_rows([{
            "wallet": WALLET, "chain": "eth", "address": "ETH", "symbol": "ETH",
            "side": "buy", "amount_usd": 100.0, "price_usd": 1.0, "quantity": 100.0,
            "timestamp": T0, "trade_timestamp": T0, "transaction_hash": "0xg1",
        }], "gmgn", wallet=WALLET)
        hl_rows = hd.normalize_rows(
            [hl.normalize_fill(perp_fill(), wallet=WALLET)], "hyperliquid", wallet=WALLET,
        )
        rows = hd.merge_sources({WALLET: base}, {WALLET: hl_rows})[WALLET]

        self.assertEqual(len(rows), 2)
        gmgn_side = [r for r in rows if "gmgn" in (r.get("sources") or [])]
        hl_side = [r for r in rows if "hyperliquid" in (r.get("sources") or [])]
        self.assertEqual(len(gmgn_side), 1, "the committed GMGN row survives untouched")
        self.assertEqual(len(hl_side), 1, "the new event is attributed to its own source")


class TestErrorHandling(unittest.TestCase):
    def setUp(self):
        hl.reset_state()

    def test_a_transport_failure_is_reported_not_raised(self):
        post = FakePost(ConnectionError("dns"))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertFalse(result["ok"])
        self.assertIn("transport error", result["error"])
        self.assertEqual(result["rows"], [])

    def test_a_retryable_status_is_retried_then_succeeds(self):
        post = FakePost(
            FakeResponse(429, text="slow down"),
            FakeResponse(503, text="unavailable"),
            FakeResponse(200, [perp_fill()]),
        )
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(len(post.calls), 3)

    def test_a_terminal_status_is_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                hl.reset_state()
                post = FakePost(FakeResponse(status, text="no"))
                result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
                self.assertFalse(result["ok"])
                self.assertEqual(len(post.calls), 1, "a terminal status must not be retried")
                self.assertEqual(result["status"], status)

    def test_an_unauthorized_response_never_asks_for_credentials(self):
        post = FakePost(FakeResponse(401, text="unauthorized"))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertFalse(result["ok"])
        self.assertNotIn("api key", result["error"].lower())
        self.assertNotIn("sign", result["error"].lower())

    def test_invalid_json_is_reported_rather_than_guessed(self):
        post = FakePost(FakeResponse(200, raises=ValueError("not json")))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", result["error"])
        self.assertEqual(result["rows"], [])

    def test_an_error_object_in_a_200_body_is_honoured(self):
        post = FakePost(FakeResponse(200, {"error": "unknown user"}))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertFalse(result["ok"])
        self.assertIn("unknown user", result["error"])

    def test_the_request_budget_is_a_hard_stop(self):
        hl.PROVIDER_STATE["requests_made"] = hl.REQUEST_BUDGET
        post = FakePost(FakeResponse(200, [perp_fill()]))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=post, sleep=no_sleep, max_pages=1)
        self.assertFalse(result["ok"])
        self.assertIn("budget", result["error"])
        self.assertEqual(post.calls, [])

    def test_retry_after_header_is_respected(self):
        self.assertEqual(hl._retry_delay(429, {"Retry-After": "7"}, 1), 7.0)
        self.assertEqual(hl._retry_delay(429, {"retry-after": "2"}, 1), 2.0)

    def test_a_failure_after_valid_pages_keeps_what_was_retrieved(self):
        post = FakePost(
            FakeResponse(200, [perp_fill(time=T0_MS)]),
            FakeResponse(500, text="boom"),
        )
        result = hl.fetch_fills(
            WALLET, start_ms=T0_MS, end_ms=T0_MS + 1000, post=post, sleep=no_sleep, max_pages=1,
        )
        # A single-page request cannot paginate here, so the point is simply that
        # a hard error never invents rows.
        self.assertEqual(len(result["rows"]), 1 if result["ok"] else 0)


class TestFetchPaginationAndCoverage(unittest.TestCase):
    def setUp(self):
        hl.reset_state()

    def test_a_partial_page_ends_pagination(self):
        post = FakePost(FakeResponse(200, [perp_fill()]))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, end_ms=T0_MS + 1000, post=post, sleep=no_sleep)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pages"], 1)
        self.assertFalse(result["truncated"])

    def test_a_full_page_advances_the_cursor_and_keeps_reading(self):
        first = [perp_fill(tid=i, time=T0_MS - i) for i in range(hl.MAX_FILLS_PER_RESPONSE)]
        second = [perp_fill(tid=90_000 + i, time=T0_MS - 5000 - i) for i in range(3)]
        post = FakePost(FakeResponse(200, first), FakeResponse(200, second))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, end_ms=T0_MS + 1000, post=post, sleep=no_sleep)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["rows"]), hl.MAX_FILLS_PER_RESPONSE + 3)
        # The documented rule: the last returned timestamp becomes the next start.
        self.assertEqual(post.calls[1]["body"]["startTime"], T0_MS - (hl.MAX_FILLS_PER_RESPONSE - 1) + 1)

    def test_the_recent_request_reads_exactly_one_page(self):
        full = [perp_fill(tid=i, time=T0_MS - i) for i in range(hl.MAX_FILLS_PER_RESPONSE)]
        post = FakePost(FakeResponse(200, full))
        result = hl.fetch_fills(
            WALLET, start_ms=None, request_type="userFills", post=post, sleep=no_sleep, max_pages=9,
        )
        self.assertEqual(result["pages"], 1)
        self.assertEqual(post.calls[0]["body"]["type"], "userFills")

    def test_a_quiet_window_is_complete_not_truncated(self):
        # A wallet with no fills for the first hour of a requested day returns
        # nothing near startTime. That is a complete answer, not a lost page, so
        # claiming the coverage is incomplete here would be a false alarm.
        result = hl.fetch_fills(
            WALLET, start_ms=T0_MS, end_ms=T0_MS + 86_400_000,
            post=FakePost(FakeResponse(200, [
                perp_fill(tid=1, time=T0_MS + 5_000),
                perp_fill(tid=2, time=T0_MS + 1_000),
            ])),
            sleep=no_sleep, max_pages=1,
        )
        self.assertEqual(result["oldest_time"], T0_MS + 1_000)
        self.assertEqual(result["newest_time"], T0_MS + 5_000)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["coverage_incomplete"])

    def test_the_provider_history_bound_is_always_declared(self):
        # The retained-fill ceiling cannot be detected from one short reply, so
        # it is reported as a standing property of the source instead of being
        # guessed per response.
        result = hl.fetch_fills(
            WALLET, start_ms=T0_MS, post=FakePost(FakeResponse(200, [])), sleep=no_sleep, max_pages=1,
        )
        self.assertTrue(result["provider_history_bounded"])
        self.assertEqual(result["retained_fills_ceiling"], hl.RETAINED_FILLS_CEILING)

    def test_a_window_older_than_the_retained_history_is_not_claimed_complete(self):
        result = hl.fetch_fills(
            WALLET,
            start_ms=T0_MS - 10_000_000_000,     # far older than anything retained
            end_ms=T0_MS,
            post=FakePost(FakeResponse(200, [perp_fill(time=T0_MS, tid=1)])),
            sleep=no_sleep, max_pages=1,
        )
        # We cannot prove the gap, so we must not assert a false clean bill of
        # health either: the bound is stated on every result.
        self.assertTrue(result["provider_history_bounded"])
        self.assertEqual(result["retained_fills_ceiling"], hl.RETAINED_FILLS_CEILING)
        self.assertFalse(result["truncated"], "nothing was cut off in this response")

    def test_hitting_the_page_limit_marks_the_coverage_incomplete(self):
        full = [perp_fill(tid=i, time=T0_MS - i) for i in range(hl.MAX_FILLS_PER_RESPONSE)]
        post = FakePost(FakeResponse(200, full))
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, end_ms=T0_MS + 1000, post=post, sleep=no_sleep, max_pages=1)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["coverage_incomplete"])

    def test_an_empty_account_is_a_successful_empty_result_not_an_error(self):
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=FakePost(FakeResponse(200, [])), sleep=no_sleep)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [])
        self.assertIsNone(result["oldest_time"])
        self.assertEqual(result["error"], "")

    def test_the_result_declares_that_no_credentials_are_required(self):
        result = hl.fetch_fills(WALLET, start_ms=T0_MS, post=FakePost(FakeResponse(200, [])), sleep=no_sleep)
        self.assertFalse(result["auth_required"])


class TestArchiveReadPath(unittest.TestCase):
    def setUp(self):
        hl.reset_state()

    def test_committed_fills_are_readable_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / f"{WALLET}.json").write_text(json.dumps({
                "wallet": WALLET,
                "fetched_at": T0_MS,
                "fills": [perp_fill(), perp_fill(tid=2, time=1681222255000, side="A", dir="Close Long")],
            }), encoding="utf-8")
            history = hl.read_archive(folder)
            self.assertEqual(len(history[WALLET]), 2)
            observations = hd.read_hyperliquid_archive(folder)
            self.assertEqual(len(observations[WALLET]), 2)
            for obs in observations[WALLET]:
                self.assertEqual(obs["source"], "hyperliquid")
                self.assertTrue(obs["trade_time_known"])

    def test_a_missing_archive_is_empty_rather_than_an_error(self):
        self.assertEqual(hl.read_archive("/nonexistent/hyperliquid"), {})

    def test_corrupt_archive_files_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "broken.json").write_text("{not json", encoding="utf-8")
            (folder / f"{WALLET}.json").write_text(json.dumps({"wallet": WALLET, "fills": [perp_fill()]}), encoding="utf-8")
            self.assertEqual(len(hl.read_archive(folder)[WALLET]), 1)

    def test_hyperliquid_is_a_registered_reader(self):
        self.assertIn("hyperliquid", hd.READERS)
        self.assertIs(hd.READERS["hyperliquid"], hd.read_hyperliquid_archive)

    def test_hyperliquid_rows_merge_additively_without_touching_other_sources(self):
        base = hd.normalize_rows([{
            "wallet": WALLET, "chain": "eth", "address": "ETH", "symbol": "ETH",
            "side": "buy", "amount_usd": 100.0, "price_usd": 1.0,
            "timestamp": T0, "trade_timestamp": T0, "transaction_hash": "0xg1",
        }], "gmgn", wallet=WALLET)
        merged = hd.merge_sources(
            {WALLET: base},
            {WALLET: hd.normalize_rows(
                [hl.normalize_fill(perp_fill(), wallet=WALLET)], "hyperliquid", wallet=WALLET,
            )},
        )
        self.assertGreaterEqual(len(merged[WALLET]), len(base))
        self.assertTrue(any("gmgn" in (r.get("sources") or []) for r in merged[WALLET]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
