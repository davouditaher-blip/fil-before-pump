"""Deterministic smoke tests for the wallet-first Fil Before Pump pipeline."""
import scanner
from scanner import is_primary_crypto_asset, wallet_conviction_signals
from scanner import apply_wallet_radar_signals, apply_wallet_cluster_signals
from scanner import (
    COVALENT_CHAIN_BY_PLATFORM,
    FLOW_PROVIDER_GMGN,
    FLOW_PROVIDER_SOLSCAN,
    FLOW_SEMANTICS_SMART_MONEY,
    FLOW_SEMANTICS_TOKEN_FLOW,
    GMGN_FLOW_WINDOW_SECONDS,
    PROJECT_LAYER_MISSING_REASONS,
    _project_evidence_strength,
    apply_wallet_signals,
    coin_contracts,
    gmgn_flow_7d,
    project_layer_missing_reason,
)
from trade_readiness import build_trade_plan
from risk_engine import evaluate, filter_plans
from confluence_engine import (
    BROAD_BUYER_FLOOR,
    BUCKET_CAPS,
    FUTURES_VOLUME_FLOOR_USD,
    MARKET_CAP_FLOOR_USD,
    PAPER_READY_MIN_CONFLUENCE,
    SCALE_MAX,
    _caps_reachable,
    _market,
    _project,
    _safety,
    _volume,
    build_confluence,
)
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

def test_confluence_caps_are_reachable():
    assert _caps_reachable()
    best = build_confluence({
        "wallet_conviction_score": 30.0,
        "wallet": {
            "buy_sell_ratio_7d": 1.8,
            "buyers_7d": 50,
            "sellers_7d": 10,
            "top5_holder_pct": 20.0,
            "top20_holder_pct": 30.0,
        },
        "vol_changes": {"1d": 30, "2d": 25, "3d": 5, "7d": 5},
        "current_volume": FUTURES_VOLUME_FLOOR_USD * 10,
        "quote": {"market_cap": MARKET_CAP_FLOOR_USD * 10},
        "ch24": 0.0,
        "futures_source": "binance",
    }, {"btc24": 3.0, "eth24": 3.0, "btc7": 4.0, "eth7": 4.0})
    assert best["fil_confluence_score"] == SCALE_MAX
    assert best["fil_confluence_scale"]["max_score"] == SCALE_MAX
    assert best["fil_confluence_scale"]["paper_ready_threshold"] == PAPER_READY_MIN_CONFLUENCE
    assert best["fil_confluence_components"]["project"] == BUCKET_CAPS["project"]
    assert best["fil_confluence_components"]["safety"] == BUCKET_CAPS["safety"]

def test_evidence_coverage_is_reported():
    """A candidate with no project-layer data must be distinguishable from a weak one."""
    with_layer = build_confluence({
        "wallet_conviction_score": 20.0,
        "wallet": {"buy_sell_ratio_7d": 1.6, "buyers_7d": 40, "sellers_7d": 5,
                   "top5_holder_pct": 20, "top20_holder_pct": 30},
        "vol_changes": {"1d": 5, "2d": 2}, "current_volume": 5e7,
        "quote": {"market_cap": 5e9}, "ch24": 2.0,
    }, {"btc24": 1.0, "eth24": 1.0, "btc7": 1.0, "eth7": 1.0})
    without_layer = build_confluence({
        "wallet_conviction_score": 20.0, "wallet": {},
        "vol_changes": {"1d": 5, "2d": 2}, "current_volume": 5e7,
        "quote": {"market_cap": 5e9}, "ch24": 2.0,
    }, {"btc24": 1.0, "eth24": 1.0, "btc7": 1.0, "eth7": 1.0})
    assert with_layer["fil_confluence_scale"]["project_layer_present"] is True
    assert without_layer["fil_confluence_scale"]["project_layer_present"] is False
    assert without_layer["project_intelligence_score"] == 0.0
    # Equal wallet evidence, so the gap is honest missing data, not a penalty.
    assert with_layer["fil_confluence_score"] > without_layer["fil_confluence_score"]
    for result in (with_layer, without_layer):
        coverage = result["fil_confluence_scale"]["evidence_coverage"]
        assert set(coverage) == {"wallet", "project", "volume", "market", "safety"}

def test_project_bucket_scores_breadth():
    score, reasons, _ = _project({
        "wallet": {
            "buy_sell_ratio_7d": 1.6, "buyers_7d": BROAD_BUYER_FLOOR, "sellers_7d": 5,
            "top5_holder_pct": 20, "top20_holder_pct": 30,
        }
    })
    assert score == BUCKET_CAPS["project"]
    assert any("broad buyer participation" in r for r in reasons)

def test_volume_bucket_scores_fresh_acceleration():
    accelerating, reasons, evidence = _volume({
        "vol_changes": {"1d": 8, "2d": 3, "3d": 1, "7d": 1}, "current_volume": 1e8,
    })
    fading, fading_reasons, fading_evidence = _volume({
        "vol_changes": {"1d": 3, "2d": 8, "3d": 1, "7d": 1}, "current_volume": 1e8,
    })
    assert accelerating > fading
    assert evidence["fresh_acceleration"] is True
    assert fading_evidence["fresh_acceleration"] is False
    assert any("accelerating" in r for r in reasons)
    assert not any("accelerating" in r for r in fading_reasons)

def test_market_bucket_requires_breadth_not_just_average():
    broad, broad_reasons, _ = _market({"btc24": 5.0, "eth24": -4.0, "btc7": 1.0, "eth7": 1.0})
    both, both_reasons, _ = _market({"btc24": 3.0, "eth24": 3.0, "btc7": 1.0, "eth7": 1.0})
    # Same positive 24h average, but only the broad pair earns the breadth points.
    assert broad == 8.0
    assert both == 10.0
    assert not any("both positive" in r for r in broad_reasons)
    assert any("both positive" in r for r in both_reasons)

def test_safety_bucket_scores_execution_floors():
    deep, _, deep_evidence = _safety({
        "quote": {"market_cap": MARKET_CAP_FLOOR_USD * 2}, "current_volume": FUTURES_VOLUME_FLOOR_USD * 2,
        "ch24": 0.0, "futures_source": "binance",
    })
    thin, thin_reasons, thin_evidence = _safety({
        "quote": {"market_cap": MARKET_CAP_FLOOR_USD / 10}, "current_volume": 1000.0, "ch24": 0.0,
    })
    assert deep == BUCKET_CAPS["safety"]
    assert deep_evidence["market_cap_above_execution_floor"] is True
    assert thin_evidence["market_cap_above_execution_floor"] is False
    assert any("execution floor" in r for r in thin_reasons)

def test_paper_ready_threshold_is_single_sourced():
    assert PAPER_READY_MIN_CONFLUENCE == 70.0
    assert SCALE_MAX == 100.0
    plan = build_trade_plan({
        "symbol": "LINK", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 24, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "wallet_unique_shared_count": 1,
        "vol_changes": {"1d": 5, "2d": 2}, "wallet_exit_pressure": 10,
        "fil_confluence_score": PAPER_READY_MIN_CONFLUENCE,
    })
    assert plan["state"] == "PAPER_READY"
    assert plan["fil_confluence_max_score"] == SCALE_MAX
    assert plan["fil_confluence_paper_ready_threshold"] == PAPER_READY_MIN_CONFLUENCE
    below = build_trade_plan({
        "symbol": "LINK", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 24, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "vol_changes": {"1d": 5, "2d": 2},
        "wallet_exit_pressure": 10, "fil_confluence_score": PAPER_READY_MIN_CONFLUENCE - 0.1,
    })
    assert below["state"] != "PAPER_READY"

def test_wallet_cap_and_technical_context_are_preserved():
    """The reweighting must not buy reachability by diluting wallet evidence."""
    assert BUCKET_CAPS["wallet"] == 30.0
    result = build_confluence({
        "wallet_conviction_score": 30.0,
        "wallet": {"buy_sell_ratio_7d": 1.6, "buyers_7d": 50, "sellers_7d": 5,
                   "top5_holder_pct": 20, "top20_holder_pct": 30},
        "vol_changes": {"1d": 30, "2d": 25, "3d": 5, "7d": 5},
        "current_volume": 1e9, "quote": {"market_cap": 5e9},
        "ch24": 40.0, "futures_source": "binance", "tech": {"rsi": 78},
    }, {"btc24": 3.0, "eth24": 3.0, "btc7": 4.0, "eth7": 4.0})
    # A 40% 24h extension and an overbought RSI cost safety points, never reject.
    assert result["fil_confluence_components"]["wallet"] == 30.0
    assert result["fil_confluence_components"]["safety"] < BUCKET_CAPS["safety"]
    assert "tech" not in result.get("fil_confluence_components", {})
    plan = build_trade_plan({
        "symbol": "LINK", "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 26, "wallet_unique_active_count": 3,
        "wallet_unique_proven_count": 2, "vol_changes": {"1d": 5, "2d": 2},
        "wallet_exit_pressure": 5, "ch24": 40,
        "fil_confluence_score": PAPER_READY_MIN_CONFLUENCE,
    })
    assert plan["state"] == "PAPER_READY"
    assert "24h move > 15%: late-entry caution" in " ".join(plan["reasons"])


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


_DAY = 24 * 60 * 60
_NOW = 1_800_000_000


def _trade(symbol, side, usd, age_days, ts_offset=0):
    return {
        "symbol": symbol,
        "side": side,
        "amount_usd": usd,
        "trade_timestamp": _NOW - int(age_days * _DAY) + ts_offset,
    }


def test_gmgn_flow_aggregates_distinct_wallets_over_seven_days():
    assert GMGN_FLOW_WINDOW_SECONDS == 7 * 24 * 60 * 60
    history = {
        # One wallet trading twice must still count as a single buyer.
        "0xaaa": [_trade("NEAR", "buy", 1000, 1), _trade("NEAR", "buy", 500, 2)],
        "0xbbb": [_trade("NEAR", "buy", 500, 3), _trade("NEAR", "sell", 250, 1)],
        "0xccc": [_trade("NEAR", "sell", 750, 4)],
        # Beyond the window, so it must not move the ratio at all.
        "0xddd": [_trade("NEAR", "buy", 99_000, 8)],
        # A different asset entirely.
        "0xeee": [_trade("UNI", "buy", 50_000, 1)],
    }
    flow = gmgn_flow_7d(history, "NEAR", _NOW)
    assert flow["buyers_7d"] == 2
    assert flow["sellers_7d"] == 2
    # 2000 bought against 1000 sold. If the 8-day-old row leaked in this would
    # be 101, so this single assertion also pins the window length.
    assert flow["buy_sell_ratio_7d"] == 2.0
    assert flow["flow_provider"] == FLOW_PROVIDER_GMGN
    assert flow["flow_semantics"] == FLOW_SEMANTICS_SMART_MONEY


def test_gmgn_flow_window_is_inclusive_at_exactly_seven_days():
    inside = {"0xeee": [_trade("EDGE", "buy", 100, 7)]}
    assert gmgn_flow_7d(inside, "EDGE", _NOW)["buyers_7d"] == 1
    # One second older than the window and the trade is gone.
    outside = {"0xeee": [_trade("EDGE", "buy", 100, 7, -1)]}
    assert gmgn_flow_7d(outside, "EDGE", _NOW) == {}


def test_gmgn_sell_only_flow_scores_below_equivalent_buying():
    sell_flow = gmgn_flow_7d({
        "0x1": [_trade("XYZ", "sell", 900, 1)],
        "0x2": [_trade("XYZ", "sell", 100, 2)],
    }, "XYZ", _NOW)
    assert sell_flow["buyers_7d"] == 0
    assert sell_flow["sellers_7d"] == 2
    # Nothing was bought at all, so the ratio is a measured zero rather than
    # missing data, and the project bucket must be able to punish that.
    assert sell_flow["buy_sell_ratio_7d"] == 0.0

    concentration = {"top5_holder_pct": 10, "top20_holder_pct": 20}
    sell_score, sell_reasons, _ = _project({"wallet": dict(sell_flow, **concentration)})
    buy_score, _, _ = _project({"wallet": dict(
        concentration, buy_sell_ratio_7d=2.0, buyers_7d=30, sellers_7d=2)})
    assert any("sellers exceed buyers" in r for r in sell_reasons)
    # Concentration-only EVM evidence could never separate these two cases.
    assert sell_score < buy_score


def test_evm_project_score_can_exceed_the_old_concentration_ceiling():
    # Exactly the state an EVM asset was stuck in: concentration but no flow.
    thin = {"top5_holder_pct": 20, "top20_holder_pct": 30,
            "buy_sell_ratio_7d": None, "buyers_7d": 0, "sellers_7d": 0}
    thin_score, _, _ = _project({"wallet": thin})
    assert thin_score == 7

    # The same asset once provider-labelled GMGN flow is attached.
    rich = dict(thin, buy_sell_ratio_7d=2.0, buyers_7d=40, sellers_7d=10,
                flow_provider=FLOW_PROVIDER_GMGN,
                flow_semantics=FLOW_SEMANTICS_SMART_MONEY)
    rich_score, _, evidence = _project({"wallet": rich})
    assert rich_score > 7
    assert rich_score <= BUCKET_CAPS["project"]
    assert evidence["flow_provider"] == FLOW_PROVIDER_GMGN
    assert evidence["flow_semantics"] == FLOW_SEMANTICS_SMART_MONEY


def test_solscan_flow_is_never_replaced_by_gmgn_flow():
    solscan = {
        "chain": "solana", "symbol": "PENGU",
        "top5_holder_pct": 5, "top20_holder_pct": 10,
        "buy_sell_ratio_7d": 1.8, "buyers_7d": 120, "sellers_7d": 30,
        "flow_provider": FLOW_PROVIDER_SOLSCAN,
        "flow_semantics": FLOW_SEMANTICS_TOKEN_FLOW,
    }
    goldrush = {
        "chain": "eth-mainnet", "symbol": "PENGU",
        "top5_holder_pct": 5, "top20_holder_pct": 10,
        "buy_sell_ratio_7d": 4.0, "buyers_7d": 40, "sellers_7d": 2,
        "flow_provider": FLOW_PROVIDER_GMGN,
        "flow_semantics": FLOW_SEMANTICS_SMART_MONEY,
        "provider": "GoldRush",
    }
    # Solscan is collected first in the scan, so this is the real order.
    forward = {"score": 0.0, "reasons": []}
    apply_wallet_signals(forward, solscan)
    apply_wallet_signals(forward, goldrush)
    assert forward["wallet"]["flow_provider"] == FLOW_PROVIDER_SOLSCAN
    assert forward["wallet"]["buyers_7d"] == 120
    assert forward["wallet"]["flow_semantics"] == FLOW_SEMANTICS_TOKEN_FLOW

    # Order must not decide the winner either.
    reverse = {"score": 0.0, "reasons": []}
    apply_wallet_signals(reverse, goldrush)
    apply_wallet_signals(reverse, solscan)
    assert reverse["wallet"]["flow_provider"] == FLOW_PROVIDER_SOLSCAN


def test_richest_project_layer_survives_any_provider_order():
    thin = {
        "chain": "eth-mainnet", "symbol": "AAVE", "holders": [],
        "top5_holder_pct": 8, "top20_holder_pct": 12,
        "buy_sell_ratio_7d": None, "buyers_7d": 0, "sellers_7d": 0,
        "provider": "GoldRush",
    }
    rich = dict(thin, buy_sell_ratio_7d=1.9, buyers_7d=55, sellers_7d=21,
                flow_provider=FLOW_PROVIDER_GMGN,
                flow_semantics=FLOW_SEMANTICS_SMART_MONEY)
    assert _project_evidence_strength(rich) > _project_evidence_strength(thin)

    for layers in ([thin, rich], [rich, thin]):
        result = {"score": 0.0, "reasons": []}
        for layer in layers:
            apply_wallet_signals(result, layer)
        assert result["wallet"] is rich


def test_polygon_maps_to_a_real_covalent_chain():
    coin = {"symbol": "POL", "platform": {"name": "Polygon", "token_address": "0xabc"}}
    assert coin_contracts(coin) == [("polygon-mainnet", "0xabc")]
    # "matic-mainnet" is not a Covalent chain; it silently emptied the holder
    # map and cost every Polygon candidate its project layer.
    assert COVALENT_CHAIN_BY_PLATFORM["polygon"] == "polygon-mainnet"
    assert "matic-mainnet" not in COVALENT_CHAIN_BY_PLATFORM.values()


def test_unsupported_chain_is_reported_not_hidden():
    coin = {"symbol": "WEIRD", "platform": {"name": "Some New Chain", "token_address": "0xdef"}}
    assert coin_contracts(coin) == []
    reason = project_layer_missing_reason(coin)
    assert reason == "unsupported_chain"
    assert reason in PROJECT_LAYER_MISSING_REASONS


def test_missing_contract_is_classified():
    # No platform object at all.
    assert project_layer_missing_reason({"symbol": "AAA"}) == "no_contract_mapping"
    # Platform present but carrying no contract address.
    assert project_layer_missing_reason({"symbol": "AAA", "platform": {}}) == "no_token_address"
    assert project_layer_missing_reason(
        {"symbol": "AAA", "platform": {"name": "Ethereum"}}) == "no_token_address"
    assert coin_contracts({"symbol": "AAA", "platform": {}}) == []


def test_resolved_contract_with_empty_provider_response_is_classified():
    coin = {"symbol": "GHOST", "platform": {"name": "Ethereum", "token_address": "0xfeed"}}
    assert coin_contracts(coin) == [("eth-mainnet", "0xfeed")]
    # The contract resolved, so only the provider returning nothing can be why
    # there is still no project layer.
    assert project_layer_missing_reason(coin) == "provider_empty_response"
    assert "provider_empty_response" in PROJECT_LAYER_MISSING_REASONS


def test_missing_keys_degrade_without_inventing_flow():
    original = scanner.GOLDRUSH_API_KEY
    try:
        scanner.GOLDRUSH_API_KEY = ""
        # No key, no layer, and above all no fabricated flow evidence.
        assert scanner.goldrush_wallet_layer({"symbol": "UNI"}) == {}
    finally:
        scanner.GOLDRUSH_API_KEY = original

    # An absent history, an unknown symbol and an unlabelled row all yield no
    # flow rather than zeros that would read as measured evidence.
    assert gmgn_flow_7d({}, "NEAR", _NOW) == {}
    assert gmgn_flow_7d({"0x1": [_trade("NEAR", "buy", 10, 1)]}, "OTHER", _NOW) == {}
    assert gmgn_flow_7d(
        {"0x1": [{"symbol": "NEAR", "amount_usd": 10, "trade_timestamp": _NOW}]}, "NEAR", _NOW) == {}


class _StubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise scanner.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _StubSession:
    """Replays a fixed sequence of responses and counts the calls made."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_goldrush_retries_a_transient_failure_before_giving_up():
    original_key = scanner.GOLDRUSH_API_KEY
    original_session = scanner.session
    original_sleep = scanner.time.sleep
    try:
        scanner.GOLDRUSH_API_KEY = "test"
        scanner.time.sleep = lambda _s: None

        # Throttled once, then answered. A single retry must recover the data
        # instead of recording a false "provider returned nothing" gap.
        stub = _StubSession([
            _StubResponse(429),
            _StubResponse(200, {"data": {"items": [{"address": "0x1", "percentage": "5"}]}}),
        ])
        scanner.session = stub
        items = scanner.goldrush_get("/eth-mainnet/tokens/0xabc/token_holders_v2/")
        assert stub.calls == 2
        assert items == [{"address": "0x1", "percentage": "5"}]

        # Persistently throttled: give up, and report a failure rather than an
        # empty holder list so the two are never confused.
        stub = _StubSession([_StubResponse(429), _StubResponse(429)])
        scanner.session = stub
        assert scanner.goldrush_get("/eth-mainnet/tokens/0xabc/token_holders_v2/") is None
        assert stub.calls == 2

        # A genuine empty answer is an empty list, and is not retried.
        stub = _StubSession([_StubResponse(200, {"data": {"items": []}})])
        scanner.session = stub
        assert scanner.goldrush_get("/eth-mainnet/tokens/0xabc/token_holders_v2/") == []
        assert stub.calls == 1

        # A 404 is a real answer about the resource, so it must not be retried.
        stub = _StubSession([_StubResponse(404)])
        scanner.session = stub
        assert scanner.goldrush_get("/eth-mainnet/tokens/0xabc/token_holders_v2/") is None
        assert stub.calls == 1
    finally:
        scanner.GOLDRUSH_API_KEY = original_key
        scanner.session = original_session
        scanner.time.sleep = original_sleep


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
    test_confluence_caps_are_reachable()
    test_evidence_coverage_is_reported()
    test_project_bucket_scores_breadth()
    test_volume_bucket_scores_fresh_acceleration()
    test_market_bucket_requires_breadth_not_just_average()
    test_safety_bucket_scores_execution_floors()
    test_paper_ready_threshold_is_single_sourced()
    test_wallet_cap_and_technical_context_are_preserved()
    test_calibration_requires_a_measurable_paper_sample()
    test_conviction_applies_bounded_calibration()
    test_missing_artifacts_degrade_to_no_evidence()
    test_long_term_intel_protects_candidate_but_never_rejects()
    test_readiness_plan_reports_long_term_evidence()
    test_adverse_measurable_calibration_blocks_paper_plan()
    test_wallet_intel_layer_never_enables_execution()
    test_gmgn_flow_aggregates_distinct_wallets_over_seven_days()
    test_gmgn_flow_window_is_inclusive_at_exactly_seven_days()
    test_gmgn_sell_only_flow_scores_below_equivalent_buying()
    test_evm_project_score_can_exceed_the_old_concentration_ceiling()
    test_solscan_flow_is_never_replaced_by_gmgn_flow()
    test_richest_project_layer_survives_any_provider_order()
    test_polygon_maps_to_a_real_covalent_chain()
    test_unsupported_chain_is_reported_not_hidden()
    test_missing_contract_is_classified()
    test_resolved_contract_with_empty_provider_response_is_classified()
    test_missing_keys_degrade_without_inventing_flow()
    test_goldrush_retries_a_transient_failure_before_giving_up()
    print("Fil Before Pump smoke tests: PASS")
