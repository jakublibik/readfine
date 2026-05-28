"""Tests for briefing_service and briefing endpoints."""
from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.briefing_service import compute_next_send_at


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_config(**kwargs):
    defaults = dict(
        id=1,
        user_id=1,
        name="Test Config",
        period="7days",
        scope_include=None,
        filter_status="all",
        filter_labeled=False,
        filter_score_min=None,
        article_limit=100,
        model_slot="quality",
        custom_prompt=None,
        include_snippet=False,
        briefing_enabled=True,
        briefing_interval="daily",
        briefing_day=None,
        briefing_time="08:00",
        briefing_recipients=None,
        briefing_last_sent_at=None,
        briefing_last_error=None,
        briefing_retry_count=0,
        briefing_next_send_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_user(**kwargs):
    defaults = dict(
        id=1,
        email="user@test.com",
        role="user",
        settings=SimpleNamespace(
            timezone="UTC",
            ai_scoring_enabled_default=False,
        ),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_app_settings(**kwargs):
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_from_email="noreply@example.com",
        smtp_user="user",
        smtp_port=587,
        smtp_use_tls=True,
        smtp_password_encrypted=None,
        ai_enabled=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── compute_next_send_at ──────────────────────────────────────────────────────

class TestComputeNextSendAt:

    def test_daily_time_in_future_returns_today(self):
        # 06:00 UTC now, scheduled at 08:00 UTC → same day
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 5, 6, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            result = compute_next_send_at("daily", None, "08:00", "UTC")
        assert result.date() == now.date()
        assert result.hour == 8
        assert result.minute == 0

    def test_daily_time_in_past_returns_tomorrow(self):
        # 10:00 UTC now, scheduled at 08:00 UTC → next day
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 5, 10, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            result = compute_next_send_at("daily", None, "08:00", "UTC")
        assert result.date() == (now + timedelta(days=1)).date()

    def test_daily_exact_current_time_returns_tomorrow(self):
        # exactly at scheduled time → next day (candidate <= now)
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 5, 8, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now
            result = compute_next_send_at("daily", None, "08:00", "UTC")
        assert result > now

    def test_weekly_target_day_future_this_week(self):
        # Monday (weekday 0), target Wednesday (2) → this Wednesday
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)  # Monday
            mock_dt.now.return_value = now
            result = compute_next_send_at("weekly", 2, "08:00", "UTC")
        assert result.weekday() == 2
        assert result > now

    def test_weekly_target_day_today_time_in_future(self):
        # Wednesday (2), target Wednesday (2), 06:00 → today at 08:00
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 5, 6, 0, tzinfo=timezone.utc)  # Wednesday
            mock_dt.now.return_value = now
            result = compute_next_send_at("weekly", 2, "08:00", "UTC")
        assert result.weekday() == 2
        assert result.date() == now.date()

    def test_weekly_target_day_today_time_in_past_returns_next_week(self):
        # Wednesday (2), target Wednesday (2), 10:00 → next Wednesday
        with patch("app.services.briefing_service.datetime") as mock_dt:
            now = datetime(2024, 6, 5, 10, 0, tzinfo=timezone.utc)  # Wednesday
            mock_dt.now.return_value = now
            result = compute_next_send_at("weekly", 2, "08:00", "UTC")
        assert result.weekday() == 2
        assert (result - now).days >= 6

    def test_invalid_timezone_falls_back_to_utc(self):
        result = compute_next_send_at("daily", None, "08:00", "Invalid/Timezone")
        assert result.tzinfo == timezone.utc

    def test_invalid_time_str_falls_back_to_0800(self):
        result = compute_next_send_at("daily", None, "invalid", "UTC")
        assert result.minute == 0
        # hour is either 8 (future) or 8 next day — result is valid datetime
        assert isinstance(result, datetime)

    def test_weekly_none_day_defaults_to_monday(self):
        result = compute_next_send_at("weekly", None, "08:00", "UTC")
        assert result.weekday() == 0

    def test_result_always_in_utc(self):
        result = compute_next_send_at("daily", None, "08:00", "Europe/Prague")
        assert result.tzinfo == timezone.utc

    def test_result_always_in_future(self):
        # Run without patching — result must be strictly after now
        result = compute_next_send_at("daily", None, "08:00", "UTC")
        assert result > datetime.now(timezone.utc)


# ── send_briefing ─────────────────────────────────────────────────────────────

class TestSendBriefing:

    @pytest.fixture
    def mock_db(self):
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_smtp_not_configured_disables_briefing(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings(smtp_host=None)

        await send_briefing(config, user, mock_db, app_settings)

        assert config.briefing_enabled is False
        assert "SMTP" in config.briefing_last_error
        assert config.briefing_next_send_at is None
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_smtp_not_configured_test_mode_does_not_disable(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings(smtp_host=None)

        await send_briefing(config, user, mock_db, app_settings, test_mode=True)

        assert config.briefing_enabled is True
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scope_error_disables_briefing(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   side_effect=ValueError("Feed 99 not found")):
            await send_briefing(config, user, mock_db, app_settings)

        assert config.briefing_enabled is False
        assert "Scope error" in config.briefing_last_error
        assert config.briefing_next_send_at is None
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_articles_skips_silently_and_schedules_next(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[]):
            await send_briefing(config, user, mock_db, app_settings)

        mock_db.add.assert_called_once()  # CatchupLog with article_count=0
        log = mock_db.add.call_args[0][0]
        assert log.article_count == 0
        assert config.briefing_next_send_at is not None
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_ai_client_raises(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=None):
                with pytest.raises(RuntimeError, match="No AI client"):
                    await send_briefing(config, user, mock_db, app_settings)

    @pytest.mark.asyncio
    async def test_smtp_exception_bubbles_up(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email",
                                       side_effect=smtplib.SMTPException("connection refused")):
                                with pytest.raises(smtplib.SMTPException):
                                    await send_briefing(config, user, mock_db, app_settings)

    @pytest.mark.asyncio
    async def test_successful_send_updates_config(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("digest text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email"):
                                with patch("app.services.briefing_service._build_email_html",
                                           return_value="<html>...</html>"):
                                    await send_briefing(config, user, mock_db, app_settings)

        assert config.briefing_last_sent_at is not None
        assert config.briefing_last_error is None
        assert config.briefing_retry_count == 0
        assert config.briefing_next_send_at is not None
        mock_db.add.assert_called_once()  # CatchupLog
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_test_mode_does_not_update_next_send_at(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config(briefing_next_send_at=None)
        user = make_user()
        app_settings = make_app_settings()

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email"):
                                with patch("app.services.briefing_service._build_email_html",
                                           return_value="<html>...</html>"):
                                    await send_briefing(config, user, mock_db, app_settings,
                                                        test_mode=True)

        # test_mode: next_send_at and last_sent_at must NOT be updated
        assert config.briefing_next_send_at is None
        assert config.briefing_last_sent_at is None

    @pytest.mark.asyncio
    async def test_test_mode_subject_has_test_prefix(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config()
        user = make_user()
        app_settings = make_app_settings()
        sent_subject = []

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        def capture_send(s, to_list, subject, html_body, plain_body):
            sent_subject.append(subject)

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email",
                                       side_effect=capture_send):
                                with patch("app.services.briefing_service._build_email_html",
                                           return_value="<html>...</html>"):
                                    await send_briefing(config, user, mock_db, app_settings,
                                                        test_mode=True)

        assert sent_subject[0].startswith("[TEST]")

    @pytest.mark.asyncio
    async def test_extra_recipients_included_in_to_list(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config(
            briefing_recipients=json.dumps(["extra1@test.com", "extra2@test.com"])
        )
        user = make_user()
        app_settings = make_app_settings()
        captured_to = []

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        def capture_send(s, to_list, subject, html_body, plain_body):
            captured_to.extend(to_list)

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email",
                                       side_effect=capture_send):
                                with patch("app.services.briefing_service._build_email_html",
                                           return_value="<html>...</html>"):
                                    await send_briefing(config, user, mock_db, app_settings)

        assert "user@test.com" in captured_to
        assert "extra1@test.com" in captured_to
        assert "extra2@test.com" in captured_to
        assert len(captured_to) == 3

    @pytest.mark.asyncio
    async def test_malformed_recipients_json_falls_back_to_empty(self, mock_db):
        from app.services.briefing_service import send_briefing
        config = make_config(briefing_recipients="not-json")
        user = make_user()
        app_settings = make_app_settings()
        captured_to = []

        mock_article = SimpleNamespace(id=1, title="A", feed_title="F",
                                       published_at=None, fetched_at=datetime.now(timezone.utc),
                                       folder_id=None, ai_score=None, ai_summary=None,
                                       readable_content=None, content="text")

        def capture_send(s, to_list, subject, html_body, plain_body):
            captured_to.extend(to_list)

        with patch("app.services.briefing_service.fetch_catchup_articles",
                   new_callable=AsyncMock, return_value=[mock_article]):
            with patch("app.services.ai_service.get_ai_client",
                       new_callable=AsyncMock, return_value=(MagicMock(), "anthropic", "claude-3")):
                with patch("app.services.briefing_service.apply_catchup_limit",
                           return_value=[mock_article]):
                    with patch("app.services.briefing_service.build_articles_meta",
                               return_value=[]):
                        with patch("app.services.ai_service.catch_me_up",
                                   new_callable=AsyncMock, return_value=("text", 100, 50)):
                            with patch("app.services.briefing_service.send_html_email",
                                       side_effect=capture_send):
                                with patch("app.services.briefing_service._build_email_html",
                                           return_value="<html>...</html>"):
                                    await send_briefing(config, user, mock_db, app_settings)

        assert captured_to == ["user@test.com"]


# ── Endpoint validation ───────────────────────────────────────────────────────

class TestBriefingEndpointValidation:
    """Tests for PUT /htmx/catchup-configs/{id}/briefing validation logic."""

    def _make_endpoint_deps(self):
        """Returns (config, user, db) mocks for endpoint testing."""
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        user = make_user()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)
        return config, user, db

    @pytest.mark.asyncio
    async def test_invalid_interval_returns_validation_error(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)

        response = await htmx_briefing_modal_save(
            config_id=1, request=MagicMock(),
            briefing_enabled=True, briefing_interval="monthly",
            briefing_day=None, briefing_time="08:00", briefing_recipients=None,
            user=make_user(), db=db,
        )
        assert "HX-Retarget" in response.headers
        assert "Invalid interval" in response.body.decode()

    @pytest.mark.asyncio
    async def test_weekly_without_day_returns_validation_error(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)

        response = await htmx_briefing_modal_save(
            config_id=1, request=MagicMock(),
            briefing_enabled=True, briefing_interval="weekly",
            briefing_day=None, briefing_time="08:00", briefing_recipients=None,
            user=make_user(), db=db,
        )
        assert "HX-Retarget" in response.headers
        assert "day" in response.body.decode().lower()

    @pytest.mark.asyncio
    async def test_invalid_time_format_returns_validation_error(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)

        response = await htmx_briefing_modal_save(
            config_id=1, request=MagicMock(),
            briefing_enabled=True, briefing_interval="daily",
            briefing_day=None, briefing_time="25:99", briefing_recipients=None,
            user=make_user(), db=db,
        )
        assert "HX-Retarget" in response.headers
        assert "time" in response.body.decode().lower()

    @pytest.mark.asyncio
    async def test_too_many_recipients_returns_validation_error(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)

        six_emails = "a@t.com,b@t.com,c@t.com,d@t.com,e@t.com,f@t.com"
        response = await htmx_briefing_modal_save(
            config_id=1, request=MagicMock(),
            briefing_enabled=True, briefing_interval="daily",
            briefing_day=None, briefing_time="08:00", briefing_recipients=six_emails,
            user=make_user(), db=db,
        )
        assert "HX-Retarget" in response.headers
        assert "5" in response.body.decode()

    @pytest.mark.asyncio
    async def test_invalid_email_format_returns_validation_error(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        config = make_config()
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(config)

        response = await htmx_briefing_modal_save(
            config_id=1, request=MagicMock(),
            briefing_enabled=True, briefing_interval="daily",
            briefing_day=None, briefing_time="08:00",
            briefing_recipients="not-an-email",
            user=make_user(), db=db,
        )
        assert "HX-Retarget" in response.headers
        assert "not-an-email" in response.body.decode()

    @pytest.mark.asyncio
    async def test_valid_save_sets_hx_trigger(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result

        config = make_config()
        user_settings = SimpleNamespace(timezone="UTC")
        db = make_mock_db()

        def execute_side_effect(stmt):
            # First call: config lookup; second call: user settings
            return make_scalar_result(config) if db.execute.call_count <= 1 \
                else make_scalar_result(user_settings)
        db.execute.side_effect = execute_side_effect

        with patch("app.routers.web.app._catchup_configs_list_html",
                   new_callable=AsyncMock) as mock_list:
            from fastapi.responses import HTMLResponse
            mock_list.return_value = HTMLResponse("<ul></ul>")
            response = await htmx_briefing_modal_save(
                config_id=1, request=MagicMock(),
                briefing_enabled=True, briefing_interval="daily",
                briefing_day=None, briefing_time="08:00", briefing_recipients=None,
                user=make_user(), db=db,
            )

        assert response.headers.get("HX-Trigger") == "closeBriefingModal"

    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self):
        from app.routers.web.app import htmx_briefing_modal_save
        from tests.conftest import make_mock_db, make_scalar_result
        db = make_mock_db()
        db.execute.return_value = make_scalar_result(None)

        response = await htmx_briefing_modal_save(
            config_id=999, request=MagicMock(),
            briefing_enabled=False, briefing_interval="daily",
            briefing_day=None, briefing_time="08:00", briefing_recipients=None,
            user=make_user(), db=db,
        )
        assert response.status_code == 404
