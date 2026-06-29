import pytest
from pydantic import ValidationError

from carbuyer.llm.schemas import (
    CarfaxFindings,
    EnrichmentOutput,
    FlagInstance,
    NormalizedVehicle,
    PerImageOutput,
    RarityAssessment,
    VisionOutput,
)


def _valid_payload() -> dict:
    return {
        "normalized_vehicle": {
            "year": 2010, "make": "Ford", "model": "F-150",
            "trim": None, "engine": "5.4L V8", "transmission": "automatic",
            "drivetrain": "4wd", "mileage_km": 250000, "vin": None,
        },
        "title_status": "NORMAL",
        "condition_categorical": "decent",
        "condition_confidence": 0.7,
        "red_flags": [],
        "green_flags": [],
        "showstopper_flags": [],
        "carfax_url": None,
        "summary": "an older F-150",
        "description_quality": "adequate",
        "rarity": {
            "desirable_trim_or_spec": False, "classic_or_collector": False,
            "desirability_signals": [], "desirability_evidence": [],
        },
    }


def test_enrichment_output_round_trip() -> None:
    out = EnrichmentOutput.model_validate(_valid_payload())
    assert out.normalized_vehicle.year == 2010  # noqa: PLR2004 -- explicit fixture value
    assert out.description_quality == "adequate"
    schema = EnrichmentOutput.model_json_schema()
    assert "properties" in schema


def test_enrichment_output_forbids_extra() -> None:
    payload = _valid_payload()
    payload["junk"] = True
    with pytest.raises(ValidationError):
        EnrichmentOutput.model_validate(payload)


def test_enrichment_output_requires_description_quality() -> None:
    payload = _valid_payload()
    del payload["description_quality"]
    with pytest.raises(ValidationError):
        EnrichmentOutput.model_validate(payload)


def test_condition_literal_does_not_include_unknown() -> None:
    payload = _valid_payload()
    payload["condition_categorical"] = "unknown"
    with pytest.raises(ValidationError):
        EnrichmentOutput.model_validate(payload)


def test_condition_confidence_bounded() -> None:
    payload = _valid_payload()
    payload["condition_confidence"] = 1.5
    with pytest.raises(ValidationError):
        EnrichmentOutput.model_validate(payload)


def test_flag_instance_validates_weight() -> None:
    f = FlagInstance(flag="rust_mentioned", evidence="surface rust on fenders", weight=-1)
    assert f.weight == -1


def test_normalized_vehicle_transmission_unknown_allowed() -> None:
    nv = NormalizedVehicle(
        year=None, make=None, model=None, trim=None, engine=None,
        transmission="unknown", drivetrain="unknown",
        mileage_km=None, vin=None,
    )
    assert nv.transmission == "unknown"


def test_carfax_findings_round_trip() -> None:
    cf = CarfaxFindings(
        accident_count=2,
        accident_severity_max="moderate",
        service_record_density="regular",
        ownership_count=2,
        title_brands=[],
        odometer_consistency="consistent",
    )
    assert cf.accident_count == 2  # noqa: PLR2004 -- explicit fixture value


def test_vision_output_round_trip() -> None:
    vo = VisionOutput(
        coverage_gaps=["undercarriage"],
        cross_panel_paint_consistency="consistent",
        staging_signals=[],
        overall_red_flags=[],
        overall_green_flags=[],
        exterior_condition="decent",
        interior_condition="decent",
        overall_vision_condition="decent",
        vision_confidence=0.6,
        contradictions_with_description=[],
    )
    assert vo.exterior_condition == "decent"


def test_rarity_assessment_round_trip() -> None:
    r = RarityAssessment(
        desirable_trim_or_spec=True,
        classic_or_collector=False,
        desirability_signals=["TRD Pro"],
        desirability_evidence=["title says 'TRD Pro'"],
    )
    assert r.desirable_trim_or_spec is True


def test_archetype_expansion_round_trips() -> None:
    from carbuyer.llm.schemas import ArchetypeExpansion
    payload = {"models": [
        {"make": "Lexus", "model": "GX 470", "year_min": 2003, "year_max": 2009,
         "trims": [], "reason": "J120 4Runner platform, body-on-frame"},
    ]}
    exp = ArchetypeExpansion.model_validate(payload)
    assert exp.models[0].make == "Lexus"
    assert exp.models[0].reason


def test_per_image_output_severity_bounds() -> None:
    payload = {
        "shot_type": "exterior_front",
        "image_quality_sharpness": "sharp",
        "image_quality_lighting": "well_lit",
        "image_quality_cleanliness": "clean",
        "visible_panels": ["hood", "front_bumper"],
        "findings": [{
            "type": "rust", "location": "hood lip", "severity": 4,  # invalid: max 3
            "confidence": 4, "reasoning": "visible orange",
        }],
        "explicit_unknowns": [],
    }
    with pytest.raises(ValidationError):
        PerImageOutput.model_validate(payload)
