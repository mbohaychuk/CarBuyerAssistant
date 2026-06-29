"""Pydantic schemas for LLM-structured output.

These are passed as `response_format=` to the OpenAI structured-output API and
also used as the in-process types for downstream pipeline workers (valuator,
notifier, dashboard). Single source of truth — Phase 8 vision extends the
same VisionOutput / PerImageOutput defined here.

`Condition` is `Literal["bad","poor","decent","good","great"]` deliberately — no
"unknown" value. Phase 3 design overlay #14: when the LLM is uncertain about
condition, the system prompt instructs it to output `decent` and set
`condition_confidence < 0.5`. The enricher worker then sets
`condition_inferred_from_sparse_listing=True` so Phase 4 can apply a separate
sparse-listing pessimism penalty (vs. a genuinely-confident "decent" rating).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Transmission = Literal["manual", "automatic", "cvt", "unknown"]
Drivetrain = Literal["fwd", "rwd", "awd", "4wd", "unknown"]
TitleStatus = Literal[
    "NORMAL", "SALVAGE", "REBUILT", "NON_REPAIRABLE", "STOLEN", "UNKNOWN",
]
Condition = Literal["bad", "poor", "decent", "good", "great"]
DescriptionQuality = Literal["thin", "adequate", "detailed"]


class NormalizedVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    engine: str | None
    transmission: Transmission
    drivetrain: Drivetrain
    mileage_km: int | None
    vin: str | None


class FlagInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str
    evidence: str
    weight: int


class ShowstopperInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str
    evidence: str


class RarityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desirable_trim_or_spec: bool
    classic_or_collector: bool
    desirability_signals: list[str]
    desirability_evidence: list[str]


class EnrichmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normalized_vehicle: NormalizedVehicle
    title_status: TitleStatus
    condition_categorical: Condition
    condition_confidence: float = Field(ge=0, le=1)
    red_flags: list[FlagInstance]
    green_flags: list[FlagInstance]
    showstopper_flags: list[ShowstopperInstance]
    carfax_url: str | None
    summary: str
    description_quality: DescriptionQuality
    rarity: RarityAssessment


class CarfaxFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accident_count: int
    accident_severity_max: Literal["minor", "moderate", "severe", "none"]
    service_record_density: Literal["none", "sparse", "regular", "dense"]
    ownership_count: int | None
    title_brands: list[str]
    odometer_consistency: Literal["consistent", "rollback_suspected", "unknown"]


class PerImageFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "rust", "dent", "scratch", "paint_mismatch", "panel_gap",
        "interior_wear", "stain", "other",
    ]
    location: str
    severity: int = Field(ge=1, le=3)
    confidence: int = Field(ge=1, le=5)
    reasoning: str


class PerImageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_type: Literal[
        "exterior_front", "exterior_rear", "exterior_side", "interior",
        "engine_bay", "wheel", "undercarriage", "document", "other",
    ]
    image_quality_sharpness: Literal["sharp", "blurry"]
    image_quality_lighting: Literal["well_lit", "dim", "harsh_shadow"]
    image_quality_cleanliness: Literal["clean", "dirty"]
    visible_panels: list[str]
    findings: list[PerImageFinding]
    explicit_unknowns: list[str]


class ExpandedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    make: str
    model: str
    year_min: int | None
    year_max: int | None
    trims: list[str]
    reason: str  # one line: why this model fits the archetype (shown in the table)


class ArchetypeExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ExpandedModel]


class VisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage_gaps: list[str]
    cross_panel_paint_consistency: Literal[
        "consistent", "inconsistent", "cannot_assess",
    ]
    staging_signals: list[str]
    overall_red_flags: list[str]
    overall_green_flags: list[str]
    exterior_condition: Condition
    interior_condition: Condition
    overall_vision_condition: Condition
    vision_confidence: float = Field(ge=0, le=1)
    contradictions_with_description: list[str]
