"""Regression tests for the repository audit findings.

All Telegram, LLM,
translation, and DNS/socket interactions in these probes are substituted locally.
"""
import asyncio
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import anime_news_bot as bot


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: False)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock(), record_failed_send=AsyncMock(), record_published=AsyncMock()))
    monkeypatch.setattr(bot, 'sent_links', bot.SentLinksStore(tmp_path / 'sent.json'))
    monkeypatch.setattr(bot, 'pending_posts', None)
    monkeypatch.setattr(bot, 'published_texts', None)
    monkeypatch.setattr(bot, 'recent_subjects', None)
    monkeypatch.setattr(bot, '_llm_deferred', {})
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, '_translation_cache', {})
    monkeypatch.setattr(bot, '_channel_send_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs_async', AsyncMock(return_value={}))
    monkeypatch.setattr(bot, '_event_log', lambda *a, **kw: None)
    monkeypatch.setattr(bot, '_mark_published', lambda: None)
    monkeypatch.setattr(bot, '_maybe_mirror_canary', AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_deferred_posts_remain_in_durable_queue(monkeypatch, tmp_path):
    bot.settings.require_image = False
    queue = bot.PostQueue(tmp_path / 'queue.json')
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, 'matches_keywords', lambda news: True)
    for name in ('_improve_thumb', '_discover_article_video', '_optimize_news_media'):
        monkeypatch.setattr(bot, name, AsyncMock())
    monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='defer'))
    monkeypatch.setattr(bot, '_llm_prefetch_queue_head', AsyncMock(return_value=0))
    await queue.push_many([{'title': 'Anime lunar mission announced', 'link': 'https://news.invalid/1',
                            'source': 'audit', 'images': []}])
    result, _ = await bot._publish_one_from_queue(MagicMock())
    assert json.loads(queue.path.read_text(encoding='utf-8'))
    assert result == 'deferred'
    assert await queue.peek_size() == 1, 'A deferred, unsent post was permanently removed from the queue'


@pytest.mark.asyncio
async def test_first_llm_network_failure_defers_post(monkeypatch):
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_wanted', lambda: True)
    monkeypatch.setattr(bot, '_llm_source_text', AsyncMock(return_value='An anime is announced.'))
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=None))
    result = await bot._llm_enrich({'title': 'Anime news', 'link': 'https://news.invalid/2'})
    assert result == 'defer', 'A transient failure falls through to raw publication immediately'


@pytest.mark.asyncio
async def test_channel_translation_runs_off_event_loop(monkeypatch):
    bot.settings.require_image = False
    bot.settings.video_enabled = False
    event_loop_thread = threading.get_ident()
    translation_threads = []

    def translate(text, **kwargs):
        translation_threads.append(threading.get_ident())
        return 'Новое аниме получило дату премьеры'

    monkeypatch.setattr(bot, 'translate_text', translate)
    transport = MagicMock(send_message=AsyncMock())
    ok = await bot._send_post(transport, {'title': 'A new animated feature has been announced',
                                       'source': 'audit', 'images': []}, 123, None)
    assert ok and translation_threads
    assert event_loop_thread not in translation_threads, 'Network translation executes on the Telegram event loop'


def test_google_translation_sets_network_timeout(monkeypatch):
    captured = []

    def fake_get(*args, **kwargs):
        captured.append(kwargs)
        raise requests.Timeout('Audit stub: no external request made')

    monkeypatch.setattr(requests, 'get', fake_get)
    translator = bot.GoogleTranslator(source='en', target='ru')
    with pytest.raises(requests.Timeout):
        translator.translate('An animated feature is announced')
    assert captured[0].get('timeout'), 'The fallback translator makes an unbounded HTTP request'


def test_public_url_validation_pins_dns_result(monkeypatch):
    resolutions = []
    connections = []

    def fake_dns(host, port, *args, **kwargs):
        ip = '8.8.8.8' if not resolutions else '127.0.0.1'
        resolutions.append(ip)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))]

    class FakeSocket:
        def __init__(self, *args, **kwargs): pass
        def setsockopt(self, *args): pass
        def settimeout(self, *args): pass
        def close(self): pass
        def connect(self, address):
            connections.append(address)
            raise OSError('Audit stub blocked actual connection')

    session = requests.Session()
    session.trust_env = False
    monkeypatch.setattr(socket, 'getaddrinfo', fake_dns)
    monkeypatch.setattr(socket, 'socket', FakeSocket)
    monkeypatch.setattr(requests, 'get', session.get)
    monkeypatch.setattr(bot, 'HTTP_RETRY_ATTEMPTS', 1)
    bot.http_get_public_with_retry('http://rebind.invalid/article', timeout=1)
    assert all(address[0] != '127.0.0.1' for address in connections), 'DNS rebinding reaches loopback after validation'


def test_manifest_check_detects_changed_content(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('audit_manifest', REPO / 'tools' / 'build_manifest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'files': {'app.py': {'sha256': 'original', 'bytes': 4}}}))
    monkeypatch.setattr(module, 'MANIFEST', manifest)
    monkeypatch.setattr(module, 'build', lambda: {'files': {'app.py': {'sha256': 'changed', 'bytes': 999}}})
    monkeypatch.setattr(sys, 'argv', ['build_manifest.py', '--check'])
    code = module.main()
    assert code != 0, 'Manifest validator accepts changed hashes and sizes'


@pytest.mark.asyncio
async def test_album_is_not_resent_when_only_buttons_failed(monkeypatch, tmp_path):
    bot.settings.require_image = True
    bot.settings.video_enabled = False
    monkeypatch.setattr(bot, 'pending_posts', bot.PendingPosts(tmp_path / 'pending.json'))
    monkeypatch.setattr(bot, 'matches_keywords', lambda news: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_resolve_photos_for_album', AsyncMock(side_effect=lambda photos, *args: photos))
    monkeypatch.setattr(bot, '_tg_call_flood_safe', lambda factory: factory())
    transport = MagicMock(send_media_group=AsyncMock(return_value=[]),
                          send_message=AsyncMock(side_effect=bot.BadRequest('Audit: buttons rejected')))
    news = {'title': 'Новый аниме-фильм', 'lang': 'ru', 'source': 'audit',
            'link': 'https://news.invalid/album', 'images': ['https://img.invalid/1.jpg', 'https://img.invalid/2.jpg']}
    first = await bot.send_news_to_thread(transport, dict(news))
    second = await bot.send_news_to_thread(transport, dict(news))
    assert first == 'uncertain'
    assert second == 'skipped_dup'
    assert transport.send_media_group.await_count == 1, 'A delivered album is resent after the separate buttons message fails'


@pytest.mark.asyncio
async def test_inline_llm_retry_respects_daily_call_limit(monkeypatch):
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_llm_last_call', 0)
    monkeypatch.setattr(bot, '_llm_pace_for', lambda slot: 0)
    monkeypatch.setattr(bot, '_llm_route_for', lambda task: None)
    monkeypatch.setattr(bot, '_llm_fallback_configured', lambda: False)
    monkeypatch.setattr(bot, '_llm_last_failure', {})
    monkeypatch.setattr(bot, '_llm_wait_hint_sec', 0)
    monkeypatch.setattr(bot, '_llm_note_primary_recovered', lambda: None)
    monkeypatch.setattr(bot, '_llm_pace_faster', lambda slot: None)
    counted = []
    actual = []
    monkeypatch.setattr(bot, '_llm_quota_left', lambda: max(0, 1 - len(counted)))
    monkeypatch.setattr(bot, '_llm_count_call', lambda: counted.append(1))

    def request(*args, **kwargs):
        actual.append(1)
        if len(actual) == 1:
            bot._llm_last_failure['kind'] = 'rate_limit'
            bot._llm_wait_hint_sec = 0.001
            return None
        return '{}'

    monkeypatch.setattr(bot, '_llm_request', request)
    await bot._llm_call([{'role': 'user', 'content': 'audit'}])
    assert len(actual) <= 1, 'Inline retry bypasses the daily call limit and is not counted'


@pytest.mark.asyncio
async def test_rejected_photo_uses_bytes_fallback(monkeypatch):
    bot.settings.require_image = True
    bot.settings.video_enabled = False
    monkeypatch.setattr(bot, 'matches_keywords', lambda news: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_resolve_photos_for_album', AsyncMock(side_effect=lambda photos, *args: photos))
    downloader = MagicMock(return_value=b'audit-image')
    monkeypatch.setattr(bot, '_download_image_bytes', downloader)
    transport = MagicMock(send_photo=AsyncMock(side_effect=[bot.BadRequest('Wrong file identifier/HTTP URL specified'), object()]))
    news = {'title': 'Новый аниме-фильм получил дату премьеры', 'source': 'audit', 'lang': 'ru',
            'link': 'https://news.invalid/photo', 'images': ['https://img.invalid/photo.jpg']}
    result = await bot.send_news(transport, news)
    assert transport.send_photo.await_count == 2
    assert downloader.call_count == 1
    assert result == 'sent', 'A definite Telegram rejection is incorrectly made uncertain, bypassing bytes fallback'
