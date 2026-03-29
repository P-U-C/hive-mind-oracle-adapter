"""
adapter.py — HiveMindOracleAdapter: ingests oracle evidence and produces routing decisions.
"""
from __future__ import annotations

import math
import sys
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
        self._time_offset = time_offset
        self._ledger: dict[tuple[str, str], LedgerEntry] = {}
        self._snapshots: dict[tuple[str, str], dict[str, OracleScoreSnapshotV1]] = {}
        self._nonces: dict[str, int] = {}
        self._processed_keys: set[str] = set()
        self._operator_karma: dict[str, float] = {}
        self._peak_karma: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return time.time() + self._time_offset

    def _verify_signature(self, msg_type: str, payload: dict, signature: str, oracle_id: str) -> bool:
        """
        Stub: secp256k1 signature verification.

        Production implementation must:
        1. Fetch oracle's registered public key from Oracle Registry
        2. Compute canonical payload hash (SHA-256 of sorted JSON fields)
        3. Verify secp256k1 signature against hash
        4. Reject message if signature invalid or oracle key not registered

        Current implementation: STUB — logs warning, always returns True.
        """
        print(f"[WARN] Signature verification not implemented for {msg_type} from {oracle_id}", file=sys.stderr)
        return True

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
        # Signature verification stub
        self._verify_signature("ORACLE_SCORE_SNAPSHOT_V1", {}, msg.signature, msg.oracle_id)
        # Field range validation
        self._validate_fields(msg)

    def _validate_fields(self, msg: OracleScoreSnapshotV1) -> None:
        """Validate numeric field ranges to prevent adversarial manipulation."""
        if not (0.0 <= msg.raw_karma <= 1.0):
            raise ValueError(f"raw_karma out of range [0,1]: {msg.raw_karma}")
        if not (0.0 <= msg.oracle_quality_score <= 1.0):
            raise ValueError(f"oracle_quality_score out of range [0,1]: {msg.oracle_quality_score}")
        if msg.oracle_stake_pft < 0:
            raise ValueError(f"oracle_stake_pft cannot be negative: {msg.oracle_stake_pft}")
        if msg.effective_sample_size < 0:
            raise ValueError(f"effective_sample_size cannot be negative: {msg.effective_sample_size}")
        ci = msg.confidence_interval
        if not (0.0 <= ci.lower <= ci.upper <= 1.0):
            raise ValueError(f"confidence_interval invalid: [{ci.lower}, {ci.upper}]")
        if ci.lower == ci.upper:
            raise ValueError(f"confidence_interval has zero width — pathological input: {ci.lower}")

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

        # Store snapshot for aggregation — deduplicate by oracle_id, keep latest per oracle
        if key not in self._snapshots:
            self._snapshots[key] = {}  # dict keyed by oracle_id
        self._snapshots[key][msg.oracle_id] = msg  # overwrites previous snapshot from same oracle

        # Aggregate all snapshots for this (operator, domain)
        consensus_karma, high_divergence = aggregate_oracle_snapshots(
            list(self._snapshots[key].values())
        )

        eligible_snaps = [s for s in self._snapshots[key].values() if s.oracle_quality_score >= 0.7]
        haircut_applied = len(eligible_snaps) < 3

        all_snapshot_timestamps = [s.timestamp for s in self._snapshots[key].values()]
        oldest_snapshot_timestamp = min(all_snapshot_timestamps)
        snapshot_timestamp = max(all_snapshot_timestamps)

        now = self._now()
        entry = LedgerEntry(
            operator_id=msg.operator_id,
            domain=msg.domain,
            oracle_id=msg.oracle_id,
            karma=consensus_karma,
            effective_sample_size=msg.effective_sample_size,
            oracle_count=len(eligible_snaps),
            timestamp=now,
            snapshot_timestamp=snapshot_timestamp,
            oldest_snapshot_timestamp=oldest_snapshot_timestamp,
            nonce=msg.nonce,
            high_divergence=high_divergence,
            confidence_haircut_applied=haircut_applied,
            historical_peak_karma=max(
                self._ledger[key].historical_peak_karma if key in self._ledger else 0.0,
                consensus_karma,
            ),
        )

        self._ledger[key] = entry

        # Direct assignment — CI-weighted consensus IS the karma
        self._operator_karma[msg.operator_id] = consensus_karma

        # Track peak
        self._peak_karma[msg.operator_id] = max(
            self._peak_karma.get(msg.operator_id, 0.0),
            consensus_karma,
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

        if not (0.0 <= msg.signal_weight <= 1.0):
            raise ValueError(f"signal_weight out of range [0,1]: {msg.signal_weight}")
        if not (0.0 <= msg.recency_weight <= 1.0):
            raise ValueError(f"recency_weight out of range [0,1]: {msg.recency_weight}")
        if msg.pnl_volatility < 0:
            raise ValueError(f"pnl_volatility cannot be negative: {msg.pnl_volatility}")
        benchmark_vol = msg.benchmark_volatility or DEFAULT_BENCHMARK_VOLATILITY
        if benchmark_vol <= 0:
            raise ValueError(f"benchmark_volatility must be positive: {benchmark_vol}")

        self._processed_keys.add(msg.idempotency_key)

        # Enforce spec: signal_weight = 0 if confidence < 0.30
        effective_signal_weight = msg.signal_weight if msg.signal_weight >= 0.30 else 0.0

        # karma_delta = recency_weight * signal_weight * (baseline_brier - realized_brier)
        karma_delta = (
            msg.recency_weight
            * effective_signal_weight
            * (msg.baseline_brier - msg.realized_brier)
        )

        karma_delta_risk_adj = karma_delta / (
            1 + msg.pnl_volatility / benchmark_vol
        )

        # Update operator karma (seeded at 0.0 if not seen before)
        current_karma = self._operator_karma.get(msg.operator_id, 0.0)
        new_karma = max(0.0, min(1.0, current_karma + karma_delta_risk_adj))
        self._operator_karma[msg.operator_id] = new_karma

        # Update peak in _peak_karma only (no ledger mutation)
        self._peak_karma[msg.operator_id] = max(
            self._peak_karma.get(msg.operator_id, 0.0), new_karma
        )

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

        # Age in days for decay — use oldest snapshot to prevent fresh oracle masking stale consensus
        staleness_base = entry.oldest_snapshot_timestamp if entry.oldest_snapshot_timestamp > 0 else entry.snapshot_timestamp
        age_seconds = now - staleness_base
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

        # Trust tier — oracle_count from ledger's computed count
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

    # ------------------------------------------------------------------
    # Task-spec aliases (explicit names matching verification requirements)
    # ------------------------------------------------------------------

    def handle_reputation_update(self, msg: OracleScoreSnapshotV1) -> LedgerEntry:
        """
        Alias for ingest_snapshot().
        Matches the 'reputation_update' message type handler required by the
        Hive Mind Oracle Routing spec verification criteria.
        """
        return self.ingest_snapshot(msg)

    def handle_attribution_outcome(self, msg: AttributionOutcomeV1) -> dict:
        """
        Alias for ingest_attribution_outcome().
        Matches the 'attribution_outcome' message type handler required by the
        Hive Mind Oracle Routing spec verification criteria.
        """
        return self.ingest_attribution_outcome(msg)
