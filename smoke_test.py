"""Deterministic smoke tests for the wallet-first Fil Before Pump pipeline."""
from scanner import is_primary_crypto_asset, wallet_conviction_signals
from scanner import apply_wallet_radar_signals, apply_wallet_cluster_signals
from trade_readiness import build_trade_plan
from risk_engine import evaluate, filter_plans
import wallet_intel_gate as wig

def test_asset_exclusions():
    assert not is_primary_crypto_asset({"symbol": "PAXG", "name": "PAX Gold"})
    assert not is_primary_crypto_asset({"symbol": "XAUT", "name": "Tether Gold"})
    assert not is_primary_crypto_asset({"symbol": "USDT", "name": "Tether USDt"})
    assert not is_primary_crypto_asset({"symbol": "AAPLX", "name": "Tokenized Apple Stock"})
    assert is_primary_crypto_asset({"symbol": "LINK", "name": "Chainlink"})

def test_trade_plan_is_deterministic_and_paper_only():
    plan = build_trade_plan({
        "symbol": "LINK",
        "rank": 13,
        "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 18,
        "wallet_unique_active_count": 2,
        "wallet_unique_proven_count": 1,
        "wallet_unique_shared_count": 1,
        "wallet_exit_pressure": 10,
        "fil_confluence_score": 75,
        "ch24": 3,
        "vol_changes": {"1d": 5, "2d": 2},
    })
    assert plan["state"] == "PAPER_READY"
    assert "risk" in plan
    assert plan["risk"]["leverage_cap"] == 3


def test_watch_plans_never_enter_paper_execution():
    watch = build_trade_plan({
        "symbol": "TEST", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 18, "wallet_unique_active_count": 2,
        "wallet_unique_proven_count": 0, "wallet_unique_shared_count": 1,
        "wallet_exit_pressure": 10, "fil_confluence_score": 65,
        "ch24": 3, "vol_changes": {"1d": 5, "2d": 2},
    })
    assert watch["state"] == "WATCH_HIGH_CONVICTION"
    assert not evaluate(watch)["approved"]
    assert filter_plans([watch]) == []


def test_risk_gate_blocks_unsafe_paper_plan():
    safe = build_trade_plan({
        "symbol": "LINK", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 18, "wallet_unique_active_count": 2,
        "wallet_unique_proven_count": 1, "wallet_unique_shared_count": 1,
        "wallet_exit_pressure": 10, "fil_confluence_score": 75,
        "ch24": 3, "vol_changes": {"1d": 5, "2d": 2},
    })
    assert evaluate(safe)["approved"]
    unsafe = dict(safe)
    unsafe["wallet_exit_pressure"] = 65
    assert not evaluate(unsafe)["approved"]
    assert filter_plans([safe, unsafe]) == [safe]


# ---------------------------------------------------------------------------
# Long-term wallet intelligence -> decision gate integration.
# The long-term signal-wallet profile layer and the bounded paper calibration
# memory must reach the gate, stay deduplicated, bounded and read-only.
# ---------------------------------------------------------------------------

WALLET = "0xabc123"


def _profile(**overrides):
    base = {
        "wallet": WALLET,
        "profile_type": "SIGNAL_WALLET_CANDIDATE",
        "active_assets": ["FOO"],
        "forward_14d_attempts": 40,
        "forward_14d_hit_rate": 40.0,
        "pre_pump_24h_10pct_rate": 10.0,
        "pre_pump_observations": 40,
        "quality_score": 70.0,
        "quality_tier": "B",
        "active_asset_count": 2,
        "qualified_buy_usd": 250_000.0,
        "entry_timing_windows": {},
    }
    base.update(overrides)
    return base


def _rows(symbol="FOO", profile=None):
    profiles = {WALLET: profile or _profile()}
    index = wig.build_profile_index(profiles)
    return wig.rows_for_symbol(symbol, index.get(symbol))


def test_long_term_proof_requires_depth_and_outcome():
    assert wig.is_long_term_proven(_profile())
    # Too shallow to trust, however good the outcome looks.
    assert not wig.is_long_term_proven(
        _profile(forward_14d_attempts=2, pre_pump_observations=2, forward_14d_hit_rate=100.0)
    )
    # Deep enough but with no observed expansion at all.
    assert not wig.is_long_term_proven(
        _profile(forward_14d_hit_rate=0.0, pre_pump_24h_10pct_rate=0.0)
    )
    # The weaker pre-pump bound alone is still accepted.
    assert wig.is_long_term_proven(
        _profile(forward_14d_hit_rate=0.0, pre_pump_24h_10pct_rate=5.0)
    )


def test_profile_rows_expose_identity_and_evidence():
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["wallet"] == WALLET
    assert row["identity"] == WALLET
    assert row["long_term_proven"] is True
    assert row["active"] is True
    assert 0.0 < row["reliability"] <= 1.0
    assert row["pre_pump_proof_attempts"] == 40
    assert _rows(symbol="NOPE") == []


def test_profile_component_is_bounded():
    assert wig.profile_component([]) == 0.0
    assert wig.profile_component(_rows()) == wig.PROFILE_WALLET_WEIGHT
    # A distribution state must not earn a profile contribution.
    exited = _rows(profile=_profile(radar_position_states={"FOO": "exited"}))
    assert wig.profile_component(exited) == 0.0
    # Many proven wallets stay inside the published cap.
    many = [dict(_rows()[0], wallet=f"0x{i}", long_term_proven=True, reliability=1.0) for i in range(50)]
    assert wig.profile_component(many) == wig.PROFILE_SCORE_CAP


def test_wallet_conviction_never_double_counts_one_wallet():
    radar = {
        WALLET: {
            "wallet": WALLET,
            "chain": "",
            "assets": {"FOO": {"buy_usd": 9000, "buys": 1}},
            "position_states": {"FOO": "holding"},
            "performance": {"observed_opportunities": 5, "pre_pump_win_rate": 70.0},
            "qualified_buy_usd": 9000,
        }
    }
    rows = _rows()

    radar_only = {"symbol": "FOO", "score": 0.0, "reasons": []}
    apply_wallet_radar_signals(radar_only, radar)
    wallet_conviction_signals(radar_only, [], {})

    profile_only = {"symbol": "FOO", "score": 0.0, "reasons": []}
    wallet_conviction_signals(profile_only, rows, {})

    merged = {"symbol": "FOO", "score": 0.0, "reasons": []}
    apply_wallet_radar_signals(merged, radar)
    wallet_conviction_signals(merged, rows, {})

    # The same wallet seen by Radar and by the profile layer is one wallet.
    assert merged["wallet_unique_active_count"] == 1
    assert [w["wallet"] for w in merged["wallet_conviction_wallets"]] == [WALLET]
    assert set(merged["wallet_conviction_wallets"][0]["sources"]) == {"radar", "profile"}
    assert merged["wallet_conviction_score"] == max(
        radar_only["wallet_conviction_score"], profile_only["wallet_conviction_score"]
    )
    assert merged["wallet_conviction_score"] < (
        radar_only["wallet_conviction_score"] + profile_only["wallet_conviction_score"]
    )


def test_conviction_score_stays_inside_its_cap():
    profiles = {}
    rows = []
    for i in range(60):
        address = f"0x{i:040x}"
        profiles[address] = _profile(wallet=address, forward_14d_hit_rate=90.0)
        rows.append(_rows(profile=profiles[address])[0] | {"wallet": address})
    result = {"symbol": "FOO", "score": 0.0, "reasons": []}
    wallet_conviction_signals(result, rows, {})
    assert result["wallet_conviction_score"] == 30.0
    assert result["wallet_profile_score"] == wig.PROFILE_SCORE_CAP
    assert result["wallet_longterm_proven_count"] == 60


def test_calibration_requires_a_measurable_paper_sample():
    signature = wig.calibration_signature(True, False, True)
    assert wig.memory_key(signature) == "proven_wallet=1|shared_wallet=0|high_conviction=1"

    assert wig.lookup_calibration({}, signature) == (0.0, "UNAVAILABLE")
    assert wig.lookup_calibration({"mode": "PAPER_ONLY", "orders_enabled": True}, signature) == (0.0, "UNAVAILABLE")
    assert wig.lookup_calibration({"mode": "LIVE", "orders_enabled": False}, signature) == (0.0, "UNAVAILABLE")

    thin = {
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "max_calibration_bonus": 5.0,
        "memory": [{
            "evidence_signature": signature,
            "sample_status": "INSUFFICIENT_SAMPLE",
            "calibration_bonus": 4.0,
        }],
    }
    assert wig.lookup_calibration(thin, signature) == (0.0, "INSUFFICIENT_SAMPLE")

    measurable = {
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "max_calibration_bonus": 5.0,
        "memory": [{
            "evidence_signature": signature,
            "sample_status": "MEASURABLE",
            "calibration_bonus": 3.5,
        }],
    }
    bonus, status = wig.lookup_calibration(measurable, signature)
    assert (bonus, status) == (3.5, "MEASURABLE")
    assert wig.lookup_calibration(measurable, wig.calibration_signature(False, False, False)) == (0.0, "NO_HISTORY")

    # The published cap is always honoured, even by a corrupted artifact.
    measurable["memory"][0]["calibration_bonus"] = 99.0
    assert wig.lookup_calibration(measurable, signature)[0] == wig.DEFAULT_MAX_CALIBRATION_BONUS
    measurable["max_calibration_bonus"] = 500.0
    assert wig.lookup_calibration(measurable, signature)[0] == wig.DEFAULT_MAX_CALIBRATION_BONUS


def _calibration_memory(signature, bonus):
    return {
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "max_calibration_bonus": 5.0,
        "memory": [{
            "evidence_signature": signature,
            "sample_status": "MEASURABLE",
            "calibration_bonus": bonus,
        }],
    }


def test_conviction_applies_bounded_calibration():
    # Three proven long-term wallets plus shared cluster wallets: proven, shared
    # and a pre-calibration conviction above the documented 18 threshold.
    profiles = {}
    rows = []
    for i in range(3):
        address = f"0x{i:040x}"
        profiles[address] = _profile(wallet=address, forward_14d_hit_rate=90.0)
        rows.append(_rows(profile=profiles[address])[0] | {"wallet": address})
    clusters = {
        "assets": {"FOO": {"wallet_count": 2, "wallets": [
            {"wallet": "0xaaa", "chain": "", "status": "holding"},
            {"wallet": "0xbbb", "chain": "", "status": "holding"},
        ]}}
    }
    result = {"symbol": "FOO", "score": 0.0, "reasons": []}
    apply_wallet_cluster_signals(result, clusters)
    wallet_conviction_signals(result, rows, _calibration_memory(
        wig.calibration_signature(True, True, True), -4.0
    ))

    assert result["wallet_conviction_score_pre_calibration"] >= 18
    assert result["wallet_calibration_bonus"] == -4.0
    assert result["wallet_calibration_status"] == "MEASURABLE"
    # The prior is bounded and can never push the conviction below zero.
    assert result["wallet_conviction_score"] == round(
        result["wallet_conviction_score_pre_calibration"] - 4.0, 1
    )
    assert 0.0 <= result["wallet_conviction_score"] <= 30.0

    # An adverse prior on a candidate with no wallet evidence floors at zero.
    empty = {"symbol": "BAR", "score": 0.0, "reasons": []}
    wallet_conviction_signals(empty, [], _calibration_memory(
        wig.calibration_signature(False, False, False), -5.0
    ))
    assert empty["wallet_calibration_bonus"] == -5.0
    assert empty["wallet_conviction_score"] == 0.0


def test_missing_artifacts_degrade_to_no_evidence():
    result = {"symbol": "FOO", "score": 0.0, "reasons": []}
    wallet_conviction_signals(result, None, None)
    assert result["wallet_conviction_score"] == 0.0
    assert result["wallet_profile_score"] == 0.0
    assert result["wallet_longterm_proven_count"] == 0
    assert result["wallet_calibration_bonus"] == 0.0
    assert result["wallet_calibration_status"] == "UNAVAILABLE"


def test_long_term_intel_protects_candidate_but_never_rejects():
    assert wig.meaningful_wallet_intel({"wallet_longterm_proven_count": 1, "wallet_unique_active_count": 1})
    assert wig.meaningful_wallet_intel({"wallet_profile_score": wig.PROFILE_WALLET_WEIGHT * 3})
    # A proven record without any active wallet is not enough.
    assert not wig.meaningful_wallet_intel({"wallet_longterm_proven_count": 1, "wallet_unique_active_count": 0})
    assert not wig.meaningful_wallet_intel({})


def test_readiness_plan_reports_long_term_evidence():
    plan = build_trade_plan({
        "symbol": "LINK", "rank": 13, "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 24, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "wallet_unique_shared_count": 1,
        "wallet_longterm_proven_count": 2, "wallet_profile_score": 4.0,
        "wallet_calibration_bonus": 0.0, "wallet_calibration_status": "NO_HISTORY",
        "wallet_conviction_score_pre_calibration": 26.0,
        "wallet_exit_pressure": 10, "fil_confluence_score": 75,
        "ch24": 3, "vol_changes": {"1d": 5, "2d": 2},
    })
    assert plan["wallet_longterm_proven"] == 2
    assert plan["wallet_profile_score"] == 4.0
    assert plan["wallet_calibration_status"] == "NO_HISTORY"
    # Calibration provenance stays auditable on every persisted plan.
    assert plan["wallet_conviction_pre_calibration"] == 26.0
    assert any("long-term proven" in r for r in plan["reasons"])
    decision = evaluate(plan)
    assert decision["wallet_intel"]["long_term_proven"] == 2
    assert decision["mode"] == "PAPER_ONLY"


def test_adverse_measurable_calibration_blocks_paper_plan():
    plan = build_trade_plan({
        "symbol": "LINK", "rank": 13, "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 24, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "wallet_unique_shared_count": 1,
        "wallet_longterm_proven_count": 1, "wallet_profile_score": 2.0,
        "wallet_calibration_bonus": -5.0, "wallet_calibration_status": "MEASURABLE",
        "wallet_exit_pressure": 10, "fil_confluence_score": 75,
        "ch24": 3, "vol_changes": {"1d": 5, "2d": 2},
    })
    assert plan["state"] == "PAPER_READY"
    assert not evaluate(plan)["approved"]
    assert "adverse_paper_calibration" in evaluate(plan)["blockers"]
    assert filter_plans([plan]) == []
    # The same plan is acceptable while the sample is not measurable.
    thin = dict(plan, wallet_calibration_bonus=0.0, wallet_calibration_status="INSUFFICIENT_SAMPLE")
    assert evaluate(thin)["approved"]


def test_wallet_intel_layer_never_enables_execution():
    profiles = wig.load_signal_profiles()
    memory = wig.load_performance_memory()
    # A read-only artifact is the only kind this layer will consume.
    if profiles:
        assert isinstance(profiles, dict)
    if memory:
        assert memory.get("orders_enabled") is False
        assert memory.get("mode") == "PAPER_ONLY"
    for plan in filter_plans([build_trade_plan({
        "symbol": "LINK", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 24, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "wallet_unique_shared_count": 1,
        "wallet_longterm_proven_count": 2, "wallet_profile_score": 4.0,
        "wallet_exit_pressure": 10, "fil_confluence_score": 75,
        "ch24": 3, "vol_changes": {"1d": 5, "2d": 2},
    })]):
        assert evaluate(plan)["mode"] == "PAPER_ONLY"
        assert not evaluate(plan).get("orders_enabled")


if __name__ == "__main__":
    test_asset_exclusions()
    test_trade_plan_is_deterministic_and_paper_only()
    test_watch_plans_never_enter_paper_execution()
    test_risk_gate_blocks_unsafe_paper_plan()
    test_long_term_proof_requires_depth_and_outcome()
    test_profile_rows_expose_identity_and_evidence()
    test_profile_component_is_bounded()
    test_wallet_conviction_never_double_counts_one_wallet()
    test_conviction_score_stays_inside_its_cap()
    test_calibration_requires_a_measurable_paper_sample()
    test_conviction_applies_bounded_calibration()
    test_missing_artifacts_degrade_to_no_evidence()
    test_long_term_intel_protects_candidate_but_never_rejects()
    test_readiness_plan_reports_long_term_evidence()
    test_adverse_measurable_calibration_blocks_paper_plan()
    test_wallet_intel_layer_never_enables_execution()
    print("Fil Before Pump smoke tests: PASS")
