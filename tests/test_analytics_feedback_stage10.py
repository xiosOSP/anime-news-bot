from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import anime_news_bot as bot


def _event(at, **kw):
    row = {'at': at.isoformat(), 'action': 'published', 'source': 'src', 'format': 'standard',
           'confidence': 0.8, 'prompt_version': 'v1'}
    row.update(kw)
    return row


def test_analytics_store_persists_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'analytics_feedback', True)
    path = tmp_path / 'analytics.json'
    store = bot.AnalyticsStore(path, max_events=100)
    for i in range(130):
        store.record('delivery', {'title': f'N{i}', 'source': 'RSS'}, result='sent')
    again = bot.AnalyticsStore(path, max_events=100)
    assert len(again.events()) == 100
    assert again.events()[0]['kind'] == 'delivery'


def test_analytics_store_feature_flag_disables_writes(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'analytics_feedback', False)
    store = bot.AnalyticsStore(tmp_path / 'analytics.json')
    store.record('delivery', {'source': 'RSS'}, result='sent')
    assert store.events() == []


def test_delivery_summary_counts_results(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'analytics_feedback', True)
    store = bot.AnalyticsStore(tmp_path / 'analytics.json')
    for result in ('sent', 'sent', 'failed', 'uncertain'):
        store.record('delivery', {'source': 'RSS'}, result=result)
    assert store.delivery_summary(30) == {
        'attempts': 4, 'sent': 2, 'failed': 1, 'uncertain': 1, 'other': 0,
    }


def test_delivery_summary_filters_old_events(tmp_path):
    store = bot.AnalyticsStore(tmp_path / 'analytics.json')
    store._data['events'] = [
        {'at': (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
         'kind': 'delivery', 'result': 'sent', 'source': 'old'},
        {'at': datetime.now(timezone.utc).isoformat(), 'kind': 'delivery',
         'result': 'sent', 'source': 'new'},
    ]
    assert store.delivery_summary(30)['attempts'] == 1


def test_source_delivery_groups_by_source(tmp_path):
    store = bot.AnalyticsStore(tmp_path / 'analytics.json')
    now = datetime.now(timezone.utc).isoformat()
    store._data['events'] = [
        {'at': now, 'kind': 'delivery', 'result': 'sent', 'source': 'A'},
        {'at': now, 'kind': 'delivery', 'result': 'failed', 'source': 'A'},
        {'at': now, 'kind': 'delivery', 'result': 'sent', 'source': 'B'},
    ]
    rows = {r['source']: r for r in store.source_delivery(30)}
    assert rows['A']['attempts'] == 2 and rows['A']['failed'] == 1
    assert rows['B']['sent'] == 1


def test_moderation_feedback_records_stage10_dimensions(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_learning', True)
    fb = bot.ModerationFeedback(tmp_path / 'feedback.json')
    news = {'title': 'One Piece trailer', 'source': 'ANN', '_format_variant': 'compact',
            '_priority_score': 77.5, '_confidence_score': 0.91,
            '_breaking_news': True, '_llm_subject': 'One Piece'}
    fb.record('published', news, SimpleNamespace(id=123))
    row = fb._events[-1]
    assert row['format'] == 'compact'
    assert row['breaking'] is True
    assert row['subject'] == 'onepiece'
    assert row['priority'] == 77.5


def test_beta_acceptance_is_smoothed():
    assert bot._beta_acceptance(0, 0) == 0.5
    assert 0.5 < bot._beta_acceptance(3, 0) < 1.0
    assert 0.0 < bot._beta_acceptance(0, 3) < 0.5


def test_feedback_report_recommends_strong_and_weak_sources(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    events = []
    for i in range(8):
        events.append(_event(now - timedelta(minutes=i), source='Good', action='published'))
        events.append(_event(now - timedelta(minutes=i), source='Bad', action='hidden'))
    monkeypatch.setattr(bot, 'moderation_feedback', SimpleNamespace(_events=events))
    monkeypatch.setattr(bot, 'analytics_store', bot.AnalyticsStore(tmp_path / 'a.json'))
    monkeypatch.setattr(bot, 'ANALYTICS_MIN_SAMPLES', 8)
    monkeypatch.setattr(bot, 'ANALYTICS_RECOMMEND_MARGIN', 0.10)
    report = bot._analytics_feedback_report(30)
    recs = {x['source']: x['action'] for x in report['source_recommendations']}
    assert recs['Good'] == 'boost'
    assert recs['Bad'] == 'review/downrank'


def test_feedback_report_groups_formats_prompts_and_confidence(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    events = [
        _event(now, format='compact', prompt_version='v2', confidence=0.9, action='published'),
        _event(now, format='compact', prompt_version='v2', confidence=0.9, action='hidden'),
        _event(now, format='standard', prompt_version='v1', confidence=0.4, action='published'),
    ]
    monkeypatch.setattr(bot, 'moderation_feedback', SimpleNamespace(_events=events))
    monkeypatch.setattr(bot, 'analytics_store', bot.AnalyticsStore(tmp_path / 'a.json'))
    report = bot._analytics_feedback_report(30)
    assert {r['name'] for r in report['formats']} == {'compact', 'standard'}
    assert {r['name'] for r in report['prompt_versions']} == {'v1', 'v2'}
    assert {r['name'] for r in report['confidence_buckets']} == {'<0.50', '≥0.85'}


def test_feedback_report_respects_requested_window(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    events = [
        _event(now - timedelta(days=2), source='Recent', action='published'),
        _event(now - timedelta(days=40), source='Old', action='hidden'),
    ]
    monkeypatch.setattr(bot, 'moderation_feedback', SimpleNamespace(_events=events))
    monkeypatch.setattr(bot, 'analytics_store', bot.AnalyticsStore(tmp_path / 'a.json'))
    report = bot._analytics_feedback_report(7)
    assert {r['name'] for r in report['sources']} == {'Recent'}


def test_publication_hours_use_admin_timezone(monkeypatch, tmp_path):
    store = bot.AnalyticsStore(tmp_path / 'a.json')
    store._data['events'] = [{
        'at': '2026-08-08T12:00:00+00:00', 'kind': 'delivery', 'result': 'sent', 'source': 'A'
    }]
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(timezone_name='UTC', tz_offset=0))
    rows = dict(store.publication_hours(30))
    assert rows[12] == 1


class _Ledger:
    def has_similar_title(self, *args, **kwargs):
        return False
    async def claim(self, *args, **kwargs):
        return True
    async def mark_sending(self, *args, **kwargs):
        return True
    async def commit(self, *args, **kwargs):
        return None
    async def reject(self, *args, **kwargs):
        return None
    async def release(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_send_news_records_successful_channel_delivery(monkeypatch, tmp_path):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'analytics_feedback', True)
    store = bot.AnalyticsStore(tmp_path / 'analytics.json')
    monkeypatch.setattr(bot, 'analytics_store', store)
    monkeypatch.setattr(bot, 'sent_links', _Ledger())
    monkeypatch.setattr(bot, 'matches_keywords', lambda n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', lambda *a, **k: _async_value(None))
    monkeypatch.setattr(bot, '_prepare_video_file', lambda *a, **k: _async_value(None))
    monkeypatch.setattr(bot, '_send_channel_post', lambda *a, **k: _async_value(True))
    monkeypatch.setattr(bot, '_commit_image_fingerprint', lambda *a, **k: None)
    monkeypatch.setattr(bot, '_mark_published', lambda: None)
    monkeypatch.setattr(bot, '_maybe_mirror_canary', lambda *a, **k: _async_value(False))
    monkeypatch.setattr(bot, 'story_history', None)
    monkeypatch.setattr(bot, 'experiments', None)
    monkeypatch.setattr(bot, 'stats', SimpleNamespace(
        record_published=lambda *a, **k: _async_value(None),
        record_skipped=lambda *a, **k: _async_value(None),
        record_failed_send=lambda *a, **k: _async_value(None),
    ))
    news = {'title': 'Test', 'source': 'RSS', 'link': 'https://example.com/x'}
    result = await bot.send_news(SimpleNamespace(), news)
    assert result == 'sent'
    assert store.delivery_summary(30)['sent'] == 1


async def _async_value(value):
    return value


def test_analytics_store_recovers_from_corrupt_json(tmp_path):
    path = tmp_path / 'analytics.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.AnalyticsStore(path)
    assert store.events() == []


@pytest.mark.asyncio
async def test_analytics_command_handles_empty_dataset(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'analytics_feedback', True)
    monkeypatch.setattr(bot, 'analytics_store', bot.AnalyticsStore(tmp_path / 'a.json'))
    monkeypatch.setattr(bot, 'moderation_feedback', SimpleNamespace(_events=[]))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=bot.ADMIN_ID))
    context = SimpleNamespace(args=[])
    await bot.analytics_command(update, context)
    text = message.reply_text.await_args.args[0]
    assert 'Analytics' in text
    assert '0/0' in text
    assert 'Просмотры' in text
