"""Fair-value range and expected-value computation.

Given a comp set (already filtered to make/model/year/mileage band by
``build_comp_set``), normalize each price to its private-party equivalent
via the channel multiplier, trim mileage outliers (>2 sd), then derive:

- ``value_low``  = p10 of the normalized population
- ``value_mid``  = p50 (median)
- ``value_high`` = p90
- ``expected_value`` = p10 + condition_position * (p90 - p10)

Confidence buckets are coarse on purpose — the consuming dashboard renders
them as colored badges, not numerical CIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from statistics import median, quantiles

from carbuyer.scoring.channels import condition_position, normalize_to_private
from carbuyer.scoring.comps import ComparableSale

# Below this many trimmed comps, the fair value is too noisy to use; the
# valuator records the count and signals INSUFFICIENT.
INSUFFICIENT_COMPS_THRESHOLD = 5
# At or above this many trimmed comps, confidence is HIGH. Ten is enough to
# get real deciles out of statistics.quantiles(n=10).
HIGH_CONFIDENCE_MIN_COMPS = 10
# z-score threshold for mileage trimming. <=2 sd keeps the bulk; >2 sd is the
# stale-fleet outlier that pulls the mean down.
MILEAGE_OUTLIER_SD = 2.0
# Below this many comps the trim function does nothing (too few points to
# meaningfully estimate variance).
MIN_COMPS_FOR_OUTLIER_TRIM = 4


class ConfidenceBucket(StrEnum):
    INSUFFICIENT = "insufficient"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class FairValue:
    value_low_cad: Decimal | None
    value_mid_cad: Decimal | None
    value_high_cad: Decimal | None
    expected_value_cad: Decimal | None
    comp_count: int
    confidence: ConfidenceBucket


def _trim_mileage_outliers(comps: list[ComparableSale]) -> list[ComparableSale]:
    """Drop comps whose mileage is more than ``MILEAGE_OUTLIER_SD`` away from
    the mean. With fewer than ``MIN_COMPS_FOR_OUTLIER_TRIM`` comps the
    trimming is meaningless; pass through. Comps with no mileage are kept
    unconditionally — they aren't outliers in a way we can measure."""
    if len(comps) < MIN_COMPS_FOR_OUTLIER_TRIM:
        return comps
    miles = [float(c.mileage_km) for c in comps if c.mileage_km is not None]
    if not miles:
        return comps
    mean = sum(miles) / len(miles)
    var = sum((m - mean) ** 2 for m in miles) / len(miles)
    sd = var ** 0.5
    if sd == 0:
        return comps
    return [
        c for c in comps
        if c.mileage_km is None
        or abs((c.mileage_km - mean) / sd) <= MILEAGE_OUTLIER_SD
    ]


def compute_fair_value(
    comps: list[ComparableSale],
    *,
    condition: str,
    sparse: bool = False,
) -> FairValue:
    """Phase 4 overlay #9: ``sparse`` threads through from
    ``lot.condition_inferred_from_sparse_listing`` so the position penalty
    fires only when the enricher signaled low confidence.

    Returns ``FairValue`` even on insufficient comp sets; the
    ``confidence`` field carries the verdict so callers don't need a
    None-or-result branch.
    """
    trimmed = _trim_mileage_outliers(comps)
    if len(trimmed) < INSUFFICIENT_COMPS_THRESHOLD:
        return FairValue(
            value_low_cad=None, value_mid_cad=None, value_high_cad=None,
            expected_value_cad=None, comp_count=len(trimmed),
            confidence=ConfidenceBucket.INSUFFICIENT,
        )
    normalized = sorted(
        float(normalize_to_private(c.price_cad, c.sale_channel)) for c in trimmed
    )
    if len(normalized) >= HIGH_CONFIDENCE_MIN_COMPS:
        # statistics.quantiles(n=10) returns the 9 inter-decile cutpoints,
        # so q[0] is p10 and q[-1] is p90.
        q = quantiles(normalized, n=10)
        p10, p90 = q[0], q[-1]
    else:
        # Not enough data for proper deciles; fall back to range endpoints
        # so the medium-confidence band at least bounds the observed prices.
        p10, p90 = min(normalized), max(normalized)
    p50 = median(normalized)
    pos = condition_position(condition, sparse=sparse)
    expected = p10 + pos * (p90 - p10)
    confidence = (
        ConfidenceBucket.HIGH if len(trimmed) >= HIGH_CONFIDENCE_MIN_COMPS
        else ConfidenceBucket.MEDIUM
    )
    return FairValue(
        value_low_cad=Decimal(round(p10, 2)),
        value_mid_cad=Decimal(round(p50, 2)),
        value_high_cad=Decimal(round(p90, 2)),
        expected_value_cad=Decimal(round(expected, 2)),
        comp_count=len(trimmed),
        confidence=confidence,
    )
