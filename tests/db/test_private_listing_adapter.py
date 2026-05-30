from __future__ import annotations

from decimal import Decimal

from carbuyer.db.models import PrivateListing, SavedSearch
from carbuyer.db.saved_searches import MatchableListing, adapt_private_listing, match_listing


def test_adapt_maps_fields_and_kind() -> None:
    pl = PrivateListing(
        id=7, source="kijiji", source_listing_id="L1",
        url="u", canonical_url="u",
        make="Ford", model="Mustang", year=1968, trim="Fastback",
        mileage_km=90_000, title_status="NORMAL", condition_categorical="good",
        pickup_province="AB", all_in_cost_cad=Decimal("25000.50"), rarity_score=2.1,
    )
    m = adapt_private_listing(pl)
    assert isinstance(m, MatchableListing)
    assert m.source_kind == "private_listing"
    assert m.source_id == 7  # noqa: PLR2004
    assert m.make == "Ford" and m.model == "Mustang" and m.year == 1968  # noqa: PLR2004
    assert m.province == "AB"
    assert m.all_in_cost_cad == 25_001  # noqa: PLR2004  # ceil, like adapt_auction_lot
    assert m.rarity_score == 2.1  # noqa: PLR2004


def test_adapt_none_all_in_cost() -> None:
    pl = PrivateListing(id=8, source="kijiji", source_listing_id="L2",
                        url="u", canonical_url="u", make="Ford")
    assert adapt_private_listing(pl).all_in_cost_cad is None


def test_adapted_listing_matches_a_saved_search() -> None:
    pl = PrivateListing(id=9, source="kijiji", source_listing_id="L3",
                        url="u", canonical_url="u", make="Ford", model="Mustang",
                        year=1968, pickup_province="AB")
    s = SavedSearch(name="stangs", make="Ford", model="Mustang", province=["AB"])
    assert match_listing(adapt_private_listing(pl), s) is True
    s_bc = SavedSearch(name="bc", make="Ford", province=["BC"])
    assert match_listing(adapt_private_listing(pl), s_bc) is False
