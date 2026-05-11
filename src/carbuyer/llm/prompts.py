# ruff: noqa: E501 — prompts are paragraph text; mid-sentence wraps hurt readability and confuse the LLM.
"""Prompt assembly for the description-enrichment LLM call.

Phase 3 design overlay #13: the system prompt is assembled once at
`OpenAIProvider` construction (not regenerated per call). With the expanded
taxonomy this prompt is several KB; OpenAI's prompt caching kicks in at >=1024
tokens of identical prefix and gives 50% off cached input. Same content per
call → cache routing hits.

Phase 3 design overlay #14: the prompt does NOT contain the
`condition_confidence < 0.5 → "decent"` clamp rule (which was in the original
plan). The prompt now treats `condition_categorical` as the model's honest
estimate; the enricher worker applies the clamp in code and sets
`condition_inferred_from_sparse_listing=True` so Phase 4 valuation can
distinguish "actually decent" from "we don't know" — domain reviewer's note
that the prompt-side clamp pollutes the comp-position math.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from carbuyer.flags.taxonomy import (
    CLASSIC_EXCEPTIONS,
    DESIRABLE_TRIMS,
    GREEN_FLAG_TAXONOMY,
    RED_FLAG_TAXONOMY,
    SHOWSTOPPER_TAXONOMY,
    model_gotchas_for,
)


def _bullet(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def description_system_prompt() -> str:
    """Build the static system prompt embedding the full taxonomy.

    Stable across calls — assemble once and cache (see OpenAIProvider).
    """
    red = _bullet([
        f"{f['flag']} (weight {f['weight']}): {f['description']}"
        for f in RED_FLAG_TAXONOMY
    ])
    green = _bullet([
        f"{f['flag']} (weight {f['weight']}): {f['description']}"
        for f in GREEN_FLAG_TAXONOMY
    ])
    show = _bullet([
        f"{f['flag']}: {f['description']}" for f in SHOWSTOPPER_TAXONOMY
    ])
    desirable = _bullet([
        f"{e['make']} {e['model']} {e['trim']} — {e['note']}"
        for e in DESIRABLE_TRIMS
    ])
    classics = _bullet([
        (
            f"{e['make']} {e['model']} "
            f"({e['year_min']}-{e['year_max']}) — {e['note']}"
        )
        for e in CLASSIC_EXCEPTIONS
    ])
    return f"""You enrich auction lot listings into structured JSON for a used-vehicle deal-finder.

Use ONLY the flag taxonomies below. Do not invent new flags. The taxonomy is
versioned and downstream systems depend on flag keys matching exactly.

RED FLAGS:
{red}

GREEN FLAGS:
{green}

SHOWSTOPPER FLAGS — when any apply, the listing is excluded from notifications regardless of price. Be conservative; require explicit phrasing.
{show}

DESIRABLE TRIMS / SPEC COMBOS — set `desirable_trim_or_spec=true` when the lot matches one. Trim "any" means any trim of that make/model qualifies.
{desirable}

CLASSIC / COLLECTOR EXCEPTIONS — set `classic_or_collector=true` ONLY when the lot matches one of these entries. Pre-2000 is "old", not "classic" — do not auto-classic random vehicles.
{classics}

GENERAL RULES:
- For `transmission`, `drivetrain`, `title_status`: output `unknown` / `UNKNOWN` when you cannot determine the field. Do not guess.
- For `condition_categorical`: output your honest categorical estimate from {{bad, poor, decent, good, great}} with a paired `condition_confidence` in [0, 1]. Do NOT default to "decent" when uncertain — set `condition_confidence` low instead so downstream systems know the rating is sparse-evidence.
- For each flag in `red_flags`/`green_flags`: use the exact taxonomy `flag` key and copy the corresponding `weight` value verbatim from the taxonomy. Provide `evidence` quoted verbatim from the listing — do not paraphrase. If you can't quote, do not fire the flag.
- For `description_quality`: output `thin` for listings with <100 chars or no condition / mileage / service mention; `detailed` for listings with explicit condition + service history + photo coverage; `adequate` otherwise.
- For `summary`: 1-3 sentences, factual, no marketing tone.
- If no MODEL-SPECIFIC GOTCHAS block follows in the user message, do not invent gotchas the model wasn't told about.
"""


def description_user_prompt(
    *,
    title: str,
    description: str,
    year: int | None,
    make: str | None,
    model: str | None,
    auctioneer_name: str | None,
    auction_subtype: str,
    pickup_province: str | None,
    current_high_bid_cad: Decimal | None,
    bid_increment: Decimal | None,
    auction_close_at: datetime | None,
    is_no_reserve: bool,
    image_count: int,
    current_year: int,
) -> str:
    """Build the per-lot user message. All listing-context goes here so the
    system prompt stays stable for prompt caching."""
    gotchas = model_gotchas_for(make=make, model=model, year=year)
    gotcha_block = ""
    if gotchas:
        gotcha_block = (
            "\n\nMODEL-SPECIFIC GOTCHAS for this make/model/year — surface "
            "as red_flags or green_flags if the listing addresses or omits "
            "them:\n" + _bullet(gotchas)
        )
    bid_str = (
        f"{current_high_bid_cad} CAD" if current_high_bid_cad is not None else "no_bid_yet"
    )
    increment_str = str(bid_increment) if bid_increment is not None else "unknown"
    close_str = (
        auction_close_at.isoformat() if auction_close_at is not None else "unknown"
    )
    return f"""TITLE: {title}

DESCRIPTION:
{description}

CONTEXT:
- current_year: {current_year}
- listing_year: {year}, make: {make}, model: {model}
- auctioneer: {auctioneer_name}
- auction_subtype: {auction_subtype}
- pickup_province: {pickup_province}
- current_high_bid_cad: {bid_str}
- bid_increment_cad: {increment_str}
- auction_close_at: {close_str}
- is_no_reserve: {is_no_reserve}
- image_count: {image_count}{gotcha_block}

Return the structured EnrichmentOutput.
"""


VISION_PER_IMAGE_PROMPT = """You are inspecting a single photo of a used vehicle for a deal-finder.

Output the structured PerImageOutput. Rules:
- Set explicit_unknowns for anything you cannot judge from THIS image alone.
- Do not guess. Output `unknown`-equivalent values when uncertain.
- Severity: 1=cosmetic, 2=needs repair, 3=structural / safety.
- Confidence: 1=very unsure, 5=certain.
"""


VISION_AGGREGATION_PROMPT = """You are aggregating per-image findings (JSON only, no images) into an overall VisionOutput for a single vehicle.

Rules:
- coverage_gaps: list standard angles missing (e.g., "no engine bay shot", "no undercarriage").
- cross_panel_paint_consistency only "consistent"/"inconsistent" if the same panel appears in 2+ shots; else "cannot_assess".
- staging_signals: pro photography, perfect lighting, no underbody close-ups.
- contradictions_with_description: list specific contradictions with the supplied description condition / flags.
- Set overall_vision_condition pessimistically when finding severity 3 items.
"""
