"""
mock_oracle.py — MockOracleClient for generating test oracle events.
"""
from __future__ import annotations

import time
import uuid

from .types import (
    AttributionOutcomeV1,
    ConfidenceInterval,
    OracleScoreSnapshotV1,
    OracleState,
)


class MockOracleClient:
    """Emits realistic synthetic oracle messages for testing."""

    def __init__(self) -> None:
        self._nonce_counter: dict[str, int] = {}

    def _next_nonce(self, oracle_id: str) -> int:
        n = self._nonce_counter.get(oracle_id, 0) + 1
        self._nonce_counter[oracle_id] = n
        return n

    def emit_high_karma_producer(
        self, operator_id: str, domain: str
    ) -> OracleScoreSnapshotV1:
        """T3-quality operator: karma ~0.85, 180 effective samples, 3 oracles."""
        oracle_id = f"oracle-{operator_id}-primary"
        return OracleScoreSnapshotV1(
            schema_version="spi.oracle.v1",
            oracle_id=oracle_id,
            operator_id=operator_id,
            domain=domain,
            timestamp=time.time(),
            nonce=self._next_nonce(oracle_id),
            raw_karma=0.85,
            confidence_interval=ConfidenceInterval(lower=0.80, upper=0.90),
            oracle_quality_score=0.92,
            oracle_stake_pft=5000.0,
            effective_sample_size=180,
            oracle_state=OracleState.verified,
        )

    def emit_degraded_oracle(
        self, operator_id: str, domain: str
    ) -> OracleScoreSnapshotV1:
        """Degraded oracle: quality_score=0.4 (below filter), stale."""
        oracle_id = f"oracle-{operator_id}-degraded"
        return OracleScoreSnapshotV1(
            schema_version="spi.oracle.v1",
            oracle_id=oracle_id,
            operator_id=operator_id,
            domain=domain,
            timestamp=time.time() - 7200,  # 2 hours stale
            nonce=self._next_nonce(oracle_id),
            raw_karma=0.55,
            confidence_interval=ConfidenceInterval(lower=0.30, upper=0.80),
            oracle_quality_score=0.4,  # below MIN_QUALITY_SCORE=0.7
            oracle_stake_pft=100.0,
            effective_sample_size=10,
            oracle_state=OracleState.degraded,
        )

    def emit_unknown_producer(self, domain: str) -> OracleScoreSnapshotV1:
        """Brand-new operator: 0 effective samples, low karma."""
        operator_id = f"unknown-{uuid.uuid4().hex[:8]}"
        oracle_id = f"oracle-{operator_id}"
        return OracleScoreSnapshotV1(
            schema_version="spi.oracle.v1",
            oracle_id=oracle_id,
            operator_id=operator_id,
            domain=domain,
            timestamp=time.time(),
            nonce=self._next_nonce(oracle_id),
            raw_karma=0.20,
            confidence_interval=ConfidenceInterval(lower=0.0, upper=0.5),
            oracle_quality_score=0.75,
            oracle_stake_pft=50.0,
            effective_sample_size=0,
            oracle_state=OracleState.unverified,
        )

    def emit_attribution_outcome(
        self,
        operator_id: str,
        domain: str,
        trade_id: str,
        outperformed: bool = True,
    ) -> AttributionOutcomeV1:
        """Generate a realistic attribution outcome for a trade."""
        oracle_id = f"oracle-{operator_id}-primary"
        if outperformed:
            baseline_brier = 0.25
            realized_brier = 0.10  # beat the baseline
        else:
            baseline_brier = 0.25
            realized_brier = 0.40  # worse than baseline

        return AttributionOutcomeV1(
            schema_version="spi.oracle.v1",
            idempotency_key=f"{oracle_id}:attribution.outcome:{trade_id}",
            operator_id=operator_id,
            domain=domain,
            trade_id=trade_id,
            timestamp=time.time(),
            baseline_brier=baseline_brier,
            realized_brier=realized_brier,
            recency_weight=0.9,
            signal_weight=0.8,
            pnl_volatility=0.015,
            benchmark_volatility=0.02,
        )
