"""WG1 — coarse pre-match: a cheap "could this raw offer match any active want?"
gate that runs on scraped fields (parsed make/model/year OR the raw title) before
any LLM enrichment. Lenient: unknown LLM-only fields never exclude; the precise
matcher still runs post-enrichment to create the actual want_match."""
from __future__ import annotations

from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.matcher import could_match_any_want


def _c(**kw: object) -> WantCriteria:
    return WantCriteria(**kw)  # type: ignore[arg-type]


def test_matches_on_parsed_make_and_model() -> None:
    crit = [_c(makes=["Nissan"], models=["Xterra"])]
    assert could_match_any_want(
        make="Nissan", model="Xterra", year=2010, title="", criteria_list=crit
    )


def test_matches_via_title_when_make_model_unparsed() -> None:
    # The scraper didn't structure make/model, but the title carries them.
    crit = [_c(makes=["Nissan"], models=["Xterra"])]
    assert could_match_any_want(
        make=None, model=None, year=None,
        title="2010 Nissan Xterra PRO-4X 4WD", criteria_list=crit,
    )


def test_rejects_non_matching_make() -> None:
    crit = [_c(makes=["Nissan"], models=["Xterra"])]
    assert not could_match_any_want(
        make="Honda", model="Civic", year=2015,
        title="2015 Honda Civic EX", criteria_list=crit,
    )


def test_year_out_of_range_rejected_when_known() -> None:
    crit = [_c(makes=["Nissan"], year_min=2012)]
    assert not could_match_any_want(
        make="Nissan", model="Xterra", year=2010, title="", criteria_list=crit
    )


def test_year_lenient_when_unknown() -> None:
    crit = [_c(makes=["Nissan"], year_min=2012)]
    assert could_match_any_want(
        make="Nissan", model=None, year=None, title="Nissan Xterra", criteria_list=crit
    )


def test_matches_if_any_want_matches() -> None:
    crit = [_c(makes=["Honda"]), _c(makes=["Nissan"])]
    assert could_match_any_want(
        make="Nissan", model="Xterra", year=2010, title="", criteria_list=crit
    )


def test_make_only_want_matches_any_model() -> None:
    crit = [_c(makes=["Toyota"])]  # no models named → any Toyota
    assert could_match_any_want(
        make="Toyota", model="Tacoma", year=2015, title="", criteria_list=crit
    )


def test_lenient_on_llm_only_field() -> None:
    # transmission is an LLM-derived field, unknown at the raw stage → must not
    # exclude a make/model/year match.
    crit = [_c(makes=["Nissan"], models=["Xterra"], transmissions=["manual"])]
    assert could_match_any_want(
        make="Nissan", model="Xterra", year=2010, title="", criteria_list=crit
    )


def test_empty_criteria_list_matches_nothing() -> None:
    # No active wants → nothing is wanted (want-first: don't ingest the universe).
    assert not could_match_any_want(
        make="Nissan", model="Xterra", year=2010, title="x", criteria_list=[]
    )


def test_coarse_gate_ors_over_model_specs() -> None:
    from carbuyer.wants.criteria import ModelSpec, WantCriteria
    from carbuyer.wants.matcher import could_match_any_want
    crit = WantCriteria(model_specs=[
        ModelSpec(make="Lexus", model="GX 470", year_min=2003, year_max=2009),
    ])
    assert could_match_any_want(
        make=None, model=None, year=2005,
        title="2005 Lexus GX 470 4x4", criteria_list=[crit],
    ) is True
    assert could_match_any_want(
        make=None, model=None, year=2005,
        title="2005 Toyota Camry", criteria_list=[crit],
    ) is False
