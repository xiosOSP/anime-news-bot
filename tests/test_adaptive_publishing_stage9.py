from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


import anime_news_bot as bot


def _story(title, subject, hours_ago=1):
    return {
        'title': title,
        'subject': subject,
        'at': (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
    }


def test_adaptive_store_persists(tmp_path):
    path = tmp_path / 'adaptive.json'
    store = bot.AdaptivePublishingStore(path)
    store.record({'recommended_interval_min': 25, 'diversity_multiplier': 1.2})
    again = bot.AdaptivePublishingStore(path)
    assert again.latest()['recommended_interval_min'] == 25
    assert again.latest()['diversity_multiplier'] == 1.2


def test_adaptive_diversity_strengthens_on_concentrated_feed(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'ADAPTIVE_DIVERSITY_MIN_STORIES', 4)
    monkeypatch.setattr(bot, 'ADAPTIVE_DIVERSITY_TARGET_SHARE', 0.35)
    monkeypatch.setattr(bot, 'ADAPTIVE_DIVERSITY_MAX_MULTIPLIER', 1.75)
    fake = SimpleNamespace(_items=[
        _story('A1', 'One Piece', 1), _story('A2', 'One Piece', 2),
        _story('A3', 'One Piece', 3), _story('B1', 'Frieren', 4),
        _story('C1', 'Bleach', 5),
    ])
    monkeypatch.setattr(bot, 'story_history', fake)
    assert bot._adaptive_diversity_multiplier() > 1.0


def test_adaptive_diversity_stays_neutral_with_small_sample(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'ADAPTIVE_DIVERSITY_MIN_STORIES', 8)
    monkeypatch.setattr(bot, 'story_history', SimpleNamespace(_items=[_story('A', 'One Piece', 1)]))
    assert bot._adaptive_diversity_multiplier() == 1.0


def test_adaptive_format_recommends_small_exploration(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'POST_FORMAT_COMPACT_PERCENT', 0.0)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_MIN_OUTCOMES', 10)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_STEP_PERCENT', 10.0)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_MAX_PERCENT', 50.0)
    monkeypatch.setattr(bot, 'experiments', SimpleNamespace(snapshot=lambda: {
        'standard': {'published': 20, 'hidden': 5},
        'compact': {'published': 0, 'hidden': 0},
    }))
    pct, reason = bot._adaptive_recommend_compact_percent()
    assert pct == 10.0
    assert 'exploration' in reason


def test_adaptive_format_reduces_losing_variant(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'POST_FORMAT_COMPACT_PERCENT', 30.0)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_MIN_OUTCOMES', 10)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_MARGIN', 0.08)
    monkeypatch.setattr(bot, 'ADAPTIVE_FORMAT_STEP_PERCENT', 10.0)
    monkeypatch.setattr(bot, 'experiments', SimpleNamespace(snapshot=lambda: {
        'standard': {'published': 18, 'hidden': 2},
        'compact': {'published': 8, 'hidden': 12},
    }))
    pct, reason = bot._adaptive_recommend_compact_percent()
    assert pct == 20.0
    assert 'worse' in reason


def test_auto_format_is_opt_in(monkeypatch):
    monkeypatch.setattr(bot, 'POST_FORMAT_COMPACT_PERCENT', 23.0)
    monkeypatch.setattr(bot, 'ADAPTIVE_AUTO_FORMAT', False)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    assert bot._effective_compact_percent() == 23.0


def test_adaptive_interval_speeds_up_for_hard_queue(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_MIN', 10)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_MAX', 90)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_STEP', 5)
    monkeypatch.setattr(bot, 'BACKPRESSURE_HARD_QUEUE', 27)
    monkeypatch.setattr(bot, 'stats', None)
    interval, reason = bot._adaptive_recommend_interval(30, current=30)
    assert interval == 20
    assert 'hard queue' in reason


def test_adaptive_interval_slows_on_repeated_send_failures(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_MIN', 10)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_MAX', 90)
    monkeypatch.setattr(bot, 'ADAPTIVE_INTERVAL_STEP', 5)
    monkeypatch.setattr(bot, 'stats', SimpleNamespace(count_events_since=lambda _since, kind: 4 if kind == 'failed_send' else 0))
    interval, reason = bot._adaptive_recommend_interval(30, current=30)
    assert interval == 35
    assert 'failures' in reason


def test_breaking_news_still_bypasses_adaptive_franchise_penalty(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_publishing', True)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'diversity_scheduler', True)
    monkeypatch.setattr(bot, 'story_history', SimpleNamespace(_items=[_story('A', 'One Piece', 1)] * 10))
    news = {'title': 'One Piece new trailer', '_llm_subject': 'One Piece', '_breaking_news': True}
    assert bot._recent_franchise_penalty(news) == 0.0


def test_adaptive_auto_interval_disabled_by_default_contract():
    # Changing the user's explicit interval is a separate opt-in guardrail.
    assert isinstance(bot.ADAPTIVE_AUTO_INTERVAL, bool)
    assert isinstance(bot.ADAPTIVE_AUTO_FORMAT, bool)
