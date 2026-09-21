"""Regression tests for admin-confirmed moderation misses (#65)."""
import json

import anime_news_bot as bot


def test_missed_violation_is_deduplicated_and_persisted(tmp_path):
    path = tmp_path / 'moderation.json'
    store = bot.ChatModerationStore(path)

    assert store.record_missed_violation(
        'miss-1', -100, 7, 'spam', 'пропущенная реклама', 'admin confirmed')
    assert store.record_missed_violation(
        'miss-1', -100, 7, 'spam', 'пропущенная реклама', 'duplicate')

    stats = store.stats()
    assert stats['missed_total'] == 1
    assert stats['by_category']['spam']['missed_total'] == 1

    restored = bot.ChatModerationStore(path)
    restored_stats = restored.stats()
    assert restored_stats['missed_total'] == 1
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert len(raw['misses']) == 1


def test_unknown_missed_category_is_rejected(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    assert not store.record_missed_violation(
        'miss-x', -100, 7, 'made_up', 'text')
    assert int(store.stats().get('missed_total', 0)) == 0


def test_reset_stats_also_clears_missed_dataset(tmp_path):
    path = tmp_path / 'moderation.json'
    store = bot.ChatModerationStore(path)
    assert store.record_missed_violation('miss-1', -100, 7, 'toxic', 'text')
    assert store.reset_stats()
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert raw['misses'] == {}
    assert int(store.stats().get('missed_total', 0)) == 0


def test_modquality_mentions_admin_confirmed_misses(tmp_path, monkeypatch):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    assert store.record_missed_violation('miss-1', -100, 7, 'spam', 'text')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    rendered = bot._moderation_quality_text()
    assert 'Пропуски' in rendered
    assert '<b>1</b>' in rendered
