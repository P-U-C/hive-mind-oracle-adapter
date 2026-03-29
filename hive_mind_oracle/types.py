"""
types.py — Wire format dataclasses for the Hive Mind Oracle Routing Adapter.
Schema version: spi.oracle.v1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TrustTier(Enum):
    T0 = "T0"  # Unverified / low karma
    T1 = "T1"  # Provisional
    T2 = "T2"  # Verified
    T3 = "T3"  # Priority / high trust


class OracleState(Enum):
    unverified = "unverified"
    provisional = "provisional"
    verified = "verified"
    degraded = "degraded"
    suspended = "suspended"


@dataclass
class ConfidenceInterval:
    lower: float
    upper: float

    @property
    def width(self) -> float:
        return self.upper - self.lower


@dataclass
class OracleScoreSnapshotV1:
    """Emitted by an oracle node each scoring cycle."""
    schema_version: str          # must be "spi.oracle.v1"
    oracle_id: str
    operator_id: str
    domain: str                  # e.g. "onchain", "social", "technical", "tradfi"
    timestamp: float             # unix epoch seconds
    nonce: int                   # monotonically increasing per oracle_id
    raw_karma: float             # raw [0, 1]
    confidence_interval: ConfidenceInterval
    oracle_quality_score: float  # [0, 1] — filter threshold 0.7
    oracle_stake_pft: float      # stake weight in PFT
    effective_sample_size: int
    oracle_state: OracleState = OracleState.unverified
    # New required fields (with defaults for backwards compat)
    event_id: str = field(default="")
    occurred_at: str = field(default="")        # ISO 8601 string
    idempotency_key: str = field(default="")
    schema: str = field(default="ORACLE_SCORE_SNAPSHOT_V1")
    signature: str = field(default="")          # secp256k1 stub
    evidence_root: str = field(default="")
    calibration_score: float = field(default=0.0)
    freshness_seconds: int = field(default=0)
    source_overlap_score: float = field(default=0.0)
    sample_size: int = field(default=0)


@dataclass
class AttributionOutcomeV1:
    """Result of an attributed signal resolving (e.g. a trade closing)."""
    schema_version: str          # "spi.oracle.v1"
    idempotency_key: str         # dedup key
    operator_id: str
    domain: str
    trade_id: str
    timestamp: float
    baseline_brier: float        # reference forecast error
    realized_brier: float        # actual forecast error
    recency_weight: float        # [0, 1]
    signal_weight: float         # [0, 1]
    pnl_volatility: float        # realized vol of PnL
    benchmark_volatility: Optional[float] = None  # defaults to 0.02
    # New required fields (with defaults for backwards compat)
    event_id: str = field(default="")
    occurred_at: str = field(default="")
    schema: str = field(default="ATTRIBUTION_OUTCOME_V1")
    nonce: int = field(default=0)
    oracle_id: str = field(default="")
    producer_id: str = field(default="")
    conviction_id: str = field(default="")
    horizon_hours: int = field(default=0)
    scoring_method: str = field(default="brier")
    signature: str = field(default="")


@dataclass
class ReputationRefreshRequestV1:
    schema_version: str
    operator_id: str
    domain: str
    requested_at: float


@dataclass
class ReputationRefreshResponseV1:
    schema_version: str
    operator_id: str
    domain: str
    refreshed_at: float
    new_karma: float
    trust_tier: TrustTier
    notes: list[str] = field(default_factory=list)


@dataclass
class ExternalAffinitySignal:
    """Optional external signal that can supplement oracle evidence."""
    source: str
    operator_id: str
    domain: str
    affinity_score: float  # [-1, 1]
    confidence: float      # [0, 1]
    timestamp: float


@dataclass
class RoutingDecision:
    operator_id: str
    domain: str
    task_class: str
    trust_tier: TrustTier
    tier_multiplier: float
    effective_weight: float
    oracle_karma: float
    oracle_state: OracleState
    routing_notes: list[str] = field(default_factory=list)


@dataclass
class LedgerEntry:
    operator_id: str
    domain: str
    oracle_id: str
    karma: float
    effective_sample_size: int
    oracle_count: int             # computed: len(eligible_snaps) at ingestion time
    timestamp: float              # when this entry was recorded
    snapshot_timestamp: float     # original snapshot timestamp
    nonce: int
    high_divergence: bool = False
    confidence_haircut_applied: bool = False
    historical_peak_karma: float = 0.0
