from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import FilteringBoundLogger

from carbuyer.shared.config import settings


# stdout JSON only — systemd captures via journald in prod; do not add FileHandler.
def configure_logging(level: str | None = None) -> None:
    effective = (level or settings.log_level).upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, effective),
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.contextvars.merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, effective)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **initial_values: Any) -> FilteringBoundLogger:
    return structlog.get_logger(name).bind(**initial_values)
