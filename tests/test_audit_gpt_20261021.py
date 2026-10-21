"""Внешний аудит, 21.10.2026: подтвердившиеся замечания."""
import asyncio

import pytest

import anime_news_bot as bot


# ---------- ISSUE-01: commit() == False после успешной отправки ----------

def _disk_fails(*_a, **_k):
    raise OSError('disk full')


def test_failed_commit_alerts_admin_and_never_reposts(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)
    path = tmp_path / 'links.json'
    link = 'https://example.com/news/1'
    store = bot.SentLinksStore(path)
    assert asyncio.run(store.claim(link, 'Новость'))
    assert asyncio.run(store.mark_sending(link))          # граница Telegram записана
    with monkeypatch.context() as m:
        m.setattr(bot, '_atomic_write_json', _disk_fails)
        assert asyncio.run(store.commit(link, 'Новость')) is False
    assert alerts and 'не записалась' in alerts[-1]
    assert not asyncio.run(store.claim(link, 'Новость'))   # в этом процессе — не повторяем
    after_restart = bot.SentLinksStore(path)
    assert not asyncio.run(after_restart.claim(link, 'Новость'))   # и после рестарта
    assert after_restart.uncertain_count() == 1


def test_successful_commit_does_not_alert(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)
    store = bot.SentLinksStore(tmp_path / 'links.json')
    assert asyncio.run(store.claim('https://example.com/2', 'Т'))
    assert asyncio.run(store.commit('https://example.com/2', 'Т'))
    assert not alerts


# ---------- ISSUE-03: Telegram CDN по имени хоста, а не по подстроке ----------

@pytest.mark.parametrize('url, expected', [
    ('https://cdn4.telesco.pe/file/abc.mp4?token=x', True),
    ('https://telesco.pe/file/abc', True),
    ('https://cdn1.cdn-telegram.org/file/abc', True),
    ('https://cdn-telegram.org:8443/file/abc', True),
    ('https://cdn-telegram.org.example.com/file/abc', False),
    ('https://telesco.pe.evil.test/x', False),
    ('https://nottelesco.pe/x', False),
    ('https://example.com/?u=telesco.pe', False),
    ('not a url', False),
])
def test_telegram_cdn_is_matched_by_host(url, expected):
    assert bot._download_needed_host(url) is expected


def test_direct_video_on_fake_cdn_needs_an_extension():
    assert bot._is_direct_video('https://cdn4.telesco.pe/file/abc')
    assert not bot._is_direct_video('https://cdn-telegram.org.example.com/file/abc')
