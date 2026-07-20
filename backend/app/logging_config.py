"""Application logging setup.

Uvicorn configures only its own loggers and leaves the root logger without a
handler. Without this module every ``logger.info()`` in the app is dropped, and
warnings fall through ``logging.lastResort`` with no timestamp and no logger
name, so a log line cannot be traced back to the code that wrote it.
"""

import logging

from app.config import settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Libraries that are chatty at INFO and say nothing we don't already record:
# httpx logs a line per request, which the outbound log covers in more detail,
# and APScheduler narrates every job it runs.
_NOISY_LIBRARIES = ("httpx", "httpcore", "apscheduler", "urllib3")

# Loggers carrying the LOG_OUTBOUND_REQUESTS diagnostic. The switch has to lift
# them itself: those records are INFO, and the default level is WARNING, so
# turning the switch on would otherwise change nothing visible.
_OUTBOUND_LOGGERS = ("app.utils.url_validator", "app.services.readable_service")


def configure_logging() -> None:
    """Wire up the root logger. Call once, before the app starts serving."""
    level = getattr(logging, settings.log_level.strip().upper(), None)
    if not isinstance(level, int):
        level = logging.WARNING

    # force=True: uvicorn may already have touched logging by the time we run.
    logging.basicConfig(level=level, format=_FORMAT, force=True)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    if settings.log_outbound_requests:
        for name in _OUTBOUND_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)
