import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


def test_reservation_commit_and_restart(tmp_path):
    p = tmp_path / 'sent.json'
    s = bot.SentLinksStore(p)
    assert asyncio.run(s.claim('https://example.com/a', 'Title A')) is True
    raw = json.loads(p.read_text())
    assert raw['reservations']
    asyncio.run(s.commit('https://example.com/a', 'Title A'))
    raw = json.loads(p.read_text())
    assert raw['reservations'] == {}
    assert 'https://example.com/a' in bot.SentLinksStore(p)


def test_expired_reservation_released_after_restart(tmp_path, monkeypatch):
    p = tmp_path / 'sent.json'
    s = bot.SentLinksStore(p)
    asyncio.run(s.claim('https://example.com/a', 'Title A'))
    data = json.loads(p.read_text())
    key = next(iter(data['reservations']))
    data['reservations'][key]['at'] = time.time() - 9999
    p.write_text(json.dumps(data))
    monkeypatch.setattr(bot, 'DEDUP_RESERVATION_TTL_SEC', 1)
    again = bot.SentLinksStore(p)
    assert 'https://example.com/a' not in again
    assert asyncio.run(again.claim('https://example.com/a', 'Title A')) is True


def test_rejected_is_not_published_but_temporarily_blocks(tmp_path):
    s = bot.SentLinksStore(tmp_path / 'sent.json')
    asyncio.run(s.claim('https://example.com/a', 'Title A'))
    asyncio.run(s.reject('https://example.com/a', 'Title A', 'filtered'))
    assert 'https://example.com/a' not in s._url_set
    assert asyncio.run(s.claim('https://example.com/a', 'Title A')) is False


def test_ssrf_blocks_private_addresses():
    for url in ('http://127.0.0.1/x', 'http://10.0.0.1/x', 'http://169.254.169.254/latest'):
        assert bot._is_public_http_url(url) is False


def test_redirect_to_private_is_blocked(monkeypatch):
    first = MagicMock(status_code=302, headers={'Location': 'http://127.0.0.1/secret'})
    first.close = MagicMock()
    monkeypatch.setattr(bot, '_is_public_http_url', lambda u: '127.0.0.1' not in u)
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: first)
    assert bot.http_get_public_with_retry('https://example.com/feed') is None


def test_long_source_callback_fits_telegram_limit(monkeypatch):
    name = 'Очень длинный источник ' * 20
    monkeypatch.setattr(bot, 'SOURCES', [(name, lambda: [])])
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(is_source_enabled=lambda n: True))
    markup = bot.build_sources_menu()
    data = markup.inline_keyboard[0][0].callback_data
    assert len(data.encode('utf-8')) <= 64
    assert bot._source_name_from_callback(data[4:]) == name


def test_post_card_escapes_untrusted_html():
    text = bot._post_card(
        {'title': '<b>x</b>', 'source': 'RSS <evil>', 'link': 'https://x/?a=1&b=2'},
        {'by': {'name': '<admin>'}},
    )
    assert '<b>x</b>' not in text
    assert '&lt;b&gt;x&lt;/b&gt;' in text
    assert '&lt;evil&gt;' in text
    assert '&lt;admin&gt;' in text


def test_iana_timezone_accounts_for_dst(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.timezone_name = 'Europe/Berlin'
    monkeypatch.setattr(bot, 'settings', settings)
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    assert bot._utc_to_local(winter).hour == 13
    assert bot._utc_to_local(summer).hour == 14


def test_llm_numeric_fact_guard():
    source = 'Season 2 premieres October 4 with 12 episodes.'
    assert bot._llm_numbers_supported(source, 'Season 2 — October 4, 12 episodes')
    assert not bot._llm_numbers_supported(source, 'Season 3 — October 5, 24 episodes')


def test_priority_rewards_fresh_video(monkeypatch):
    monkeypatch.setattr(bot, 'source_health', None)
    fresh = {'title': 'Trailer premiere', 'summary': '', 'video': 'x', 'images': ['x'],
             'published_parsed': time.gmtime(time.time() - 3600)}
    old = {'title': 'Minor note', 'summary': '', 'video': None, 'images': [],
           'published_parsed': time.gmtime(time.time() - 48 * 3600)}
    assert bot._news_priority_score(fresh) > bot._news_priority_score(old)


def test_moderation_feedback_suggests_repeated_hidden_terms(tmp_path):
    f = bot.ModerationFeedback(tmp_path / 'feedback.json')
    for i in range(3):
        f.record('hidden', {'source': 'X', 'title': f'Casino promotion special {i}'})
    assert ('casino', 3) in f.blacklist_suggestions()


@pytest.mark.asyncio
async def test_notify_admin_returns_delivery_count(monkeypatch):
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: {1, 2})
    fake = MagicMock()
    fake.send_message = AsyncMock(side_effect=[None, bot.TelegramError('blocked')])
    assert await bot.notify_admin(fake, 'x') == 1
