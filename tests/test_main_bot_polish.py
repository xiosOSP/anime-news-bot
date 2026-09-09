"""Behavioural regression tests for the main bot's collection and safeguards."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

import anime_news_bot as bot


def news(key='a', **extra):
    return dict(title=f'Announcement {key}', link=f'https://example.com/{key}',
                images=['https://example.com/image.jpg'], **extra)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.require_image = True
    settings.video_enabled = True
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, 'stats', NS(record_collected=AsyncMock(), record_skipped=AsyncMock(),
                                      record_source_error=AsyncMock()))
    for attr in ('source_health', 'error_fingerprints', 'source_intelligence'):
        monkeypatch.setattr(bot, attr, None)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: False)
    monkeypatch.setattr(bot, '_cluster_news', lambda rows, **kw: rows)
    monkeypatch.setattr(bot, '_apply_active_verification', AsyncMock(side_effect=lambda rows: rows))
    monkeypatch.setattr(bot, '_run_source_discovery', AsyncMock())
    for name in ('_annotate_story_updates', '_annotate_editorial_automation', '_prioritize_news'):
        monkeypatch.setattr(bot, name, lambda rows: rows)
    return settings


@pytest.mark.asyncio
async def test_broken_row_does_not_discard_other_articles(collection, monkeypatch):
    rows = [news('a'), None, 'bad', {'title': []}, news('b')]
    monkeypatch.setattr(bot, 'SOURCES', [('Example', lambda: rows)])
    found, lines, errors = await bot.collect_all_news()
    assert [row['title'] for row in found] == ['Announcement a', 'Announcement b']
    assert errors == []
    assert '3 повреждено' in lines[0]
    assert '_collected_at' not in rows[0]
    assert found[0]['source'] == 'Example'


@pytest.mark.asyncio
async def test_title_dedup_is_per_source_after_corroboration(collection, monkeypatch):
    first = news('a')
    second = dict(news('b'), title=first['title'])
    repeated_second = dict(news('c'), title=first['title'])
    monkeypatch.setattr(bot, 'SOURCES', [('A', lambda: [first]), ('B', lambda: [second, repeated_second])])
    found, _, errors = await bot.collect_all_news()
    assert errors == []
    assert [row['link'] for row in found] == [first['link'], second['link']]


@pytest.mark.asyncio
@pytest.mark.parametrize('video_enabled', [True, False])
async def test_video_only_article_survives_collection_and_queue(collection, monkeypatch, tmp_path, video_enabled):
    collection.video_enabled = video_enabled
    row = dict(news(), images=[], video='https://example.com/trailer.mp4')
    monkeypatch.setattr(bot, 'SOURCES', [('A', lambda: [row])])
    found, _, errors = await bot.collect_all_news()
    assert errors == []
    assert len(found) == int(video_enabled)
    queue = bot.PostQueue(tmp_path / 'queue.json')
    assert await queue.push_many([row]) == int(video_enabled)
    item = await queue.pop_next()
    assert (item is not None) == video_enabled
    if item:
        assert item['video'] == row['video']


@pytest.mark.asyncio
async def test_source_http_failure_is_visible_without_stopping_other_sources(collection, monkeypatch):
    response = NS(status_code=403, close=Mock())
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)
    failure = bot._fetch_listing_source('https://example.com', 'Bad', base_url='https://example.com', href_pattern='/news/')
    assert failure == []  # Existing direct collector callers retain their contract.
    response.close.assert_called_once()
    monkeypatch.setattr(bot, 'SOURCES', [('Bad', lambda: failure), ('Good', lambda: [news()])])
    found, lines, errors = await bot.collect_all_news()
    assert len(found) == 1
    assert 'Bad: ❌' in lines
    assert any('HTTP 403' in error for error in errors)
    bot.stats.record_source_error.assert_awaited_once_with('Bad')


def test_rss_stream_exception_closes_connection(monkeypatch):
    response = NS(status_code=200, close=Mock())
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)
    monkeypatch.setattr(bot, '_read_limited_response', Mock(side_effect=OSError('stream broken')))
    rows = bot._parse_rss_with_fallback('https://example.com/rss', 'A')
    assert isinstance(rows, bot.SourceFetchFailure)
    assert 'stream broken' in rows.reason
    response.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [None, {}, [None, {'title': []}]])
async def test_invalid_batch_is_reported(collection, monkeypatch, rows):
    monkeypatch.setattr(bot, 'SOURCES', [('Bad', lambda: rows), ('Good', lambda: [news()])])
    found, lines, errors = await bot.collect_all_news()
    assert len(found) == 1 and len(errors) == 1
    assert 'Bad: ❌' in lines


def test_editorial_json_objects_are_not_post_text_or_tags():
    data = bot._llm_editorial_data({'title': {'bad': 'title'}, 'summary': ['paragraph'],
                                  'subject': 123, 'tags': [{}, 456, '#аниме'],
                                  'relevant': 'false', 'topic': 'аниме'})
    assert data == {'tags': ['#аниме'], 'topic': 'аниме'}
    assert not bot._llm_batch_usable({'title': {'bad': 'title'}})


def test_batch_keeps_ownership_marker_but_rejects_invalid_fields():
    raw = json.dumps({'items': [{'id': 1, 'title': {}, 'summary': []},
                                {'id': 2, 'src': 'Original title', 'title': 'Перевод', 'summary': {}}]})
    assert bot._llm_parse_batch(raw) == {2: {'src': 'Original title', 'title': 'Перевод'}}


def test_invalid_editorial_data_does_not_poison_cache(monkeypatch):
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    row = news()
    bot._llm_editorial_remember(row, {'title': {}, 'summary': [1]})
    assert bot._llm_editorial_cached(row) is None
    bot._llm_editorial_remember(row, {'title': 'Перевод', 'subject': {'bad': 1}, 'src': 'private'})
    assert bot._llm_editorial_cached(row) == {'title': 'Перевод'}


@pytest.mark.asyncio
async def test_malformed_llm_answer_uses_normal_post_preparation(collection, monkeypatch):
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_wanted', lambda: False)
    monkeypatch.setattr(bot, '_llm_source_text', AsyncMock(return_value='Original facts'))
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=json.dumps({'title': {'bad': 1}, 'summary': [1, 2]})))
    row = news()
    assert await bot._llm_enrich(row) == 'off'
    assert '_llm_text' not in row
    assert row['title'] == 'Announcement a'
    assert bot._llm_editorial_cached(row) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['off', 'observe'])
async def test_mode_change_during_member_lookup_prevents_sanction(tmp_path, monkeypatch, change):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    async def member(*args):
        store.set_chat(-100, False) if change == 'off' else store.set_mode('observe')
        return NS(status='member')
    api = NS(get_chat_member=AsyncMock(side_effect=member), delete_message=AsyncMock(),
             restrict_chat_member=AsyncMock())
    message = NS(chat_id=-100, message_id=1, from_user=NS(id=7, full_name='A'), sender_chat=None)
    decision = dict(action='mute', minutes=60, delete=True)
    await bot._mod_apply(api, message, decision, 'nsfw', 'test')
    api.delete_message.assert_not_called()
    api.restrict_chat_member.assert_not_called()
    assert store.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_late_media_result_is_quiet_after_moderation_off(tmp_path, monkeypatch):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.set_chat(-100, False)
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    api = NS(send_message=AsyncMock())
    await bot._mod_media_unchecked(api, NS(chat_id=-100), 'timeout')
    api.send_message.assert_not_called()
    assert store.recent_log(10) == []


@pytest.mark.asyncio
async def test_failed_silence_alert_retries_without_flooding(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.last_publish_at = (bot.datetime.now(bot.timezone.utc) - bot.timedelta(hours=20)).isoformat()
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, '_silence_reported', False)
    monkeypatch.setattr(bot, '_silence_retry_at', 0)
    send = AsyncMock(side_effect=[0, 1])
    monkeypatch.setattr(bot, 'notify_admin', send)
    now = [1000]
    monkeypatch.setattr(bot.time, 'monotonic', lambda: now[0])
    await bot._check_silence(NS())
    assert not bot._silence_reported
    await bot._check_silence(NS())
    assert send.await_count == 1
    now[0] += 301
    await bot._check_silence(NS())
    assert send.await_count == 2 and bot._silence_reported
    await bot._check_silence(NS())
    assert send.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('unexpected'), asyncio.CancelledError()])
async def test_interrupted_silence_alert_is_not_marked_delivered(collection, monkeypatch, error):
    monkeypatch.setattr(bot, '_silence_hours', lambda: 20)
    monkeypatch.setattr(bot, '_silence_reported', False)
    monkeypatch.setattr(bot, '_silence_retry_at', 0)
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await bot._check_silence(NS())
    assert not bot._silence_reported
    assert bot._silence_retry_at > bot.time.monotonic()
