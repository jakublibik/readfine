"""Star/unstar side effects shared by web toggle and API PATCH (#15).
Both paths route through _apply_star_side_effects, so testing it proves parity."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.article import _apply_star_side_effects


def _state(**kw):
    defaults = dict(
        user_starred=False, ever_starred=False, starred_at=None,
        dwell_seconds=0, unstar_dwell_seconds=0,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestApplyStarSideEffects:
    def test_star_sets_user_intent_and_retention(self):
        state = _state()
        article = SimpleNamespace(readable_status="success")
        _apply_star_side_effects(state, article, starred=True, extract_readable=True)
        assert state.user_starred is True       # AI-preference signal
        assert state.ever_starred is True        # retention protection
        assert state.starred_at is not None

    def test_star_triggers_readable_when_skipped(self):
        state = _state()
        article = SimpleNamespace(readable_status="skipped")
        _apply_star_side_effects(state, article, starred=True, extract_readable=True)
        assert article.readable_status == "pending"

    def test_star_no_readable_trigger_when_extract_disabled(self):
        state = _state()
        article = SimpleNamespace(readable_status="skipped")
        _apply_star_side_effects(state, article, starred=True, extract_readable=False)
        assert article.readable_status == "skipped"

    def test_unstar_snapshots_dwell_and_keeps_old_star(self):
        state = _state(
            ever_starred=True,
            starred_at=datetime.now(timezone.utc) - timedelta(hours=1),
            dwell_seconds=42,
        )
        article = SimpleNamespace(readable_status="success")
        _apply_star_side_effects(state, article, starred=False, extract_readable=True)
        assert state.unstar_dwell_seconds == 42
        assert state.ever_starred is True  # starred long ago → deliberate, keep protection

    def test_unstar_within_60s_clears_ever_starred(self):
        state = _state(
            ever_starred=True,
            starred_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            dwell_seconds=5,
        )
        article = SimpleNamespace(readable_status="success")
        _apply_star_side_effects(state, article, starred=False, extract_readable=True)
        assert state.ever_starred is False  # accidental star → drop protection
