import asyncio

import anime_news_bot as bot


def _news(title, source='A', link='https://example.com/a', **extra):
    item = {
        'title': title,
        'source': source,
        'link': link,
        'summary': extra.pop('summary', 'Подробности события и официальный комментарий.'),
        'images': extra.pop('images', ['https://img.example/a.jpg']),
    }
    item.update(extra)
    return item


def test_story_id_stable_when_confirmation_sources_change():
    item = _news('One Piece anime reveals new trailer')
    assert bot._story_id(item) == bot._story_id(item, extra_sources=['A', 'B', 'C'])


def test_story_cluster_merges_close_cross_source_titles(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_clustering', True)
    a = _news('One Piece anime reveals new official trailer', 'Source A', 'https://a.example/1')
    b = _news('One Piece reveals new official anime trailer', 'Source B', 'https://b.example/2')
    out = bot._cluster_news([a, b])
    assert len(out) == 1
    assert out[0]['_story_cluster_size'] == 2
    assert set(out[0]['_story_sources']) == {'Source A', 'Source B'}
    assert len(out[0]['_story_links']) == 2


def test_story_cluster_does_not_merge_different_season_numbers(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_clustering', True)
    a = _news('Blue Lock Season 2 gets new trailer', 'A', 'https://a.example/2')
    b = _news('Blue Lock Season 3 gets new trailer', 'B', 'https://b.example/3')
    assert len(bot._cluster_news([a, b])) == 2


def test_story_cluster_keeps_media_from_confirming_source(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_clustering', True)
    # Official-like source wins primary but has no media; media must be inherited.
    a = _news('Frieren anime announces new season trailer', 'Kadokawa Official',
              'https://kadokawa.example/frieren', images=[], summary='Official announcement.')
    b = _news('Frieren anime announces new season trailer', 'Mirror News',
              'https://mirror.example/frieren', images=['https://mirror.example/key.jpg'],
              video='https://mirror.example/trailer.mp4')
    out = bot._cluster_news([a, b])
    assert len(out) == 1
    assert out[0]['images'] == ['https://mirror.example/key.jpg']
    assert out[0]['video'] == 'https://mirror.example/trailer.mp4'


def test_clustering_feature_flag_can_disable_collapsing(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_clustering', False)
    a = _news('One Piece anime reveals new official trailer', 'A', 'https://a.example/1')
    b = _news('One Piece reveals new official anime trailer', 'B', 'https://b.example/2')
    out = bot._cluster_news([a, b])
    assert len(out) == 2
    assert all(x['_story_cluster_size'] == 1 for x in out)


def test_confidence_increases_with_independent_confirmation(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'confidence_scoring', True)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'source_reputation', False)
    single = _news('Anime title announces adaptation')
    single['_story_sources'] = ['A']
    confirmed = dict(single)
    confirmed['_story_sources'] = ['A', 'B', 'C']
    assert bot._confidence_score(confirmed) > bot._confidence_score(single)


def test_source_reputation_rewards_good_history(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'source_reputation', True)
    stats = bot.BotStats(tmp_path / 'stats.json')
    feedback = bot.ModerationFeedback(tmp_path / 'feedback.json')
    health = bot.SourceHealth(tmp_path / 'health.json')
    monkeypatch.setattr(bot, 'stats', stats)
    monkeypatch.setattr(bot, 'moderation_feedback', feedback)
    monkeypatch.setattr(bot, 'source_health', health)

    for _ in range(10):
        asyncio.run(stats.record_collected('Good', 1))
    for _ in range(8):
        asyncio.run(stats.record_published('Good'))
        feedback.record('published', _news('Good story', source='Good'))
    for _ in range(10):
        asyncio.run(stats.record_collected('Bad', 1))
    for _ in range(5):
        asyncio.run(stats.record_source_error('Bad'))
        feedback.record('hidden', _news('Bad story', source='Bad'))
    health.record_fail('Bad', 'timeout')

    assert bot._source_reputation_score('Good') > bot._source_reputation_score('Bad')
    assert 0.0 < bot._source_reputation_score('Bad') < 1.0


def test_priority_uses_confidence_signal(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'confidence_scoring', True)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'source_reputation', False)
    low = _news('Anime project announces trailer')
    high = dict(low)
    low['_confidence_score'] = 0.2
    high['_confidence_score'] = 0.9
    assert bot._news_priority_score(high) > bot._news_priority_score(low)


def test_metrics_registry_renders_counters_gauges_and_observations():
    registry = bot.MetricsRegistry()
    registry.inc('anime_bot_test_total', 2, {'source': 'A"B'})
    registry.set('anime_bot_queue_size', 7)
    registry.observe('anime_bot_latency_seconds', 0.25, {'op': 'fetch'})
    text = registry.render()
    assert 'anime_bot_test_total{source="A\\"B"} 2' in text
    assert 'anime_bot_queue_size 7' in text
    assert 'anime_bot_latency_seconds_sum{op="fetch"} 0.25' in text
    assert 'anime_bot_latency_seconds_count{op="fetch"} 1' in text


def test_metrics_registry_sanitizes_metric_and_label_names():
    registry = bot.MetricsRegistry()
    registry.inc('bad metric-name', labels={'bad-label': 'x'})
    text = registry.render()
    assert 'bad_metric_name{bad_label="x"}' in text


def test_doctor_detects_corrupt_runtime_json(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    (tmp_path / 'broken.json').write_text('{not-json', encoding='utf-8')
    checks = bot._doctor_local_checks()
    row = next(c for c in checks if c['name'] == 'Runtime JSON')
    assert row['ok'] is False
    assert 'broken.json' in row['detail']


def test_doctor_feature_flags_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    checks = bot._doctor_local_checks()
    row = next(c for c in checks if c['name'] == 'Feature flags')
    assert row['ok'] is True
    assert 'story_clustering=' in row['detail']


def test_structured_event_truncates_large_fields():
    value = bot._safe_event_value('x' * 5000)
    assert len(value) == 1000
