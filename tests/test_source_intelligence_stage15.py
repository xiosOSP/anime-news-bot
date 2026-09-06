from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anime_news_bot as bot


def _news(title, source, hours, *, official=False):
    base = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc) + timedelta(hours=hours)
    return {
        'title': title,
        'link': f'https://{source.lower().replace(" ", "")}.example/story/{hours}',
        'source': source,
        'summary': 'A sufficiently descriptive anime news summary.',
        'images': ['https://img.example/a.jpg'],
        'published_parsed': base.timetuple(),
        'official': official,
    }


def _feature(monkeypatch):
    original = dict(bot.FEATURE_FLAGS)
    original['source_intelligence'] = True
    original['story_clustering'] = True
    original['source_reputation'] = True
    monkeypatch.setattr(bot, 'FEATURE_FLAGS', original)


def test_source_story_time_prefers_published_and_falls_back_to_collected():
    now = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)
    news = _news('Example Anime Gets Trailer', 'A', -2)
    assert bot._source_story_time(news, now).hour == 10
    news['published_parsed'] = None
    news['_collected_at'] = '2026-08-08T14:05:00+00:00'
    assert bot._source_story_time(news, now).hour == 14
    assert bot._source_story_time(news, now).minute == 5


def test_probation_does_not_adjust_new_source(tmp_path, monkeypatch):
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_STORIES', 12)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_DAYS', 3)
    store.observe_story('s1', [_news('Example Anime New Trailer', 'New Source', 0)])
    row = store.source_metrics('New Source')
    assert row['probation'] is True
    assert row['adjustment'] == 0.0


def test_historical_source_can_skip_fresh_deploy_probation(tmp_path, monkeypatch):
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'stats', SimpleNamespace(get_by_source=lambda: {
        'Old Source': {'collected': 80}
    }))
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    assert store.probation_status('Old Source')['probation'] is False


def test_official_source_is_probable_origin_even_when_republisher_timestamp_is_earlier(tmp_path, monkeypatch):
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    cluster = [
        _news('Example Anime Announces New Season', 'Fast Blog', 0),
        _news('Example Anime Announces New Season', 'Official Studio', 1, official=True),
    ]
    result = store.observe_story('story-1', cluster)
    assert result['earliest_source'] == 'Fast Blog'
    assert result['probable_origin'] == 'Official Studio'


def test_late_republisher_receives_bounded_negative_adjustment(tmp_path, monkeypatch):
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_STORIES', 1)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_DAYS', 0)
    monkeypatch.setattr(bot, 'SOURCE_INTEL_MIN_COMPARISONS', 2)
    monkeypatch.setattr(bot, 'SOURCE_REPOST_LAG_HOURS', 3.0)
    for i in range(6):
        store.observe_story(f's{i}', [
            _news(f'Example Anime {i} Official Trailer', 'Original', 0, official=True),
            _news(f'Example Anime {i} Official Trailer', 'Late Copy', 12),
        ])
    late = store.source_metrics('Late Copy')
    original = store.source_metrics('Original')
    assert late['late_rate'] > 0.9
    assert late['adjustment'] < 0
    assert original['adjustment'] > late['adjustment']
    assert abs(late['adjustment']) <= bot.SOURCE_INTEL_WEIGHT_MAX


def test_store_persists_without_double_counting_same_story(tmp_path, monkeypatch):
    path = tmp_path / 'intel.json'
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    store = bot.SourceIntelligenceStore(path)
    cluster = [_news('Example Anime Gets Trailer', 'A', 0), _news('Example Anime Gets Trailer', 'B', 2)]
    store.observe_story('same', cluster)
    store.observe_story('same', cluster)
    store.flush()
    loaded = bot.SourceIntelligenceStore(path)
    assert loaded.source_metrics('A')['stories_seen'] == 1
    assert loaded.source_metrics('A')['comparisons'] == 1
    assert loaded.source_metrics('B')['comparisons'] == 1


def test_cluster_news_records_origin_and_keeps_cross_source_confirmation(tmp_path, monkeypatch):
    _feature(monkeypatch)
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'source_intelligence', store)
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    # Реестр историй тоже свой: он копит записи в памяти, и общий на весь
    # прогон объект приносил сюда чужие истории — размер кластера получался
    # вчетверо больше. В одиночку тест при этом проходил.
    monkeypatch.setattr(bot, 'story_registry', bot.StoryRegistry(tmp_path / 'stories.json'))
    rows = [
        _news('Frieren Season 2 Gets Official Trailer', 'Blog', 0),
        _news('Frieren Season 2 Gets Official Trailer', 'Official Studio', 1, official=True),
    ]
    clustered = bot._cluster_news(rows)
    assert len(clustered) == 1
    assert clustered[0]['_story_cluster_size'] == 2
    assert clustered[0]['_story_origin_source'] == 'Official Studio'
    assert set(clustered[0]['_story_sources']) == {'Blog', 'Official Studio'}


def test_reputation_applies_only_small_intelligence_delta(tmp_path, monkeypatch):
    _feature(monkeypatch)
    store = bot.SourceIntelligenceStore(tmp_path / 'intel.json')
    monkeypatch.setattr(bot, 'source_intelligence', store)
    monkeypatch.setattr(bot, 'stats', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, 'source_health', None)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_STORIES', 1)
    monkeypatch.setattr(bot, 'SOURCE_PROBATION_MIN_DAYS', 0)
    monkeypatch.setattr(bot, 'SOURCE_INTEL_MIN_COMPARISONS', 2)
    for i in range(5):
        store.observe_story(f'x{i}', [
            _news(f'Anime X {i} Trailer', 'Official A', 0, official=True),
            _news(f'Anime X {i} Trailer', 'Copy B', 10),
        ])
    score = bot._source_reputation_score('Official A')
    assert 0.05 <= score <= 0.99
    assert abs(store.source_metrics('Official A')['adjustment']) <= bot.SOURCE_INTEL_WEIGHT_MAX


def test_malformed_source_intelligence_file_fails_soft(tmp_path):
    path = tmp_path / 'intel.json'
    path.write_text('{"sources":{"A":{"lag_sum_hours":"oops"}},"stories":{}}', encoding='utf-8')
    store = bot.SourceIntelligenceStore(path)
    assert store.source_metrics('A')['lag_sum_hours'] == 0.0


def test_new_runtime_file_is_ignored_by_git():
    """Рабочий файл не должен попадать в репозиторий.

    Проверяем через сам git, а не поиском имени в .gitignore: правила там
    свернули в «*.json» с исключениями, и точное имя больше не встречается —
    хотя файл по-прежнему игнорируется.
    """
    import subprocess
    root = Path(bot.__file__).resolve().parent
    result = subprocess.run(['git', 'check-ignore', '-q', 'source_intelligence.json'],
                            cwd=root, capture_output=True)
    assert result.returncode == 0, 'source_intelligence.json не игнорируется git'

import pytest


@pytest.mark.asyncio
async def test_collect_keeps_exact_title_from_independent_sources_for_clustering(tmp_path, monkeypatch):
    _feature(monkeypatch)
    flags = dict(bot.FEATURE_FLAGS)
    flags['active_verification'] = False
    flags['replay'] = False
    flags['editorial_rules'] = False
    flags['editorial_learning'] = False
    flags['story_updates'] = False
    monkeypatch.setattr(bot, 'FEATURE_FLAGS', flags)
    a = _news('Exact Same Anime Trailer Headline', 'A', 0)
    b = _news('Exact Same Anime Trailer Headline', 'B', 1)
    monkeypatch.setattr(bot, 'SOURCES', [('A', lambda: [dict(a)]), ('B', lambda: [dict(b)])])
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(
        is_source_enabled=lambda _name: True, require_image=False))
    monkeypatch.setattr(bot, 'source_health', None)
    monkeypatch.setattr(bot, 'error_fingerprints', None)
    monkeypatch.setattr(bot, 'replay_buffer', None)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, 'story_history', None)
    monkeypatch.setattr(bot, 'source_intelligence', bot.SourceIntelligenceStore(tmp_path / 'intel.json'))

    class Stats:
        async def record_collected(self, *a, **k): pass
        async def record_skipped(self, *a, **k): pass
        async def record_source_error(self, *a, **k): pass
        def get_by_source(self): return {}
    monkeypatch.setattr(bot, 'stats', Stats())

    rows, _stats, errors = await bot.collect_all_news()
    assert errors == []
    assert len(rows) == 1
    assert rows[0]['_story_cluster_size'] == 2
    assert set(rows[0]['_story_sources']) == {'A', 'B'}
