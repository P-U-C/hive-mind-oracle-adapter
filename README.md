# Hive Mind Oracle Routing Adapter

A Python implementation of the **SPI Oracle v1** routing adapter — translates multi-oracle reputation evidence into trust-tiered routing decisions for the Hive Mind network.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest
```

## Run Tests

```bash
python -m pytest tests/ -v
```

---

## Example: Routing a T3 Operator

```python
import time
from hive_mind_oracle import HiveMindOracleAdapter
from hive_mind_oracle.types import OracleScoreSnapshotV1, ConfidenceInterval, OracleState

adapter = HiveMindOracleAdapter()

# T3 requires ≥3 distinct oracle IDs, karma ≥0.80, effective_sample_size ≥100
for oracle_id, karma in [("oracle-a", 0.85), ("oracle-b", 0.83), ("oracle-c", 0.86)]:
    snap = OracleScoreSnapshotV1(
        schema_version="spi.oracle.v1",
        oracle_id=oracle_id,
        operator_id="alpha-trader-001",
        domain="onchain",
        timestamp=time.time(),
        nonce=1,
        raw_karma=karma,
        confidence_interval=ConfidenceInterval(lower=0.80, upper=0.90),
        oracle_quality_score=0.92,
        oracle_stake_pft=5000.0,
        effective_sample_size=180,
        oracle_state=OracleState.verified,
    )
    adapter.handle_reputation_update(snap)

decision = adapter.compute_routing_decision("alpha-trader-001", "onchain")

print(f"Trust Tier:        {decision.trust_tier.value}")
print(f"Tier Multiplier:   {decision.tier_multiplier}×")
print(f"Effective Weight:  {decision.effective_weight:.4f}")
print(f"Oracle Karma:      {decision.oracle_karma:.4f}")
```

**Output:**
```
Trust Tier:        T3
Tier Multiplier:   2.0×
Effective Weight:  1.7000
Oracle Karma:      0.8500
```

> **Note:** T3 requires snapshots from at least 3 distinct `oracle_id` values. A single oracle sending multiple snapshots cannot satisfy the multi-oracle threshold — each oracle overwrites its own previous entry in the pool.

---

## Architecture

```
Oracle Nodes
    │
    │  OracleScoreSnapshotV1 (spi.oracle.v1)
    ▼
HiveMindOracleAdapter.ingest_snapshot()
    │
    ├── Validate: schema_version, nonce monotonicity, timestamp freshness (±60s)
    ├── Aggregate: CI-weighted multi-oracle consensus
    │       w_i = (stake × quality) / (ci_width² + 0.01)
    │       weighted median → consensus_karma
    │       <3 eligible oracles → 50% confidence haircut
    └── Store: LedgerEntry (operator, domain, karma, peak, samples)
    
AttributionOutcomeV1 (trade resolves)
    │
    ├── Idempotency dedup by key
    ├── karma_delta = recency × signal × (baseline_brier − realized_brier)
    └── Risk-adjusted: delta / (1 + pnl_vol / benchmark_vol)

compute_routing_decision(operator, domain)
    │
    ├── Exponential decay: karma_eff = karma × exp(−ln2 × age_days / half_life)
    │       Domain half-lives (days): social/narrative=7, onchain=14,
    │                                 technical=21, default=30, tradfi/macro=60
    ├── Staleness haircut if age > 3600s: haircut = min(ratio×0.25, 0.75)
    ├── Low-sample penalty if n < 20: 0.5× weight
    ├── Trust tier classification:
    │       T0: karma < 0.40
    │       T1: karma 0.40–0.59, n ≥ 20  (multiplier 1.2×)
    │       T2: karma 0.60–0.79, n ≥ 50  (multiplier 1.5×)
    │       T3: karma ≥ 0.80, n ≥ 100, oracles ≥ 3  (multiplier 2.0×)
    └── Returns RoutingDecision
```

---

## Wire Format Examples

**OracleScoreSnapshotV1:**
```json
{
  "schema_version": "spi.oracle.v1",
  "oracle_id": "oracle-alpha-001",
  "operator_id": "alpha-trader-001",
  "domain": "onchain",
  "timestamp": 1711700000.0,
  "nonce": 42,
  "raw_karma": 0.85,
  "confidence_interval": {"lower": 0.80, "upper": 0.90},
  "oracle_quality_score": 0.92,
  "oracle_stake_pft": 5000.0,
  "effective_sample_size": 180,
  "oracle_count": 3,
  "oracle_state": "verified"
}
```

**AttributionOutcomeV1:**
```json
{
  "schema_version": "spi.oracle.v1",
  "idempotency_key": "trade-007-alpha-trader-001",
  "operator_id": "alpha-trader-001",
  "domain": "onchain",
  "trade_id": "trade-007",
  "timestamp": 1711700100.0,
  "baseline_brier": 0.25,
  "realized_brier": 0.10,
  "recency_weight": 0.9,
  "signal_weight": 0.8,
  "pnl_volatility": 0.015,
  "benchmark_volatility": 0.02
}
```

See [Post Fiat SPI Oracle spec](https://postfiat.org/specs/spi-oracle) for full wire format documentation.

---

## How to Extend

### Adding a New Oracle

1. Create a snapshot emitter in `mock_oracle.py` (for testing) or implement a real oracle client.
2. Call `adapter.ingest_snapshot(snapshot)` with a valid `OracleScoreSnapshotV1`.
3. The adapter auto-aggregates all snapshots for the same `(operator_id, domain)` pair.

### Adding a New Domain

1. Add the domain name and half-life to `DOMAIN_HALF_LIVES` in `decay.py`:
   ```python
   DOMAIN_HALF_LIVES["defi"] = 10.0  # 10-day half-life for DeFi signals
   ```
2. The rest of the pipeline (aggregation, tiering, routing) picks it up automatically.

### Adjusting Trust Tier Thresholds

Edit `tiering.py` — `_raw_tier()` function. Thresholds and multipliers are defined at the top of the module.

---

## Module Reference

| Module | Purpose |
|--------|---------|
| `types.py` | Dataclasses for all wire messages and internal state |
| `adapter.py` | Main adapter: ingest, aggregate, route |
| `tiering.py` | Trust tier classification (T0–T3) |
| `decay.py` | Domain-adaptive exponential decay + staleness haircut |
| `aggregation.py` | CI-weighted multi-oracle consensus |
| `mock_oracle.py` | Synthetic oracle events for tests |

---

## Production Deployment Notes

### Persistence (Required for HA)

The current `LedgerEntry` map is in-memory only. For production Hive Mind nodes, back it with Redis (already a runbook dependency) or PostgreSQL to survive restarts:

```python
# Redis backing (aioredis)
redis_key = f"hive:ledger:{operator_id}:{domain}"
await redis.setex(redis_key, ttl_seconds, json.dumps(entry_dict))

# Or PostgreSQL — reuse the contributor_authorization table as the auth source,
# and add a oracle_ledger table keyed by (operator_id, domain).
```

### Auth-Gate Integration

This adapter is orthogonal to the Gate 4 enforcement tables (`contributor_authorization`, `reward_emissions`). The intended integration point:

```
T3 RoutingDecision → on_routing_decision callback → trigger linking-score review
                   → auto-promote PROBATIONARY → AUTHORIZED (if karma ≥ 0.80 sustained 30d)
```

Expose the hook by passing an `on_routing_decision: Callable[[RoutingDecision], None] | None = None` to `HiveMindOracleAdapter.__init__()`.

Tier → auth-gate state mapping:
| Trust Tier | Auth-Gate State | Notes |
|-----------|----------------|-------|
| T0 | UNKNOWN / PROBATIONARY | No oracle endorsement |
| T1 | PROBATIONARY | 25% liquid / 75% vesting |
| T2 | AUTHORIZED | 100% liquid |
| T3 | TRUSTED (Layer B) | Reduced gate checks + 2.0× routing |

### Policy Versioning

`RoutingDecision` exposes routing_notes but not `policy_version`. For audit-log compliance, add:

```python
@dataclass
class RoutingDecision:
    ...
    policy_version: str = "spi.oracle.v1"
```

### Cryptographic Signatures

The `_verify_signature` stub uses secp256k1 (consistent with PFTL wallet binding). Production path:
1. Register oracle public key in Oracle Registry at deployment
2. On each snapshot: SHA-256 canonical payload → verify secp256k1 sig
3. On failure: reject message, slash oracle bond per runbook Section 0.3

BLS aggregate signatures are a future upgrade — allows batch-verifying N oracle snapshots in a single pairing check. Relevant once N > 10 oracles are live.

### Deployment Model

Deploy as a sidecar to every Hive Mind / TaskNode instance. Shadow → soft → full rollout mirrors the auth-gate phased deployment. Monitor `routing_notes` for spikes in:
- `"Low sample multiplier"` → early Sybil signal
- `"Staleness haircut"` → oracle degradation
- `"High oracle divergence"` → potential oracle collusion attempt

---

## Known Limitations & Future Work

### Distributed Safety
State (nonces, snapshots, peak karma) is in-memory only. A restart resets trust history and replay protection. In a multi-worker deployment, instances can diverge. **Mitigation:** Back `LedgerEntry` with Redis (keyed `hive:ledger:{operator_id}:{domain}`) and store `processed_idempotency_keys` in a shared Redis set with TTL.

### Signature Verification  
The `_verify_signature` stub always accepts. Real deployment requires secp256k1 key lookup from Oracle Registry + canonical payload hashing before any snapshot is trusted. See `adapter.py` stub for production requirements.

### Correlated Oracle Discounting
Three oracles with high `source_overlap_score` are not yet discounted in proportion to their correlation. Independent evidence carries more epistemic weight than correlated evidence. Future: apply `source_overlap_score` as a dependency discount factor: `effective_weight = w * (1 - source_overlap_score)`.

### Divergence Pricing
`high_divergence` flag is logged in `routing_notes` but does not automatically reduce `effective_weight` beyond the 50% confidence haircut. Future: when `high_divergence=True`, cap `trust_tier` at T2 maximum until a third independent oracle resolves.
