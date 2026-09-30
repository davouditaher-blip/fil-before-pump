"""Final integration gate for Fil Before Pump.

Deterministic pre-persistence validation for the wallet-first pipeline.
This gate is read-only and never enables exchange execution.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from confluence_engine import BUCKET_CAPS, PAPER_READY_MIN_CONFLUENCE, SCALE_MAX, _caps_reachable

REQUIRED_CODE = [
    "scanner.py",
    "gmgn_layer.py",
    "wallet_quality_engine.py",
    "wallet_clustering.py",
    "wallet_radar.py",
    "wallet_performance_memory.py",
    "wallet_signal_profiles.py",
    "confluence_engine.py",
    "trade_readiness.py",
    "risk_engine.py",
    "paper_trading.py",
    "paper_performance.py",
    "wallet_paper_feedback.py",
    "wallet_intel_gate.py",
    "e2e_validate.py",
    "historical_replay.py",
    "test_historical_replay.py",
    "wallet_history_validation.py",
    "test_wallet_history_validation.py",
    "zerion_layer.py",
    "test_zerion_layer.py",
    "zerion_smoke_test.py",
    "zerion_history.py",
    "test_zerion_history.py",
    "historical_discovery.py",
    "test_historical_discovery.py",
    "proven_wallet_registry.py",
]

REQUIRED_ARTIFACTS = [
    "wallet_quality.json",
    "wallet_clusters.json",
    "wallet_radar.json",
    "trade_readiness.json",
    "paper_trades.json",
    "paper_performance.json",
    "wallet_performance_memory.json",
    "wallet_paper_feedback.json",
    "wallet_signal_profiles.json",
    "historical_replay.json",
]

def load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def main() -> None:
    errors = []
    # A declared cap that no evidence can reach silently shrinks the advertised
    # scale and makes the paper-ready threshold unattainable. This is a code
    # invariant, so it is checked before any artifact is read.
    if not _caps_reachable():
        errors.append("a confluence bucket cap is unreachable, so the 100-point scale is not honest")
    for name in REQUIRED_CODE:
        if not Path(name).is_file():
            errors.append(f"missing code module: {name}")

    data = {}
    for name in REQUIRED_ARTIFACTS:
        path = Path(name)
        if not path.is_file():
            errors.append(f"missing artifact: {name}")
            continue
        try:
            data[name] = load(name)
        except Exception as exc:
            errors.append(f"invalid artifact {name}: {exc}")

    readiness = data.get("trade_readiness.json", {})
    if readiness.get("mode") != "READ_ONLY_PAPER":
        errors.append("trade readiness mode is not READ_ONLY_PAPER")
    if readiness.get("orders_enabled") is not False:
        errors.append("trade readiness orders_enabled is not false")
    plans = readiness.get("plans", [])
    if not isinstance(plans, list):
        errors.append("trade readiness plans is not a list")
    ready_count = 0
    for plan in plans:
        if not isinstance(plan, dict):
            errors.append("invalid paper plan object")
            continue
        state = str(plan.get("state") or "")
        if state not in {"PAPER_READY", "WATCH_HIGH_CONVICTION", "WATCH"}:
            errors.append(f"invalid readiness state for {plan.get('symbol')}: {state}")
            continue
        # Watch candidates are expected in readiness and are not executable.
        if state != "PAPER_READY":
            continue
        ready_count += 1
        # A PAPER_READY plan must sit on the advertised, reachable scale.
        if "fil_confluence_max_score" in plan and float(plan["fil_confluence_max_score"]) != SCALE_MAX:
            errors.append(f"confluence scale is not {SCALE_MAX} for {plan.get('symbol')}")
        if "fil_confluence_paper_ready_threshold" in plan and float(plan["fil_confluence_paper_ready_threshold"]) != PAPER_READY_MIN_CONFLUENCE:
            errors.append(f"confluence threshold drifted for {plan.get('symbol')}")
        if float(plan.get("fil_confluence_score") or 0) < PAPER_READY_MIN_CONFLUENCE:
            errors.append(f"{plan.get('symbol')} is PAPER_READY below the confluence threshold")
        if (plan.get("blockers") or []):
            errors.append(f"{plan.get('symbol')} is PAPER_READY with blockers")
        risk = plan.get("risk") or {}
        if float(risk.get("max_account_risk_pct") or 0) <= 0 or float(risk.get("max_account_risk_pct") or 0) > 1:
            errors.append(f"invalid account risk for {plan.get('symbol')}")
        if float(risk.get("leverage_cap") or 0) <= 0 or float(risk.get("leverage_cap") or 0) > 3:
            errors.append(f"invalid leverage cap for {plan.get('symbol')}")

    paper = data.get("paper_trades.json", {})
    if paper.get("mode") != "PAPER_ONLY":
        errors.append("paper trading mode is not PAPER_ONLY")
    if (paper.get("summary") or {}).get("orders_enabled") is not False:
        errors.append("paper trading orders_enabled is not false")

    memory = data.get("wallet_performance_memory.json", {})
    if memory.get("mode") != "PAPER_ONLY":
        errors.append("wallet performance memory mode is not PAPER_ONLY")
    if memory.get("orders_enabled") is not False:
        errors.append("wallet performance memory orders_enabled is not false")
    if not isinstance(memory.get("memory"), list):
        errors.append("wallet performance memory is not a list")
    if float(memory.get("max_calibration_bonus") or 0) > 5 or float(memory.get("max_calibration_bonus") or 0) < 0:
        errors.append("wallet performance memory calibration cap is invalid")

    profiles = data.get("wallet_signal_profiles.json", {})
    if profiles.get("mode") != "DESCRIPTIVE_READ_ONLY":
        errors.append("wallet signal profiles mode is not DESCRIPTIVE_READ_ONLY")
    if profiles.get("orders_enabled") is not False:
        errors.append("wallet signal profiles orders_enabled is not false")
    if not isinstance(profiles.get("profiles"), dict):
        errors.append("wallet signal profiles is not a dict")

    feedback = data.get("wallet_paper_feedback.json", {})
    if feedback.get("mode") != "PAPER_ONLY":
        errors.append("wallet feedback mode is not PAPER_ONLY")
    if feedback.get("orders_enabled") is not False:
        errors.append("wallet feedback orders_enabled is not false")

    # Forward-only replay must keep reporting both wallet-history cohorts.
    replay = data.get("historical_replay.json", {})
    if replay.get("mode") != "HISTORICAL_FORWARD_ONLY":
        errors.append("historical replay mode is not HISTORICAL_FORWARD_ONLY")
    if replay.get("orders_enabled") is not False:
        errors.append("historical replay orders_enabled is not false")
    cohorts = replay.get("cohort_statistics")
    if not isinstance(cohorts, dict) or not cohorts:
        errors.append("historical replay is missing the forward-only cohort split")
    else:
        for cohort, per_window in cohorts.items():
            if str(cohort) not in replay.get("cohort_definition", {}):
                errors.append(f"historical replay cohort {cohort} has no definition")
            for window, stats in (per_window or {}).items():
                if not isinstance(stats, dict) or "hit_rate_pct" not in stats:
                    errors.append(f"historical replay cohort {cohort}/{window} is malformed")

    # Long-term wallet intelligence must actually reach the decision gate.
    # Plans produced before this wiring existed carry no wallet-intelligence
    # fields, so the check engages as soon as any plan exposes them.
    has_wallet_intel = any(
        isinstance(p, dict) and "wallet_profile_score" in p for p in plans
    )
    if has_wallet_intel:
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            symbol = plan.get("symbol")
            for field in ("wallet_profile_score", "wallet_calibration_bonus", "wallet_calibration_status", "wallet_longterm_proven", "wallet_conviction_pre_calibration"):
                if field not in plan:
                    errors.append(f"missing wallet intelligence field {field} for {symbol}")
            try:
                profile_score = float(plan.get("wallet_profile_score") or 0)
                calibration = float(plan.get("wallet_calibration_bonus") or 0)
            except (TypeError, ValueError):
                errors.append(f"invalid wallet intelligence values for {symbol}")
                continue
            if profile_score < 0 or profile_score > 12:
                errors.append(f"wallet profile score out of bounds for {symbol}")
            if calibration < -5 or calibration > 5:
                errors.append(f"wallet calibration bonus out of bounds for {symbol}")
            status = str(plan.get("wallet_calibration_status") or "UNAVAILABLE")
            if status not in {"MEASURABLE", "INSUFFICIENT_SAMPLE", "NO_HISTORY", "UNAVAILABLE"}:
                errors.append(f"invalid wallet calibration status for {symbol}: {status}")
            if status != "MEASURABLE" and calibration != 0:
                errors.append(f"wallet calibration applied without a measurable sample for {symbol}")
            if int(plan.get("wallet_longterm_proven") or 0) < 0:
                errors.append(f"invalid long-term proven wallet count for {symbol}")

    if errors:
        print("FINAL INTEGRATION GATE: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("FINAL INTEGRATION GATE: PASS")
    print(f"modules: {len(REQUIRED_CODE)}")
    print(f"validated artifacts: {len(data)}")
    print(f"confluence scale: {SCALE_MAX:.0f} (caps reachable: {_caps_reachable()})")
    print(f"paper-ready threshold: {PAPER_READY_MIN_CONFLUENCE:.0f}")
    print(f"readiness plans: {len(plans)}")
    print(f"risk-approved paper plans: {ready_count}")
    print(f"plans with long-term wallet evidence: {sum(1 for p in plans if isinstance(p, dict) and int(p.get('wallet_longterm_proven') or 0) > 0)}")
    with_scale = [p for p in plans if isinstance(p, dict) and "evidence_coverage" in p]
    if with_scale:
        # The honest test of the reweighting is whether real candidates clear the
        # threshold, and whether the project provider layer actually covers the
        # futures universe. Print both so the next run settles it.
        print(f"plans reporting evidence coverage: {len(with_scale)}")
        covered = [p for p in with_scale if p.get("project_layer_present")]
        gaps = [p for p in with_scale if not p.get("project_layer_present")]
        print(f"project layer coverage: {len(covered)}/{len(with_scale)}")
        # Every gap must carry an explicit reason, so an absent project layer is
        # never silently indistinguishable from a genuinely weak score. An
        # artifact written before reason recording existed is reported rather
        # than failed, because it says nothing about the current scanner.
        records_reasons = any("project_layer_missing_reason" in p for p in with_scale)
        if gaps:
            counts = Counter(str(p.get("project_layer_missing_reason") or "unclassified") for p in gaps)
            print(f"plans without a project layer: {len(gaps)}")
            for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  - {reason}: {count}")
            if counts.get("unclassified"):
                if records_reasons:
                    raise SystemExit(
                        f"FAIL: {counts['unclassified']} plans have no recorded project-layer reason"
                    )
                print("  note: artifact predates project-layer reason recording")
            # Separate what the provider genuinely does not have from faults that
            # only spoiled this run. A throttled or unauthenticated request says
            # nothing about the asset and will differ next run, so counting it as
            # a permanent gap would make a bad run look like a lasting fact.
            faults = sum(
                counts.get(reason, 0)
                for reason in ("provider_unavailable", "provider_not_queried")
            )
            if faults:
                print(
                    f"  - run faults (not asset gaps): {faults}"
                    "  -> request never delivered; expect a different result next run"
                )
            # Gaps this scanner caused. These are not provider limits, so they
            # must stay visible instead of disappearing into a provider bucket.
            created = counts.get("provider_holders_unusable", 0)
            if created:
                print(
                    f"  - self-inflicted (own filters, not the provider): {created}"
                    "  -> provider answered, our rules discarded every holder"
                )
        # Flow provenance: Solana flow is Solscan's all-participant token flow,
        # EVM flow is GMGN's labelled smart-money trades. The two populations
        # differ, so the mix is always reported.
        provenance = Counter(str(p.get("project_flow_provider") or "none") for p in with_scale)
        print(f"project flow provenance: {dict(provenance)}")
        projects = [
            _num((p.get("fil_confluence_components") or {}).get("project"))
            for p in with_scale
            if isinstance(p.get("fil_confluence_components"), dict)
        ]
        if projects:
            print(f"project bucket fill: mean {sum(projects) / len(projects):.1f}/{BUCKET_CAPS['project']:.0f}"
                  f" / max {max(projects):.1f}")
        print(f"confluence fill pct: min {min(_num(p.get('fil_confluence_fill_pct')) for p in with_scale):.1f} / max {max(_num(p.get('fil_confluence_fill_pct')) for p in with_scale):.1f}")
    print("live exchange execution: DISABLED")

if __name__ == "__main__":
    main()
