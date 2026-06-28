"""Typed criteria for a user want-list entry (a saved search / target archetype).

Stored as the JSONB `config` on the `searches` table (carbuyer.db.models.Search),
which until now was untyped scaffolding. A want fans out across makes/models/trims
(empty list = "any"), so one entry can express either a precise target (a single
make+model+transmission) or a broad archetype (several models sharing a price
ceiling and region). The matcher (carbuyer.wants.matcher) turns this into a query
against the offer columns; field names line up with the lot/auction columns and
the existing `feed.py` filters so that query stays a thin translation.

Transmission/Drivetrain/Condition reuse the enricher's literals
(carbuyer.llm.schemas) — the same vocabulary the LLM normalizes listings into, so
a want and a listing speak the same language with no mapping layer.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from carbuyer.llm.schemas import Condition, Drivetrain, Transmission


def _split_csv(value: str | None) -> list[str]:
    """Comma-separated free-text (slash command / web form) → list, blanks dropped."""
    return [part.strip() for part in value.split(",") if part.strip()] if value else []


def first_error(exc: ValidationError) -> str:
    """A short 'field: message' summary of the first error, for user-facing replies."""
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "invalid value")
    return f"{loc}: {msg}" if loc else msg


class WantCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Vehicle identity. Empty list = "any" for that field; multiple values fan
    # the want out (e.g. models=["GX 470","4Runner"] for a cross-platform want).
    makes: list[str] = []
    models: list[str] = []
    trims: list[str] = []
    transmissions: list[Transmission] = []
    drivetrains: list[Drivetrain] = []

    year_min: int | None = None
    year_max: int | None = None
    price_ceiling_cad: int | None = Field(default=None, gt=0)
    max_mileage_km: int | None = Field(default=None, gt=0)
    provinces: list[str] = []
    condition_min: Condition | None = None

    hide_showstoppers: bool = True

    @classmethod
    def from_inputs(
        cls,
        *,
        makes: str | None = None,
        models: str | None = None,
        trims: str | None = None,
        transmissions: str | None = None,
        drivetrains: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        max_price_cad: int | None = None,
        max_mileage_km: int | None = None,
        provinces: str | None = None,
        condition_min: str | None = None,
    ) -> WantCriteria:
        """Build from raw string inputs (slash command / web form). List fields are
        comma-separated; raises pydantic.ValidationError on bad values. Uses
        model_validate so runtime-validated strings don't trip the list[Literal]
        field types statically."""
        return cls.model_validate({
            "makes": _split_csv(makes),
            "models": _split_csv(models),
            "trims": _split_csv(trims),
            "transmissions": _split_csv(transmissions),
            "drivetrains": _split_csv(drivetrains),
            "year_min": year_min,
            "year_max": year_max,
            "price_ceiling_cad": max_price_cad,
            "max_mileage_km": max_mileage_km,
            "provinces": _split_csv(provinces),
            "condition_min": condition_min or None,
        })

    @model_validator(mode="after")
    def _year_range_ordered(self) -> WantCriteria:
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError("year_min must not be greater than year_max")
        return self
