"""
adapter.py — HiveMindOracleAdapter: ingests oracle evidence and produces routing decisions.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from .aggregation import aggregate_oracle_snapshots
from .decay import apply_decay, staleness_haircut
from .tiering import classify_tier, tier_multiplier
from .types import (
    AttributionOutcomeV1,
    LedgerEntry,
    OracleScoreSnapshotV1,
    OracleState,
    RoutingDecision,
    TrustTier,
)

DEFAULT_BENCHMARK_VOLATILITY = 0.02
LOW_SAMPLE_THRESHOLD = 20
LOW_SAMPLE_MULTIPLIER = 0.5


class HiveMindOracleAdapter:
    """
    Routes oracle evidence to trust-tiered routing decisions.

    Internal state:
        _ledger: dict[(operator_id, domain), LedgerEntry]
        _snapshots: dict[(operator_id, domain), list[OracleScoreSnapshotV1]]
        _nonces: dict[oracle_id, int]   — last seen nonce per oracle
        _processed_idempotency_keys: set[str]
        _operator_karma: dict[operator_id, float]  — cumulative karma
    """

    def __init__(self, time_offset: float = 0.0) -> None:
        """
        Args:
            time_offset: added to time.time() for timestamp validation in tests.
                         Set to a negative value to simulate the past, or adjust
                         snapshots to use a timestamp relative to (time.time() + offset).
        """
        self._time_offset = time_offset
        self._ledger: dict[tuple[str, str], LedgerEntry] = {}
        self._snapshots: dict[tuple[str, str], list[OracleScoreSnapshotV1]] = {}
        self._nonces: dict[str, int] = {}
        self._processed_keys: set[str] = set()
        self._operator_karma: dict[str, float] = {}
        self._peak_karma: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return time.time() + self._time_offset

    def _validate_snapshot(self, msg: OracleScoreSnapshotV1) -> None:
        if not msg.oracle_id:
            raise ValueError("oracle_id is required")
        if not msg.operator_id:
            raise ValueError("operator_id is required")
        if not msg.domain:
            raise ValueError("domain is required")
        if msg.schema_version != "spi.oracle.v1":
            raise ValueError(
                f"Unsupported schema_version: {msg.schema_version!r}. Expected 'spi.oracle.v1'."
            )
        # Nonce must be monotonically increasing per oracle_id
        last_nonce = self._nonces.get(msg.oracle_id, -1)
        if msg.nonce <= last_nonce:
            raise ValueError(
                f"Nonce regression for oracle {msg.oracle_id}: "
                f"received {msg.nonce}, last seen {last_nonce}"
            )
        # Timestamp must be within 60s of now
        age = abs(self._now() - msg.timestamp)
        if age > 60:
            raise ValueError(
                f"Snapshot timestamp too far from now: age={age:.1f}s (max 60s)"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_snapshot(self, msg: OracleScoreSnapshotV1) -> LedgerEntry:
        """
        Validate and ingest an oracle score snapshot.
        Aggregates with existing snapshots for the same (operator, domain).
        Returns a LedgerEntry.
        """
        self._validate_snapshot(msg)

        # Advance nonce
        self._nonces[msg.oracle_id] = msg.nonce

        key = (msg.operator_id, msg.domain)

        # Store snapshot for aggregation
        if key not in self._snapshots:
            self._snapshots[key] = []
        self._snapshots[key].append(msg)

        # Aggregate all snapshots for this (operator, domain)
        consensus_karma, high_divergence = aggregate_oracle_snapshots(
            self._snapshots[key]
        )

        eligible_snaps = [s for s in self._snapshots[key] if s.oracle_quality_score >= 0.7]
        total_oracle_coverage = max((s.oracle_count for s in eligible_snaps), default=0)
        haircut_applied = total_oracle_coverage < 3

        now = self._now()
        entry = LedgerEntry(
            operator_id=msg.operator_id,
            domain=msg.domain,
            oracle_id=msg.oracle_id,
            karma=consensus_karma,
            effective_sample_size=msg.effective_sample_size,
            oracle_count=msg.oracle_count,
            timestamp=now,
            snapshot_timestamp=msg.timestamp,
            nonce=msg.nonce,
            high_divergence=high_divergence,
            confidence_haircut_applied=haircut_applied,
            historical_peak_karma=max(
                self._ledger[key].historical_peak_karma if key in self._ledger else 0.0,
                consensus_karma,
            ),
        )

        self._ledger[key] = entry

        # Update operator karma
        if msg.operator_id not in self._operator_karma:
            self._operator_karma[msg.operator_id] = consensus_karma
        else:
            # EMA blend: 80% existing, 20% new
            self._operator_karma[msg.operator_id] = (
                0.8 * self._operator_karma[msg.operator_id] + 0.2 * consensus_karma
            )

        # Track peak
        self._peak_karma[msg.operator_id] = max(
            self._peak_karma.get(msg.operator_id, 0.0),
            self._operator_karma[msg.operator_id],
        )

        return entry

    def ingest_attribution_outcome(self, msg: AttributionOutcomeV1) -> dict:
        """
        Process an attribution outcome. Idempotent by idempotency_key.

        Returns:
            {"karma_delta": float, "karma_delta_risk_adj": float, "new_karma": float}
            or {"status": "already_processed"} for duplicate keys.
        """
        if not msg.idempotency_key:
            raise ValueError("idempotency_key is required")
        if not msg.operator_id:
            raise ValueError("operator_id is required")
        if msg.schema_version != "spi.oracle.v1":
            raise ValueError(f"Unsupported schema_version: {msg.schema_version!r}")

        if msg.idempotency_key in self._processed_keys:
            return {"status": "already_processed"}

        self._processed_keys.add(msg.idempotency_key)

        # karma_delta = recency_weight * signal_weight * (baseline_brier - realized_brier)
        karma_delta = (
            msg.recency_weight
            * msg.signal_weight
            * (msg.baseline_brier - msg.realized_brier)
        )

        benchmark_vol = msg.benchmark_volatility or DEFAULT_BENCHMARK_VOLATILITY
        karma_delta_risk_adj = karma_delta / (
            1 + msg.pnl_volatility / benchmark_vol
        )

        # Update operator karma
        current_karma = self._operator_karma.get(msg.operator_id, 0.5)
        new_karma = max(0.0, min(1.0, current_karma + karma_delta_risk_adj))
        self._operator_karma[msg.operator_id] = new_karma

        # Update peak
        self._peak_karma[msg.operator_id] = max(
            self._peak_karma.get(msg.operator_id, 0.0), new_karma
        )

        # Update ledger if present
        key = (msg.operator_id, msg.domain)
        if key in self._ledger:
            entry = self._ledger[key]
            entry.karma = new_karma
            entry.historical_peak_karma = max(entry.historical_peak_karma, new_karma)

        return {
            "karma_delta": karma_delta,
            "karma_delta_risk_adj": karma_delta_risk_adj,
            "new_karma": new_karma,
        }

    def compute_routing_decision(
        self,
        operator_id: str,
        domain: str,
        task_class: str = "high-value",
        max_staleness_seconds: float = 3600.0,
    ) -> RoutingDecision:
        """
        Compute a routing decision for an operator+domain pair.

        Applies:
            1. Domain-adaptive exponential decay
            2. Staleness haircut if age > max_staleness_seconds
            3. Low-sample multiplier if effective_sample_size < 20
            4. Trust tier classification
        """
        notes: list[str] = []
        key = (operator_id, domain)

        if key not in self._ledger:
            # Unknown operator — return T0 fallback
            notes.append("No ledger entry found; defaulting to T0 unverified.")
            return RoutingDecision(
                operator_id=operator_id,
                domain=domain,
                task_class=task_class,
                trust_tier=TrustTier.T0,
                tier_multiplier=1.0,
                effective_weight=0.0,
                oracle_karma=0.0,
                oracle_state=OracleState.unverified,
                routing_notes=notes,
            )

        entry = self._ledger[key]
        now = self._now()

        # Age in days for decay
        age_seconds = now - entry.snapshot_timestamp
        age_days = age_seconds / 86400.0

        # Apply exponential decay
        karma_decayed = apply_decay(entry.karma, age_days, domain)
        notes.append(
            f"Decay applied: karma {entry.karma:.4f} → {karma_decayed:.4f} "
            f"(age={age_days:.3f}d, domain={domain})"
        )

        # Staleness haircut
        haircut_mult = staleness_haircut(age_seconds, max_staleness_seconds)
        if haircut_mult < 1.0:
            notes.append(
                f"Staleness haircut applied: {haircut_mult:.3f} "
                f"(age={age_seconds:.0f}s, max={max_staleness_seconds:.0f}s)"
            )

        # Low sample penalty
        sample_mult = 1.0
        if entry.effective_sample_size < LOW_SAMPLE_THRESHOLD:
            sample_mult = LOW_SAMPLE_MULTIPLIER
            notes.append(
                f"Low sample multiplier applied: {sample_mult} "
                f"(n={entry.effective_sample_size} < {LOW_SAMPLE_THRESHOLD})"
            )

        effective_weight = karma_decayed * haircut_mult * sample_mult

        # Trust tier
        peak_karma = self._peak_karma.get(operator_id, entry.karma)
        trust_tier = classify_tier(
            karma=karma_decayed,
            effective_sample_size=entry.effective_sample_size,
            oracle_count=entry.oracle_count,
            historical_peak_karma=peak_karma,
        )
        t_mult = tier_multiplier(trust_tier)
        notes.append(f"Trust tier: {trust_tier.value} (multiplier={t_mult})")

        if entry.high_divergence:
            notes.append("High oracle divergence detected (IQR > 0.20).")

        oracle_state = OracleState.verified
        if trust_tier == TrustTier.T0:
            oracle_state = OracleState.unverified
        elif trust_tier == TrustTier.T1:
            oracle_state = OracleState.provisional

        return RoutingDecision(
            operator_id=operator_id,
            domain=domain,
            task_class=task_class,
            trust_tier=trust_tier,
            tier_multiplier=t_mult,
            effective_weight=effective_weight * t_mult,
            oracle_karma=karma_decayed,
            oracle_state=oracle_state,
            routing_notes=notes,
        )
