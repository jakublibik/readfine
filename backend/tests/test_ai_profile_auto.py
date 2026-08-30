"""Unit tests for automatic interest-profile regeneration.

Covers the parts that decide whether tokens get spent and whether an existing
profile gets overwritten: output validation, the due/skip status machine, and
the failure path.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import ai_profile_service as svc
from app.services import ai_service

NOW = datetime(2026, 7, 26, 4, 20, tzinfo=timezone.utc)


class _Closable:
    """A client stub that can be closed, which everything now does."""

    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1

VALID = (
    "High relevance: distributed systems, Postgres internals, EU tech policy\n"
    "Moderate relevance: hardware reviews, science reporting\n"
    "Avoid: celebrity news, sports"
)


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        user_id=1,
        ai_preference_text="High relevance: old profile\nModerate relevance: things\nAvoid:",
        ai_preference_prev_text=None,
        ai_preference_updated_at=None,
        ai_preference_source=None,
        ai_preference_auto_days=14,
        ai_preference_last_attempt_at=None,
        ai_preference_last_error=None,
        ai_preference_last_error_at=None,
        ai_preference_fail_count=0,
        ai_quality_provider="anthropic",
        ai_quality_model="claude-sonnet-5",
        last_ai_error=None,
        last_ai_error_at=None,
        last_ai_error_article_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestNormalizePreferenceText:
    def test_accepts_expected_three_lines(self):
        cleaned, reason = svc.normalize_preference_text(VALID)
        assert reason is None
        assert cleaned == VALID

    def test_accepts_markdown_labels(self):
        raw = (
            "**High relevance:** distributed systems, Postgres internals\n"
            "**Moderate relevance:** hardware reviews and science reporting\n"
            "**Avoid:** celebrity news"
        )
        cleaned, reason = svc.normalize_preference_text(raw)
        assert reason is None
        assert "distributed systems" in cleaned

    def test_accepts_non_english_labels(self):
        raw = (
            "Vysoká relevance: distribuované systémy, databáze, evropská technologická politika\n"
            "Střední relevance: recenze hardwaru\n"
            "Vyhnout se: celebrity"
        )
        cleaned, reason = svc.normalize_preference_text(raw)
        assert reason is None
        assert "distribuované systémy" in cleaned

    def test_drops_lines_outside_the_format(self):
        raw = "Here is the profile\n" + VALID + "\nLet me know if you want changes"
        cleaned, reason = svc.normalize_preference_text(raw)
        assert reason is None
        assert cleaned == VALID

    def test_rejects_empty(self):
        assert svc.normalize_preference_text("   ")[0] is None
        assert svc.normalize_preference_text(None)[0] is None

    def test_rejects_refusal(self):
        cleaned, reason = svc.normalize_preference_text(
            "I'm sorry, but I cannot help with building a profile of a reader."
        )
        assert cleaned is None
        assert "declined" in reason

    def test_rejects_prose_without_labels(self):
        cleaned, _ = svc.normalize_preference_text(
            "The reader seems to enjoy technology articles and long-form journalism."
        )
        assert cleaned is None

    def test_rejects_labels_without_any_content(self):
        cleaned, reason = svc.normalize_preference_text(
            "High relevance:\nModerate relevance:\nAvoid:"
        )
        assert cleaned is None
        assert "empty" in reason

    def test_rejects_too_many_lines(self):
        cleaned, _ = svc.normalize_preference_text("\n".join(f"Topic {i}: x" for i in range(10)))
        assert cleaned is None

    def test_rejects_too_short(self):
        cleaned, reason = svc.normalize_preference_text("High: ai\nAvoid: x")
        assert cleaned is None
        assert "short" in reason

    def test_rejects_too_long(self):
        cleaned, reason = svc.normalize_preference_text(
            "High relevance: " + "topic, " * 900 + "\nModerate relevance: things"
        )
        assert cleaned is None
        assert "long" in reason


@pytest.mark.asyncio
class TestPreferenceAutoStatus:
    async def _status(self, settings, monkeypatch, strong=50, fresh=50, has_key=True):
        async def fake_counts(user_id, since, db):
            return strong, fresh
        monkeypatch.setattr(svc, "signal_counts", fake_counts)
        # The only scalar() the function issues is the stored-key lookup.
        db = AsyncMock()
        db.scalar = AsyncMock(return_value="anthropic" if has_key else None)
        return await svc.preference_auto_status(settings, db, NOW)

    async def test_off(self, monkeypatch):
        status, _ = await self._status(make_settings(ai_preference_auto_days=0), monkeypatch)
        assert status == "off"

    async def test_never_generated_is_due(self, monkeypatch):
        status, _ = await self._status(make_settings(), monkeypatch)
        assert status == "due"

    async def test_recent_profile_is_up_to_date(self, monkeypatch):
        settings = make_settings(ai_preference_updated_at=NOW - timedelta(days=13))
        status, detail = await self._status(settings, monkeypatch)
        assert status == "up_to_date"
        assert detail["next_at"] == NOW - timedelta(days=13) + timedelta(days=14)

    async def test_due_exactly_on_the_interval_boundary(self, monkeypatch):
        settings = make_settings(ai_preference_updated_at=NOW - timedelta(days=14))
        status, _ = await self._status(settings, monkeypatch)
        assert status == "due"

    async def test_four_week_interval_boundary(self, monkeypatch):
        settings = make_settings(
            ai_preference_auto_days=28, ai_preference_updated_at=NOW - timedelta(days=27)
        )
        status, _ = await self._status(settings, monkeypatch)
        assert status == "up_to_date"

    async def test_manual_save_postpones_the_next_run(self, monkeypatch):
        settings = make_settings(
            ai_preference_updated_at=NOW - timedelta(hours=1), ai_preference_source="manual"
        )
        status, _ = await self._status(settings, monkeypatch)
        assert status == "up_to_date"

    async def test_failed_attempt_starts_a_full_cooldown(self, monkeypatch):
        settings = make_settings(
            ai_preference_updated_at=NOW - timedelta(days=60),
            ai_preference_last_attempt_at=NOW - timedelta(days=1),
        )
        status, _ = await self._status(settings, monkeypatch)
        assert status == "cooldown"

    async def test_missing_quality_model(self, monkeypatch):
        settings = make_settings(ai_quality_model=None)
        status, _ = await self._status(settings, monkeypatch)
        assert status == "no_quality_model"

    async def test_missing_api_key_is_not_reported_as_due(self, monkeypatch):
        """A configured model with no key still means the job will skip.

        get_ai_client needs the key, so run_auto_generation returns
        skipped:no_quality_model. Saying "due" here would promise a nightly run that
        never happens and leaves no error anywhere to explain itself.
        """
        status, detail = await self._status(make_settings(), monkeypatch, has_key=False)
        assert status == "no_api_key"
        assert detail["provider"] == "anthropic"

    async def test_cold_start(self, monkeypatch):
        status, detail = await self._status(make_settings(), monkeypatch, strong=5)
        assert status == "cold_start"
        assert detail["strong"] == 5

    async def test_not_enough_new_signals(self, monkeypatch):
        status, detail = await self._status(make_settings(), monkeypatch, fresh=8)
        assert status == "not_enough_new"
        assert detail["missing"] == svc.MIN_NEW_SIGNALS - 8


@pytest.mark.asyncio
class TestRunAutoGeneration:
    def _db(self, settings):
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=settings)
        db.add = MagicMock()
        db.commit = AsyncMock()
        return db

    def _patch(self, monkeypatch, *, status="due", generate=None, client=_Closable()):
        async def fake_status(settings, db, now=None):
            return status, {}
        monkeypatch.setattr(svc, "preference_auto_status", fake_status)

        # The service takes its client through ai_service.ai_client, which looks
        # get_ai_client up in its own module when it runs, so that is where this
        # has to land.
        async def fake_client(user_id, slot, db):
            return (client, "anthropic", "claude-sonnet-5") if client else (None, None, None)
        monkeypatch.setattr(ai_service, "get_ai_client", fake_client)

        async def fake_generate(user_id, db, client, provider, model):
            if isinstance(generate, Exception):
                raise generate
            return generate, 9000, 400
        monkeypatch.setattr(svc, "generate_preference_text", fake_generate)

    async def test_skips_without_calling_the_model(self, monkeypatch):
        settings = make_settings()
        called = False

        async def fake_client(*args):
            nonlocal called
            called = True
            return None, None, None

        self._patch(monkeypatch, status="not_enough_new")
        monkeypatch.setattr(ai_service, "get_ai_client", fake_client)
        outcome = await svc.run_auto_generation(1, self._db(settings))
        assert outcome == "skipped:not_enough_new"
        assert called is False
        assert settings.ai_preference_last_attempt_at is None
        assert settings.ai_preference_fail_count == 0

    async def test_cold_start_does_not_count_as_failure(self, monkeypatch):
        settings = make_settings()
        self._patch(monkeypatch, status="cold_start")
        assert await svc.run_auto_generation(1, self._db(settings)) == "skipped:cold_start"
        assert settings.ai_preference_fail_count == 0

    async def test_missing_key_is_a_skip_not_a_failure(self, monkeypatch):
        settings = make_settings()
        self._patch(monkeypatch, client=None)
        assert await svc.run_auto_generation(1, self._db(settings)) == "skipped:no_quality_model"
        assert settings.ai_preference_fail_count == 0

    async def test_generates_and_keeps_the_previous_version(self, monkeypatch):
        settings = make_settings()
        previous = settings.ai_preference_text
        db = self._db(settings)
        self._patch(monkeypatch, generate=VALID)

        assert await svc.run_auto_generation(1, db) == "generated"
        assert settings.ai_preference_text == VALID
        assert settings.ai_preference_prev_text == previous
        assert settings.ai_preference_source == "auto"
        assert settings.ai_preference_updated_at is not None
        assert settings.ai_preference_last_attempt_at is not None
        assert settings.ai_preference_fail_count == 0
        assert db.add.called  # usage logged

    async def test_success_clears_a_previous_error(self, monkeypatch):
        settings = make_settings(ai_preference_last_error="boom", ai_preference_fail_count=2)
        self._patch(monkeypatch, generate=VALID)
        await svc.run_auto_generation(1, self._db(settings))
        assert settings.ai_preference_last_error is None
        assert settings.ai_preference_fail_count == 0

    async def test_api_error_keeps_the_profile(self, monkeypatch):
        settings = make_settings()
        original = settings.ai_preference_text
        self._patch(monkeypatch, generate=RuntimeError("401 invalid api key"))

        assert await svc.run_auto_generation(1, self._db(settings)) == "failed:error"
        assert settings.ai_preference_text == original
        assert settings.ai_preference_prev_text is None
        assert settings.ai_preference_fail_count == 1
        assert "invalid api key" in settings.ai_preference_last_error
        assert settings.ai_preference_auto_days == 14
        # A paid attempt starts the cooldown, so tomorrow's run skips instead of retrying.
        assert settings.ai_preference_last_attempt_at is not None

    async def test_rejected_output_keeps_the_profile_but_logs_usage(self, monkeypatch):
        settings = make_settings()
        original = settings.ai_preference_text
        db = self._db(settings)
        self._patch(monkeypatch, generate="I'm sorry, I cannot do that.")

        assert await svc.run_auto_generation(1, db) == "failed:invalid_output"
        assert settings.ai_preference_text == original
        assert settings.ai_preference_fail_count == 1
        assert settings.ai_preference_last_attempt_at is not None
        assert db.add.called

    async def test_third_failure_turns_the_schedule_off(self, monkeypatch):
        settings = make_settings(ai_preference_fail_count=2)
        self._patch(monkeypatch, generate=RuntimeError("still broken"))

        await svc.run_auto_generation(1, self._db(settings))
        assert settings.ai_preference_fail_count == 3
        assert settings.ai_preference_auto_days == 0
        assert "turned off" in settings.ai_preference_last_error
