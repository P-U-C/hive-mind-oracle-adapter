"""
aggregation.py — CI-weighted multi-oracle aggregation for Hive Mind Oracle.
"""
from __future__ import annotations

import statistics
from .types import OracleScoreSnapshotV1

MIN_QUALITY_SCORE = 0.7
HIGH_DIVERGENCE_IQR_THRESHOLD = 0.20
CONFIDENCE_HAIRCUT = 0.5


def aggregate_oracle_snapshots(
    snapshots: list[OracleScoreSnapshotV1],
) -> tuple[float, bool]:
    """
    Aggregate oracle snapshots into a consensus karma score.

    Filtering:
        Only include snapshots with oracle_quality_score >= 0.7

    Weighting:
        w_i = (oracle_stake_pft * oracle_quality_score) / (ci_width^2 + 0.01)

    Aggregation:
        Weighted median (sort by karma_score, find cumulative-weight midpoint)

    Post-processing:
        - If fewer than 3 eligible snapshots: apply 50% confidence haircut
        - If IQR > 0.20: set high_divergence = True

    Returns:
        (consensus_karma: float, high_divergence: bool)
    """
    eligible = [s for s in snapshots if s.oracle_quality_score >= MIN_QUALITY_SCORE]
    # Haircut applies when total unique oracle coverage is < 3.
    # Each snapshot's oracle_count field reports how many oracles contributed
    # to that snapshot's score; use the max across eligible snapshots.
    total_oracle_coverage = max((s.oracle_count for s in eligible), default=0)
    haircut_applied = total_oracle_coverage < 3

    if not eligible:
        return 0.0, False

    # Compute weights
    weighted = []
    for snap in eligible:
        ci_width = snap.confidence_interval.width
        w = (snap.oracle_stake_pft * snap.oracle_quality_score) / (ci_width ** 2 + 0.01)
        weighted.append((snap.karma_score, w))

    # Weighted median
    weighted.sort(key=lambda x: x[0])
    total_weight = sum(w for _, w in weighted)
    midpoint = total_weight / 2.0
    cumulative = 0.0
    consensus = weighted[-1][0]
    for score, w in weighted:
        cumulative += w
        if cumulative >= midpoint:
            consensus = score
            break

    if haircut_applied:
        consensus *= CONFIDENCE_HAIRCUT

    # IQR divergence check (using raw scores of eligible snapshots)
    scores = [s.karma_score for s in eligible]
    high_divergence = False
    if len(scores) >= 4:
        scores_sorted = sorted(scores)
        n = len(scores_sorted)
        q1 = statistics.median(scores_sorted[: n // 2])
        q3 = statistics.median(scores_sorted[(n + 1) // 2 :])
        iqr = q3 - q1
        high_divergence = iqr > HIGH_DIVERGENCE_IQR_THRESHOLD
    elif len(scores) >= 2:
        iqr = max(scores) - min(scores)
        high_divergence = iqr > HIGH_DIVERGENCE_IQR_THRESHOLD

    return consensus, high_divergence
