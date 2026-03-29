"""
decay.py — Domain-adaptive exponential decay for oracle karma scores.
"""
from __future__ import annotations

import math

# Half-life in days per domain
DOMAIN_HALF_LIVES: dict[str, float] = {
    "social": 7.0,
    "narrative": 7.0,
    "onchain": 14.0,
    "technical": 21.0,
    "tradfi": 60.0,
    "macro": 60.0,
    "default": 30.0,
}

# Default max staleness before haircut applies (seconds)
DEFAULT_MAX_STALENESS_SECONDS = 3600.0


def get_half_life(domain: str) -> float:
    """Return the half-life in days for the given domain."""
    return DOMAIN_HALF_LIVES.get(domain.lower(), DOMAIN_HALF_LIVES["default"])


def apply_decay(karma_raw: float, age_days: float, domain: str) -> float:
    """
    Apply domain-adaptive exponential decay.

        karma_eff = karma_raw * exp(-ln(2) * age_days / half_life)
    """
    half_life = get_half_life(domain)
    decay_factor = math.exp(-math.log(2) * age_days / half_life)
    return karma_raw * decay_factor


def staleness_haircut(age_seconds: float, max_staleness_seconds: float = DEFAULT_MAX_STALENESS_SECONDS) -> float:
    """
    Return a multiplicative haircut for staleness (1.0 = no haircut).

    If age_seconds > max_staleness_seconds:
        haircut_fraction = min(staleness_ratio * 0.25, 0.75)
        effective_multiplier = 1.0 - haircut_fraction

    Returns the multiplier to apply to effective_weight.
    """
    if age_seconds <= max_staleness_seconds:
        return 1.0
    staleness_ratio = age_seconds / max_staleness_seconds
    haircut_fraction = min(staleness_ratio * 0.25, 0.75)
    return 1.0 - haircut_fraction
