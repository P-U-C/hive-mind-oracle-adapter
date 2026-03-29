"""
tiering.py — Trust tier classification logic for Hive Mind Oracle.
"""
from __future__ import annotations

from .types import TrustTier

# Tier multipliers applied to effective weight during routing
TIER_MULTIPLIERS: dict[TrustTier, float] = {
    TrustTier.T0: 1.0,
    TrustTier.T1: 1.2,
    TrustTier.T2: 1.5,
    TrustTier.T3: 2.0,
}


def classify_tier(
    karma: float,
    effective_sample_size: int,
    oracle_count: int,
    sustained_days: int = 0,
    historical_peak_karma: float = 0.0,
) -> TrustTier:
    """
    Classify an operator into a TrustTier based on karma and evidence quality.

    Thresholds:
        T0: karma < 0.40
        T1: karma 0.40–0.59, effective_sample_size >= 20
        T2: karma 0.60–0.79, effective_sample_size >= 50
        T3: karma >= 0.80, effective_sample_size >= 100 AND oracle_count >= 3

    Conditional T1 floor:
        If effective_sample_size >= 50 AND historical_peak_karma >= 0.60 → min tier = T1
    """
    # Determine raw tier from karma + sample requirements
    raw_tier = _raw_tier(karma, effective_sample_size, oracle_count)

    # Apply conditional T1 floor
    if (
        effective_sample_size >= 50
        and historical_peak_karma >= 0.60
        and raw_tier == TrustTier.T0
    ):
        return TrustTier.T1

    return raw_tier


def _raw_tier(
    karma: float,
    effective_sample_size: int,
    oracle_count: int,
) -> TrustTier:
    if karma >= 0.80 and effective_sample_size >= 100 and oracle_count >= 3:
        return TrustTier.T3
    if karma >= 0.60 and effective_sample_size >= 50:
        return TrustTier.T2
    if karma >= 0.40 and effective_sample_size >= 20:
        return TrustTier.T1
    return TrustTier.T0


def tier_multiplier(tier: TrustTier) -> float:
    return TIER_MULTIPLIERS[tier]
