"""Unit tests for scoring_eligible — the pure per-user/per-feed eligibility check
shared by enqueue_scoring_job and the retroactive-apply planner."""
from types import SimpleNamespace

from app.services.ai_scoring_service import scoring_eligible


def make_settings(**kwargs):
    defaults = {
        "ai_scoring_enabled_default": True,
        "ai_preference_text": "Technology and science",
        "ai_fast_provider": "anthropic",
        "ai_fast_model": "claude-haiku-4-5",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_user_feed(**kwargs):
    defaults = {"ai_scoring_enabled": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_eligible_with_defaults():
    assert scoring_eligible(make_settings(), None) is True


def test_eligible_with_unset_per_feed_override():
    assert scoring_eligible(make_settings(), make_user_feed(ai_scoring_enabled=None)) is True


def test_no_settings():
    assert scoring_eligible(None, None) is False


def test_scoring_disabled_by_default():
    assert scoring_eligible(make_settings(ai_scoring_enabled_default=False), None) is False


def test_per_feed_override_off():
    assert scoring_eligible(make_settings(), make_user_feed(ai_scoring_enabled=False)) is False


def test_per_feed_override_on_does_not_resurrect_disabled_default():
    # Documents existing behavior: the default gate is checked first, so a per-feed
    # "on" override cannot enable scoring when the user default is off.
    s = make_settings(ai_scoring_enabled_default=False)
    assert scoring_eligible(s, make_user_feed(ai_scoring_enabled=True)) is False


def test_no_preference_text():
    assert scoring_eligible(make_settings(ai_preference_text=None), None) is False
    assert scoring_eligible(make_settings(ai_preference_text="   "), None) is False


def test_no_fast_model():
    assert scoring_eligible(make_settings(ai_fast_model=None), None) is False
    assert scoring_eligible(make_settings(ai_fast_provider=None), None) is False
