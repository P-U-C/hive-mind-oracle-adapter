"""
test_adapter.py — Unit tests for the Hive Mind Oracle Routing Adapter.
"""
from __future__ import annotations

import time
import pytest

from hive_mind_oracle import (
    HiveMindOracleAdapter,
    MockOracleClient,
    OracleScoreSnapshotV1,
    AttributionOutcomeV1,
    ConfidenceInterval,
    OracleState,
    TrustTier,
)
from hive_mind_oracle.aggregation import aggregate_oracle_snapshots
from hive_mind_oracle.tiering import classify_tier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snapshot(
    operator_id: str = "op1",
    domain: str = "onchain",
    karma: float = 0.85,
    quality: float = 0.92,
    stake: float = 5000.0,
    ess: int = 180,
    oracle_count: int = 3,
    oracle_id: str = "oracle-1",
    nonce: int = 1,
    timestamp: float | None = None,
    ci_lower: float = 0.80,
    ci_upper: float = 0.90,
    state: OracleState = OracleState.verified,
) -> OracleScoreSnapshotV1:
    return OracleScoreSnapshotV1(
        schema_version="spi.oracle.v1",
        oracle_id=oracle_id,
        operator_id=operator_id,
        domain=domain,
        timestamp=timestamp if timestamp is not None else time.time(),
        nonce=nonce,
        karma_score=karma,
        confidence_interval=ConfidenceInterval(lower=ci_lower, upper=ci_upper),
        oracle_quality_score=quality,
        oracle_stake_pft=stake,
        effective_sample_size=ess,
        oracle_count=oracle_count,
        oracle_state=state,
    )


def make_attribution(
    operator_id: str = "op1",
    domain: str = "onchain",
    trade_id: str = "trade-001",
    idempotency_key: str | None = None,
    baseline_brier: float = 0.25,
    realized_brier: float = 0.10,
    recency_weight: float = 0.9,
    signal_weight: float = 0.8,
    pnl_volatility: float = 0.015,
    benchmark_volatility: float = 0.02,
) -> AttributionOutcomeV1:
    return AttributionOutcomeV1(
        schema_version="spi.oracle.v1",
        idempotency_key=idempotency_key or f"{trade_id}-{operator_id}",
        operator_id=operator_id,
        domain=domain,
        trade_id=trade_id,
        timestamp=time.time(),
        baseline_brier=baseline_brier,
        realized_brier=realized_brier,
        recency_weight=recency_weight,
        signal_weight=signal_weight,
        pnl_volatility=pnl_volatility,
        benchmark_volatility=benchmark_volatility,
    )


# ---------------------------------------------------------------------------
# Test 1: Verified operator gets T3 priority routing (2.0×)
# ---------------------------------------------------------------------------

def test_oracle_verified_operator_gets_priority_routing():
    adapter = HiveMindOracleAdapter()
    snap = make_snapshot(operator_id="op-t3", karma=0.85, ess=180, oracle_count=3)
    adapter.ingest_snapshot(snap)

    decision = adapter.compute_routing_decision("op-t3", "onchain")
    assert decision.trust_tier == TrustTier.T3
    assert decision.tier_multiplier == 2.0
    assert decision.effective_weight > 0


# ---------------------------------------------------------------------------
# Test 2: Unknown/unverified operator falls back to T0 (1.0×)
# ---------------------------------------------------------------------------

def test_unverified_operator_fallback():
    adapter = HiveMindOracleAdapter()
    # No snapshot ingested — operator unknown
    decision = adapter.compute_routing_decision("unknown-op", "onchain")
    assert decision.trust_tier == TrustTier.T0
    assert decision.tier_multiplier == 1.0
    assert decision.effective_weight == 0.0


# ---------------------------------------------------------------------------
# Test 3: Degraded oracle (quality < 0.7) excluded from consensus
# ---------------------------------------------------------------------------

def test_degraded_oracle_graceful_degradation():
    mock = MockOracleClient()
    degraded = mock.emit_degraded_oracle("op-deg", "onchain")

    # Patch timestamp to be current (degraded oracle has stale timestamp)
    degraded.timestamp = time.time()
    # Reset nonce counter
    adapter = HiveMindOracleAdapter()
    # Force nonce acceptance
    adapter._nonces[degraded.oracle_id] = degraded.nonce - 1

    entry = adapter.ingest_snapshot(degraded)
    # The degraded oracle has quality=0.4 < 0.7, so it should be excluded
    # aggregate returns 0.0 with haircut applied
    assert entry.confidence_haircut_applied is True
    # Karma should be 0.0 (no eligible oracle) * 0.5 haircut = 0.0
    assert entry.karma == 0.0


# ---------------------------------------------------------------------------
# Test 4: Karma exactly at T1→T2 boundary (0.60)
# ---------------------------------------------------------------------------

def test_karma_threshold_boundary_t1_to_t2():
    tier = classify_tier(karma=0.60, effective_sample_size=50, oracle_count=2)
    assert tier == TrustTier.T2


# ---------------------------------------------------------------------------
# Test 5: Karma exactly at T2→T3 boundary (0.80)
# ---------------------------------------------------------------------------

def test_karma_threshold_boundary_t2_to_t3():
    # Exactly 0.80 with sufficient samples and oracle_count=3
    tier = classify_tier(karma=0.80, effective_sample_size=100, oracle_count=3)
    assert tier == TrustTier.T3

    # Exactly 0.80 but oracle_count < 3 — should fall to T2
    tier_not_t3 = classify_tier(karma=0.80, effective_sample_size=100, oracle_count=2)
    assert tier_not_t3 == TrustTier.T2


# ---------------------------------------------------------------------------
# Test 6: Malformed message rejection
# ---------------------------------------------------------------------------

def test_malformed_message_rejection():
    adapter = HiveMindOracleAdapter()

    # Wrong schema_version
    bad_snap = make_snapshot(oracle_id="oracle-bad", nonce=1)
    bad_snap.schema_version = "bad.version"
    with pytest.raises(ValueError, match="schema_version"):
        adapter.ingest_snapshot(bad_snap)

    # Timestamp too far from now (600 seconds ago)
    stale_snap = make_snapshot(oracle_id="oracle-stale", nonce=1, timestamp=time.time() - 600)
    with pytest.raises(ValueError, match="timestamp"):
        adapter.ingest_snapshot(stale_snap)


# ---------------------------------------------------------------------------
# Test 7: Staleness haircut reduces effective weight
# ---------------------------------------------------------------------------

def test_staleness_haircut():
    # Use time_offset so that a "recent" snapshot ages instantly
    adapter = HiveMindOracleAdapter(time_offset=0)
    snap = make_snapshot(operator_id="op-stale", oracle_id="oracle-stale", nonce=1)
    adapter.ingest_snapshot(snap)

    # Now simulate time passing: advance adapter clock by 7200 seconds (2 hours)
    adapter._time_offset = 7200
    decision = adapter.compute_routing_decision(
        "op-stale", "onchain", max_staleness_seconds=3600
    )
    # With staleness_ratio=2.0 → haircut_fraction=min(0.5, 0.75)=0.50 → mult=0.50
    # effective_weight should be significantly reduced
    # Also, T3 karma ~0.85 with 2hr decay on onchain (half_life=14d) is minimal
    assert any("Staleness haircut" in note for note in decision.routing_notes)


# ---------------------------------------------------------------------------
# Test 8: Idempotency deduplication
# ---------------------------------------------------------------------------

def test_idempotency_deduplication():
    adapter = HiveMindOracleAdapter()
    outcome = make_attribution(idempotency_key="idem-key-001")

    result1 = adapter.ingest_attribution_outcome(outcome)
    assert "karma_delta" in result1

    result2 = adapter.ingest_attribution_outcome(outcome)
    assert result2 == {"status": "already_processed"}


# ---------------------------------------------------------------------------
# Test 9: Positive karma delta for outperformance
# ---------------------------------------------------------------------------

def test_karma_delta_positive_for_outperformance():
    adapter = HiveMindOracleAdapter()
    outcome = make_attribution(
        baseline_brier=0.25,
        realized_brier=0.10,  # better than baseline
        idempotency_key="idem-pos-001",
    )
    result = adapter.ingest_attribution_outcome(outcome)
    assert result["karma_delta"] > 0
    assert result["karma_delta_risk_adj"] > 0


# ---------------------------------------------------------------------------
# Test 10: Negative karma delta for underperformance
# ---------------------------------------------------------------------------

def test_karma_delta_negative_for_underperformance():
    adapter = HiveMindOracleAdapter()
    outcome = make_attribution(
        baseline_brier=0.25,
        realized_brier=0.40,  # worse than baseline
        idempotency_key="idem-neg-001",
    )
    result = adapter.ingest_attribution_outcome(outcome)
    assert result["karma_delta"] < 0
    assert result["karma_delta_risk_adj"] < 0


# ---------------------------------------------------------------------------
# Test 11: Multi-oracle CI-weighted aggregation
# ---------------------------------------------------------------------------

def test_multi_oracle_aggregation():
    # Three oracles with different CI widths and stakes
    snaps = [
        make_snapshot(karma=0.80, quality=0.90, stake=5000.0,
                      ci_lower=0.75, ci_upper=0.85, oracle_id="o1"),  # width=0.10
        make_snapshot(karma=0.85, quality=0.95, stake=8000.0,
                      ci_lower=0.83, ci_upper=0.87, oracle_id="o2"),  # width=0.04, high weight
        make_snapshot(karma=0.70, quality=0.80, stake=2000.0,
                      ci_lower=0.60, ci_upper=0.90, oracle_id="o3"),  # width=0.30, low weight
    ]
    consensus, high_divergence = aggregate_oracle_snapshots(snaps)
    # All 3 eligible — no haircut
    # High-weight oracle (o2) has karma=0.85, should pull median up
    assert 0.70 <= consensus <= 0.90
    # IQR = 0.85 - 0.70 = 0.15 < 0.20 → no divergence
    assert high_divergence is False


# ---------------------------------------------------------------------------
# Test 12: Conditional T1 floor
# ---------------------------------------------------------------------------

def test_conditional_t1_floor():
    # Operator with karma below T1 threshold but 50+ samples and peak >= 0.60
    tier = classify_tier(
        karma=0.35,  # normally T0
        effective_sample_size=55,  # >= 50
        oracle_count=1,
        historical_peak_karma=0.65,  # >= 0.60 peak
    )
    assert tier == TrustTier.T1

    # Without peak karma — stays T0
    tier_t0 = classify_tier(
        karma=0.35,
        effective_sample_size=55,
        oracle_count=1,
        historical_peak_karma=0.50,  # < 0.60
    )
    assert tier_t0 == TrustTier.T0
