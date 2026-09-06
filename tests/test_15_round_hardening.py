"""Регрессии для 15-раундового hardening-аудита.

Здесь только случаи, которые раньше не покрывались и реально могли приводить
к падениям, утечкам ресурсов, порче runtime-настроек или обходу лимитов.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


import anime_news_bot as bot


class _HtmlResp:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.text = text
        self.headers = {'Content-Type': 'text/html; charset=utf-8'}
        self.closed = False

    def close(self):
        self.closed = True


ARTICLE = '''<html><body><article>
<p>This is a sufficiently long first paragraph about an anime production announcement.</p>
<p>This second paragraph contains additional verified information about the same production.</p>
</article></body></html>'''


def test_anilist_is_optional_for_title_protection(monkeypatch):
    monkeypatch.setattr(bot, 'anilist', None)
    text = 'Studio Trigger Announces New Anime Project'
    protected, placeholders = bot.anilist_protect_titles(text)
    assert protected == text
    assert placeholders == {}


def test_article_detail_and_text_caches_do_not_collide(monkeypatch):
    monkeypatch.setattr(bot, '_article_cache', {})
    monkeypatch.setattr(bot, '_article_text_cache', {})
    monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: _HtmlResp(ARTICLE))

    detail = bot.fetch_article('https://example.test/a')
    text = bot.fetch_full_article_text('https://example.test/a')

    assert isinstance(detail, dict)
    assert isinstance(text, str)
    assert isinstance(bot._article_cache['https://example.test/a'], dict)
    assert isinstance(bot._article_text_cache['https://example.test/a'], str)


def test_article_caches_safe_in_reverse_call_order(monkeypatch):
    monkeypatch.setattr(bot, '_article_cache', {})
    monkeypatch.setattr(bot, '_article_text_cache', {})
    monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: _HtmlResp(ARTICLE))

    assert isinstance(bot.fetch_full_article_text('https://example.test/b'), str)
    assert isinstance(bot.fetch_article('https://example.test/b'), dict)


def test_settings_sanitize_corrupted_runtime_json(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'check_interval_min': -100,
        'post_max_age_hours': 0,
        'timezone_name': 'Mars/Olympus',
        'extra_admins': ['2', 'bad', -7, 2],
        'disabled_sources': [123, ' ANN ', 'ann', ''],
        'llm_calls_today': -50,
        'deepl_chars': -1,
    }), encoding='utf-8')
    monkeypatch.setattr(bot, 'ADMIN_ID', 999999)

    settings = bot.BotSettings(path)

    assert settings.check_interval_min == 5
    assert settings.post_max_age_hours == 1
    assert settings.timezone_name == ''
    assert settings.extra_admins == [2]
    assert settings._data['disabled_sources'] == ['ANN']
    assert settings.llm_calls_today == 0
    assert settings.deepl_chars == 0


def test_queue_ttl_accepts_timezone_aware_legacy_timestamp(tmp_path):
    q = bot.PostQueue(tmp_path / 'queue.json')
    q._items = []
    old = datetime.now(timezone.utc) - timedelta(hours=bot.QUEUE_POST_TTL_HOURS + 1)
    item = {'news': {'link': 'https://x.test/1'}, 'queued_at': old.isoformat()}
    assert q._is_expired(item) is True


def test_pending_posts_ignore_corrupt_timestamp(tmp_path):
    now = bot.time.time()
    path = tmp_path / 'pending.json'
    path.write_text(json.dumps({
        'counter': 2,
        'items': {
            '1': {'news': {'link': 'a'}, 'ts': 'not-a-number'},
            '2': {'news': {'link': 'b'}, 'ts': now},
        },
    }), encoding='utf-8')
    store = bot.PendingPosts(path)
    store._cleanup()
    assert '1' not in store._items
    assert '2' in store._items


def test_scheduled_posts_bad_retry_counter_is_repaired(tmp_path):
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    path = tmp_path / 'scheduled.json'
    path.write_text(json.dumps({
        'counter': 1,
        'items': {'1': {'news': {'link': 'a'}, 'at': when.isoformat(), 'tries': 'oops'}},
    }), encoding='utf-8')
    store = bot.ScheduledPosts(path)
    assert store.meta('1')['tries'] == 0
    assert store.mark_try('1') == 1


def test_get_retry_closes_failed_response(monkeypatch):
    failed = MagicMock(status_code=500, headers={})
    ok = MagicMock(status_code=200, headers={})
    monkeypatch.setattr(bot.requests, 'get', MagicMock(side_effect=[failed, ok]))
    monkeypatch.setattr(bot.time, 'sleep', lambda *_: None)

    assert bot.http_get_with_retry('https://example.test') is ok
    failed.close.assert_called_once()


def test_post_retry_closes_429_and_honours_retry_after(monkeypatch):
    rate = MagicMock(status_code=429, headers={'Retry-After': '0'})
    ok = MagicMock(status_code=200, headers={})
    sleeps = []
    monkeypatch.setattr(bot.requests, 'post', MagicMock(side_effect=[rate, ok]))
    monkeypatch.setattr(bot.time, 'sleep', lambda value: sleeps.append(value))

    assert bot.http_post_with_retry('https://example.test', json_body={}) is ok
    rate.close.assert_called_once()
    assert sleeps == [0.0]


def test_public_url_guard_rejects_credentials_private_and_huge_url():
    assert bot._is_public_http_url('http://127.0.0.1/admin') is False
    assert bot._is_public_http_url('http://user:pass@example.com/') is False
    assert bot._is_public_http_url('https://example.com/' + 'a' * bot.HTTP_URL_MAX_CHARS) is False


def test_stream_error_never_falls_back_to_unbounded_content():
    class Broken:
        headers = {}
        content = b'small-but-must-not-be-used'

        def iter_content(self, chunk_size=1):
            raise OSError('stream broke')

    assert bot._read_limited_response(Broken(), 1024) is None


def test_llm_month_guard_rejects_new_date_words():
    assert bot._llm_dates_supported('Premiere is in October.', 'Премьера состоится в октябре.')
    assert not bot._llm_dates_supported('Premiere is in October.', 'Премьера состоится в ноябре.')


def test_rss_body_limit_closes_response(monkeypatch):
    response = MagicMock(status_code=200)
    response.headers = {'Content-Length': str(bot.HTTP_RSS_MAX_BYTES + 1)}
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)

    assert bot._parse_rss_with_fallback('https://feed.test/rss', 'Test') == []
    response.close.assert_called_once()


def test_bounded_cache_evicts_oldest_instead_of_clearing_everything():
    cache = {'a': 1, 'b': 2, 'c': 3}
    bot._bounded_cache_put(cache, 'd', 4, 3)
    assert cache == {'b': 2, 'c': 3, 'd': 4}
