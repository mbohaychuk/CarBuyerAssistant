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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from carbuyer.llm.schemas import Condition, Drivetrain, Transmission


class WantCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Vehicle identity. Empty list = "any" for that field; multiple values fan
    # the want out (e.g. models=["GX 470","4Runner"] for a cross-platform want).
    makes: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    trims: list[str] = Field(default_factory=list)
    transmissions: list[Transmission] = []
    drivetrains: list[Drivetrain] = []

    year_min: int | None = None
    year_max: int | None = None
    price_ceiling_cad: int | None = Field(default=None, gt=0)
    max_mileage_km: int | None = Field(default=None, gt=0)
    provinces: list[str] = Field(default_factory=list)
    condition_min: Condition | None = None

    hide_showstoppers: bool = True

    @model_validator(mode="after")
    def _year_range_ordered(self) -> WantCriteria:
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError("year_min must not be greater than year_max")
        return self
