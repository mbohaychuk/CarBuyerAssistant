from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Search
from carbuyer.wants.criteria import ModelSpec, WantCriteria


def test_model_spec_round_trips_through_config() -> None:
    c = WantCriteria(
        archetype_text="cheap reliable 4runner-platform offroad",
        model_specs=[
            ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009, trims=[]),
            ModelSpec(make="Toyota", model="4Runner", year_min=2003, year_max=2009, trims=["SR5", "TRD"]),
        ],
        price_ceiling_cad=18000,
        provinces=["AB", "BC"],
    )
    dumped = c.model_dump(mode="json")
    restored = WantCriteria.model_validate(dumped)
    assert restored.archetype_text == "cheap reliable 4runner-platform offroad"
    assert len(restored.model_specs) == 2
    assert restored.model_specs[1].trims == ["SR5", "TRD"]


def test_legacy_flat_config_still_validates() -> None:
    # A want created before this feature has no archetype_text / model_specs.
    legacy = {"makes": ["Nissan"], "models": ["Xterra"], "transmissions": ["manual"]}
    c = WantCriteria.model_validate(legacy)
    assert c.makes == ["Nissan"]
    assert c.model_specs == []
    assert c.archetype_text is None


def test_model_spec_year_order_validated() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ModelSpec(make="Lexus", model="GX 470", year_min=2010, year_max=2003)


def _manual_xterra() -> WantCriteria:
    """The owner's 'manual-only Xterra' want, used across tests."""
    return WantCriteria(
        makes=["Nissan"],
        models=["Xterra"],
        trims=["PRO-4X"],
        transmissions=["manual"],
        drivetrains=["4wd"],
        year_min=2005,
        year_max=2015,
        price_ceiling_cad=15000,
        max_mileage_km=250000,
        provinces=["AB", "BC"],
        condition_min="decent",
    )


def test_want_criteria_round_trip() -> None:
    want = _manual_xterra()
    reparsed = WantCriteria.model_validate(want.model_dump(mode="json"))
    assert reparsed == want
    assert reparsed.transmissions == ["manual"]
    assert reparsed.hide_showstoppers is True  # defaulted, not set above


def test_want_criteria_defaults_to_match_anything() -> None:
    want = WantCriteria()
    assert want.makes == []
    assert want.models == []
    assert want.year_min is None
    assert want.price_ceiling_cad is None
    assert want.condition_min is None
    assert want.hide_showstoppers is True


def test_want_criteria_forbids_extra_keys() -> None:
    payload = _manual_xterra().model_dump(mode="json")
    payload["junk"] = True
    with pytest.raises(ValidationError):
        WantCriteria.model_validate(payload)


def test_want_criteria_rejects_year_min_after_max() -> None:
    with pytest.raises(ValidationError):
        WantCriteria(year_min=2015, year_max=2005)


def test_want_criteria_rejects_nonpositive_price_ceiling() -> None:
    with pytest.raises(ValidationError):
        WantCriteria(price_ceiling_cad=0)


def test_want_criteria_rejects_nonpositive_mileage() -> None:
    with pytest.raises(ValidationError):
        WantCriteria(max_mileage_km=-1)


def test_want_criteria_rejects_invalid_transmission() -> None:
    with pytest.raises(ValidationError):
        WantCriteria.model_validate({"transmissions": ["stick"]})


def test_from_inputs_is_case_insensitive_for_literal_fields() -> None:
    # make/model/trim/province are matched case-insensitively downstream; the
    # Literal-typed fields must accept mixed case at the input boundary too.
    crit = WantCriteria.from_inputs(
        makes="Nissan", transmissions="Manual", drivetrains="4WD", condition_min="Good"
    )
    assert crit.transmissions == ["manual"]
    assert crit.drivetrains == ["4wd"]
    assert crit.condition_min == "good"


def test_from_inputs_rejects_unknown_literal() -> None:
    with pytest.raises(ValidationError):
        WantCriteria.from_inputs(transmissions="stick")


async def test_search_config_round_trips_want_criteria(session: AsyncSession) -> None:
    """A WantCriteria survives a write/read through searches.config (JSONB)."""
    want = _manual_xterra()
    search = Search(name="manual xterra", config=want.model_dump(mode="json"))
    session.add(search)
    await session.commit()
    search_id = search.id

    session.expire_all()
    fetched = (
        await session.execute(select(Search).where(Search.id == search_id))
    ).scalar_one()

    assert WantCriteria.model_validate(fetched.config) == want
