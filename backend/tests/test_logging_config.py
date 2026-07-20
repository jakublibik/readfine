"""Tests for root logging setup.

Uvicorn leaves the root logger unconfigured, which silently dropped every
INFO record the app wrote — including the LOG_OUTBOUND_REQUESTS diagnostic,
whose whole purpose is to be readable. These pin the wiring down.
"""
import logging

import pytest

from app.logging_config import configure_logging


@pytest.fixture(autouse=True)
def restore_logging():
    """configure_logging() mutates global state; put it back for other tests."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    watched = ["httpx", "httpcore", "apscheduler", "urllib3",
               "app.utils.url_validator", "app.services.readable_service"]
    saved_levels = {name: logging.getLogger(name).level for name in watched}
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def _configure(monkeypatch, level="WARNING", outbound=False):
    from app.config import settings
    monkeypatch.setattr(settings, "log_level", level, raising=False)
    monkeypatch.setattr(settings, "log_outbound_requests", outbound, raising=False)
    configure_logging()


class TestLogLevel:
    def test_root_gets_a_handler(self, monkeypatch):
        # Without one, warnings fall through logging.lastResort unformatted.
        _configure(monkeypatch)
        assert logging.getLogger().handlers

    def test_default_drops_info_keeps_warning(self, monkeypatch):
        _configure(monkeypatch, level="WARNING")
        app_logger = logging.getLogger("app.services.feed")
        assert not app_logger.isEnabledFor(logging.INFO)
        assert app_logger.isEnabledFor(logging.WARNING)

    def test_info_level_lets_app_info_through(self, monkeypatch):
        _configure(monkeypatch, level="INFO")
        assert logging.getLogger("app.services.feed").isEnabledFor(logging.INFO)

    def test_level_is_case_insensitive(self, monkeypatch):
        _configure(monkeypatch, level="  info  ")
        assert logging.getLogger("app.services.feed").isEnabledFor(logging.INFO)

    def test_unknown_level_falls_back_to_warning(self, monkeypatch):
        _configure(monkeypatch, level="chatty")
        app_logger = logging.getLogger("app.services.feed")
        assert app_logger.isEnabledFor(logging.WARNING)
        assert not app_logger.isEnabledFor(logging.INFO)

    def test_noisy_libraries_stay_quiet_at_info(self, monkeypatch):
        # httpx logs a line per request, which would drown the outbound log.
        _configure(monkeypatch, level="INFO")
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert not logging.getLogger("apscheduler").isEnabledFor(logging.INFO)

    def test_debug_level_does_not_mute_libraries_further(self, monkeypatch):
        _configure(monkeypatch, level="DEBUG")
        assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


class TestOutboundSwitch:
    def test_switch_lifts_its_loggers_above_the_default_level(self, monkeypatch):
        # The switch is the feature: it must show its records at LOG_LEVEL=WARNING.
        _configure(monkeypatch, level="WARNING", outbound=True)
        assert logging.getLogger("app.utils.url_validator").isEnabledFor(logging.INFO)
        assert logging.getLogger("app.services.readable_service").isEnabledFor(logging.INFO)

    def test_switch_off_leaves_them_at_the_default_level(self, monkeypatch):
        _configure(monkeypatch, level="WARNING", outbound=False)
        assert not logging.getLogger("app.utils.url_validator").isEnabledFor(logging.INFO)

    def test_other_app_loggers_are_unaffected_by_the_switch(self, monkeypatch):
        _configure(monkeypatch, level="WARNING", outbound=True)
        assert not logging.getLogger("app.services.feed").isEnabledFor(logging.INFO)
