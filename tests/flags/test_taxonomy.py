from carbuyer.flags.taxonomy import (
    CLASSIC_EXCEPTIONS,
    DESIRABLE_TRIMS,
    GOTCHAS,
    GREEN_FLAG_TAXONOMY,
    RED_FLAG_TAXONOMY,
    SHOWSTOPPER_TAXONOMY,
    is_classic,
    is_desirable_trim,
    model_gotchas_for,
)

# Phase 3 design overlay #23: taxonomies expanded post-deliberation. These
# minimums are the as-shipped seed sizes; bumps must update the asserts so the
# next reviewer can see the seed grew (or the diff justifies why it shrank).
MIN_RED_FLAGS = 26
MIN_GREEN_FLAGS = 16
MIN_SHOWSTOPPERS = 9
MIN_DESIRABLE = 25
MIN_CLASSIC = 25
MIN_GOTCHAS = 27


def test_red_flag_taxonomy_minimum_size() -> None:
    assert len(RED_FLAG_TAXONOMY) >= MIN_RED_FLAGS


def test_green_flag_taxonomy_minimum_size() -> None:
    assert len(GREEN_FLAG_TAXONOMY) >= MIN_GREEN_FLAGS


def test_showstopper_taxonomy_minimum_size() -> None:
    assert len(SHOWSTOPPER_TAXONOMY) >= MIN_SHOWSTOPPERS


def test_red_flag_weights_negative() -> None:
    assert all(f["weight"] < 0 for f in RED_FLAG_TAXONOMY)


def test_green_flag_weights_positive() -> None:
    assert all(f["weight"] > 0 for f in GREEN_FLAG_TAXONOMY)


def test_red_flag_keys_unique() -> None:
    keys = [f["flag"] for f in RED_FLAG_TAXONOMY]
    assert len(keys) == len(set(keys))


def test_green_flag_keys_unique() -> None:
    keys = [f["flag"] for f in GREEN_FLAG_TAXONOMY]
    assert len(keys) == len(set(keys))


def test_needs_work_weight_recalibrated_to_minus_one() -> None:
    """Phase 3 overlay #23: 'needs_work' fires on ~80% of lots — diluted to -1
    so it doesn't dominate scoring."""
    needs_work = next(f for f in RED_FLAG_TAXONOMY if f["flag"] == "needs_work")
    assert needs_work["weight"] == -1


def test_frame_rust_distinct_from_surface_rust() -> None:
    """Western Canada cosmetic vs structural rust must be separable."""
    flags = {f["flag"] for f in RED_FLAG_TAXONOMY}
    assert "frame_rust" in flags
    assert "rust_mentioned" in flags
    frame = next(f for f in RED_FLAG_TAXONOMY if f["flag"] == "frame_rust")
    assert frame["weight"] <= -3  # noqa: PLR2004 -- domain calibration


def test_wont_start_is_red_flag_not_showstopper() -> None:
    """Phase 3 overlay #23: 'won't start' often means dead battery on RB lots
    — moved from showstopper to -3 red flag. Showstopper reserved for explicit
    'engine_seized' / 'for_parts_only'."""
    showstopper_keys = {f["flag"] for f in SHOWSTOPPER_TAXONOMY}
    red_keys = {f["flag"] for f in RED_FLAG_TAXONOMY}
    assert "wont_start" not in showstopper_keys
    assert "wont_start" in red_keys
    assert "engine_seized" in showstopper_keys
    assert "for_parts_only" in showstopper_keys


def test_as_is_no_warranty_replaced_with_specific_showstopper() -> None:
    """Bare 'as_is_no_warranty' fires on every online auction. Replaced with
    'seller_says_for_parts_only' which requires explicit phrasing."""
    keys = {f["flag"] for f in SHOWSTOPPER_TAXONOMY}
    assert "as_is_no_warranty" not in keys
    assert "seller_says_for_parts_only" in keys


def test_critical_red_flag_additions_present() -> None:
    keys = {f["flag"] for f in RED_FLAG_TAXONOMY}
    for required in (
        "no_keys", "bill_of_sale_only", "transmission_slipping",
        "head_gasket_suspected", "diesel_emissions_deleted",
    ):
        assert required in keys, f"missing {required}"


def test_critical_showstopper_additions_present() -> None:
    keys = {f["flag"] for f in SHOWSTOPPER_TAXONOMY}
    for required in (
        "vin_mismatch", "stolen_recovered", "no_title",
        "non_repairable_brand", "engine_seized", "for_parts_only",
        "flood_damage_total",
    ):
        assert required in keys, f"missing {required}"


def test_salvage_outstanding_lien_lemon_buyback_are_red_not_showstopper() -> None:
    """Phase 3 review: these aren't dispositive for flippers / wholesale
    buyers / post-fix retail. Demoted to -4 red flags."""
    show_keys = {f["flag"] for f in SHOWSTOPPER_TAXONOMY}
    red_keys = {f["flag"] for f in RED_FLAG_TAXONOMY}
    for f in ("salvage_not_rebuilt", "outstanding_lien", "lemon_law_buyback"):
        assert f not in show_keys, f"{f} should not be a showstopper"
        assert f in red_keys, f"{f} should be a red flag"
    salvage = next(f for f in RED_FLAG_TAXONOMY if f["flag"] == "salvage_not_rebuilt")
    assert salvage["weight"] == -4  # noqa: PLR2004 -- domain calibration


def test_flood_damage_split_total_vs_partial() -> None:
    show_keys = {f["flag"] for f in SHOWSTOPPER_TAXONOMY}
    red_keys = {f["flag"] for f in RED_FLAG_TAXONOMY}
    assert "flood_damage_total" in show_keys
    assert "flood_damage_partial" in red_keys
    assert "flood_damage" not in show_keys  # original generic removed


def test_critical_green_flag_additions_present() -> None:
    keys = {f["flag"] for f in GREEN_FLAG_TAXONOMY}
    for required in (
        "non_smoker", "from_southern_climate", "warranty_remaining",
        "cpo_certified", "two_sets_of_tires",
    ):
        assert required in keys, f"missing {required}"


def test_desirable_trims_includes_known_examples() -> None:
    assert any("TRD Pro" in entry["trim"] for entry in DESIRABLE_TRIMS)
    assert any("Raptor" in entry["trim"] for entry in DESIRABLE_TRIMS)
    assert any("Type R" in entry["trim"] for entry in DESIRABLE_TRIMS)
    assert len(DESIRABLE_TRIMS) >= MIN_DESIRABLE


def test_desirable_trims_does_not_contain_bare_z71() -> None:
    """Phase 3 overlay #23: Z71 is a package, not a desirable trim."""
    bare_z71 = [
        e for e in DESIRABLE_TRIMS
        if e["trim"].strip() == "Z71" and "1500" in e["model"]
    ]
    assert bare_z71 == []


def test_classic_exceptions_includes_known_examples() -> None:
    assert any(e["model"].startswith("Supra") for e in CLASSIC_EXCEPTIONS)
    assert any(e["model"] == "NSX" for e in CLASSIC_EXCEPTIONS)
    assert any(e["model"] == "RX-7" for e in CLASSIC_EXCEPTIONS)
    assert len(CLASSIC_EXCEPTIONS) >= MIN_CLASSIC


def test_is_classic_returns_false_for_random_pre_2000() -> None:
    """Phase 3 overlay #23: pre-2000 default flips — 'old' is not 'classic'."""
    assert is_classic(make="Honda", model="Civic", year=1996) is False


def test_is_classic_returns_true_for_listed_model() -> None:
    assert is_classic(make="Toyota", model="Supra", year=1995) is True
    assert is_classic(make="Mazda", model="RX-7", year=1995) is True


def test_is_desirable_trim_matches_canonical_form() -> None:
    assert is_desirable_trim(
        make="Toyota", model="Tacoma", trim="TRD Pro",
    ) is True
    assert is_desirable_trim(make="Honda", model="Civic", trim="LX") is False


def test_gotchas_minimum_size_with_diesel_set() -> None:
    """Phase 3 overlay #23: gotchas expanded to include the diesel powertrain
    failure set that dominates Western Canada auction yards."""
    assert len(GOTCHAS) >= MIN_GOTCHAS


def test_gotcha_lookup_is_case_and_punctuation_insensitive() -> None:
    g = model_gotchas_for(make="ford", model="F150", year=2013)
    assert any("CP4" in note or "EcoBoost" in note for note in g)


def test_gotcha_lookup_returns_empty_when_missing_inputs() -> None:
    assert model_gotchas_for(make=None, model="Tacoma", year=2010) == []
    assert model_gotchas_for(make="Toyota", model=None, year=2010) == []
    assert model_gotchas_for(make="Toyota", model="Tacoma", year=None) == []


def test_diesel_powertrain_gotchas_present() -> None:
    """The CP4 fuel pump failure on 6.7L PowerStroke / Duramax LML is the
    single biggest budget hit on auction-yard diesels."""
    powerstroke_67 = model_gotchas_for(make="Ford", model="F-350", year=2014)
    assert any("CP4" in note for note in powerstroke_67)
    duramax_lml = model_gotchas_for(make="Chevrolet", model="Silverado 2500", year=2014)
    assert any("CP4" in note for note in duramax_lml)
