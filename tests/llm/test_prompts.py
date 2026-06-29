from datetime import UTC, datetime
from decimal import Decimal

from carbuyer.llm.prompts import description_system_prompt, description_user_prompt


def test_system_prompt_includes_full_taxonomy() -> None:
    prompt = description_system_prompt()
    # Sample entries from each category — confirms the taxonomy is interpolated.
    assert "engine_knock" in prompt
    assert "needs_work" in prompt
    assert "frame_rust" in prompt
    assert "wont_start" in prompt
    assert "no_accidents_carfax" in prompt
    assert "from_southern_climate" in prompt
    assert "engine_seized" in prompt
    assert "TRD Pro" in prompt
    assert "Supra" in prompt


def test_system_prompt_does_not_contain_old_decent_clamp() -> None:
    """Phase 3 design overlay #14: prompt-side clamp removed; code-side now."""
    prompt = description_system_prompt()
    assert "condition_categorical = \"decent\"" not in prompt
    assert "set `condition_confidence` low" in prompt


def test_system_prompt_explicit_about_evidence_verbatim() -> None:
    prompt = description_system_prompt()
    assert "verbatim" in prompt.lower()
    assert "do not paraphrase" in prompt.lower()


def test_user_prompt_carries_phase3_context() -> None:
    """Phase 3 design overlay #24: bid state, close time, image count, current
    year must be in the user prompt so the LLM can reason about urgency,
    priced-below-scrap, and listing sparsity."""
    user = description_user_prompt(
        title="2010 Ford F-150 4x4",
        description="runs and drives",
        year=2010, make="Ford", model="F-150",
        auctioneer_name="ABC Auctions", auction_subtype="estate",
        pickup_province="AB",
        current_high_bid_cad=Decimal("3200"),
        bid_increment=Decimal("100"),
        auction_close_at=datetime(2026, 5, 15, 18, tzinfo=UTC),
        is_no_reserve=True,
        image_count=8,
        current_year=2026,
    )
    assert "current_year: 2026" in user
    assert "current_high_bid_cad: 3200" in user
    assert "is_no_reserve: True" in user
    assert "image_count: 8" in user
    assert "auction_close_at:" in user


def test_user_prompt_includes_gotcha_block_when_match() -> None:
    user = description_user_prompt(
        title="2014 F-350", description="diesel",
        year=2014, make="Ford", model="F-350",
        auctioneer_name=None, auction_subtype="commercial",
        pickup_province="AB",
        current_high_bid_cad=None, bid_increment=None,
        auction_close_at=None, is_no_reserve=False,
        image_count=2, current_year=2026,
    )
    assert "MODEL-SPECIFIC GOTCHAS" in user
    assert "CP4" in user


def test_user_prompt_omits_gotcha_block_when_no_match() -> None:
    user = description_user_prompt(
        title="1996 Buick Roadmaster", description="ran ok",
        year=1996, make="Buick", model="Roadmaster",
        auctioneer_name=None, auction_subtype="estate",
        pickup_province="SK",
        current_high_bid_cad=None, bid_increment=None,
        auction_close_at=None, is_no_reserve=False,
        image_count=1, current_year=2026,
    )
    assert "MODEL-SPECIFIC GOTCHAS" not in user


def test_archetype_prompt_seeds_from_taxonomy() -> None:
    from carbuyer.llm.prompts import archetype_system_prompt
    p = archetype_system_prompt()
    # Seeded with the desirable-trims taxonomy so the model knows platform
    # relationships; spot-check a known entry.
    assert "GX 470" in p or "GX470" in p
    assert "year" in p.lower()
