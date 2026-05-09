from carbuyer.shared.logging import configure_logging, get_logger


def test_get_logger_returns_bound_logger() -> None:
    configure_logging("DEBUG")
    log = get_logger("test")
    assert log is not None
    log.info("hello", key="value")  # should not raise


def test_configure_logging_default_reads_settings() -> None:
    # Calling with no level argument falls back to settings.log_level (default INFO).
    configure_logging()
    log = get_logger("test")
    log.info("ok")
