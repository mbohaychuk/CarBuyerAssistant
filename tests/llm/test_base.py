import inspect

from carbuyer.llm.base import (
    DescribeInput,
    DescribeProvider,
    LLMProvider,
    VisionInput,
    VisionProvider,
)


def test_describe_input_carries_phase3_context_fields() -> None:
    """Phase 3 design overlay #24: DescribeInput needs current_high_bid_cad,
    bid_increment, auction_close_at, is_no_reserve, image_count, current_year
    so the LLM can reason about urgency, priced-below-scrap signals, and
    description sparsity."""
    fields = {f.name for f in DescribeInput.__dataclass_fields__.values()}
    for required in (
        "title", "description", "year", "make", "model",
        "auctioneer_name", "auction_subtype", "pickup_province",
        "raw_carfax_url", "current_high_bid_cad", "bid_increment",
        "auction_close_at", "is_no_reserve", "image_count", "current_year",
        "lot_id",
    ):
        assert required in fields, f"missing {required}"


def test_describe_provider_is_abstract() -> None:
    assert inspect.isabstract(DescribeProvider)


def test_vision_provider_is_abstract() -> None:
    assert inspect.isabstract(VisionProvider)


def test_llm_provider_is_union_of_describe_and_vision() -> None:
    """Phase 3 design overlay #17: LLMProvider extends both role ABCs so a
    full-capability provider (OpenAI, Anthropic) implements both, while a
    describe-only provider (a future local model) implements just DescribeProvider."""
    assert issubclass(LLMProvider, DescribeProvider)
    assert issubclass(LLMProvider, VisionProvider)


def test_describe_provider_has_async_cm_defaults() -> None:
    """Phase 3 design overlay #17: ABCs get __aenter__/__aexit__ defaults so
    workers can `async with provider:` for clean shutdown."""
    assert hasattr(DescribeProvider, "__aenter__")
    assert hasattr(DescribeProvider, "__aexit__")


def test_vision_input_carries_required_fields() -> None:
    fields = {f.name for f in VisionInput.__dataclass_fields__.values()}
    for required in (
        "photo_paths", "year", "make", "model",
        "description_condition", "description_red_flags",
        "description_green_flags",
    ):
        assert required in fields
