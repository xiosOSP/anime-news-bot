"""Битая история публикаций: подняться с проверенной копии или встать на паузу.

С пустой историей бот выкладывал бы заново всё, что уже выходило; ежедневный
бэкап лежит у админа в личке, а не на диске, поэтому сам он восстановиться не мог.
"""
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot

LINK = 'https://example.com/news/1'


@pytest.fixture
def alerts(monkeypatch):
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    return bot._pending_admin_alerts


async def _published(path):
    store = bot.SentLinksStore(path)
    assert await store.claim(LINK, 'Some anime news title')
    await store.commit(LINK, 'Some anime news title')
    return store


@pytest.mark.asyncio
async def test_good_history_gets_a_checked_copy(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    await _published(path)
    bot.SentLinksStore(path)                       # чтение при старте снимает копию
    copy = json.loads((tmp_path / 'sent_links.json.lastgood').read_text(encoding='utf-8'))
    assert any(LINK in u for u in copy['urls'])


@pytest.mark.asyncio
async def test_corrupt_history_is_restored_from_the_copy(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    await _published(path)
    bot.SentLinksStore(path)
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert LINK in store and not store.history_lost and store.restored_from
    assert any('Восстановлена проверенная копия' in a for a in alerts)
    assert any(p.name.startswith('sent_links.json.corrupt-') for p in tmp_path.iterdir())


def test_corrupt_history_without_a_copy_pauses_publishing(tmp_path, alerts, monkeypatch):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert store.history_lost
    assert any('ПРИОСТАНОВЛЕНА' in a for a in alerts)
    monkeypatch.setattr(bot, 'sent_links', store)
    assert bot._history_paused()


def test_corrupt_copy_is_not_trusted(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    (tmp_path / 'sent_links.json.lastgood').write_text('{also broken', encoding='utf-8')
    assert bot.SentLinksStore(path).history_lost


@pytest.mark.asyncio
async def test_pause_survives_a_restart_until_the_owner_decides(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    await store.claim(LINK, 'Some anime news title')    # любая запись на диск
    assert bot.SentLinksStore(path).history_lost       # пустой, но читаемый файл паузу не снял
    assert store.accept_lost_history()
    assert not bot.SentLinksStore(path).history_lost


@pytest.mark.asyncio
async def test_copy_is_refreshed_at_most_hourly(tmp_path, alerts, monkeypatch):
    path = tmp_path / 'sent_links.json'
    store = await _published(path)
    copy = tmp_path / 'sent_links.json.lastgood'
    first = copy.read_text(encoding='utf-8')
    await store.claim('https://example.com/news/2', 'Another anime title here')
    await store.commit('https://example.com/news/2', 'Another anime title here')
    assert copy.read_text(encoding='utf-8') == first        # час ещё не прошёл
    store._lastgood_at = time.time() - store.LASTGOOD_EVERY_SEC - 1
    await store.claim('https://example.com/news/3', 'Third anime title is here')
    assert 'news/2' in copy.read_text(encoding='utf-8')


def test_lost_history_is_never_copied_over_the_good_one(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    store._lastgood_at = 0
    store._save()
    assert not (tmp_path / 'sent_links.json.lastgood').exists()


# --------------------------------------------------- пауза публикации

@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setattr(bot, 'sent_links', NS(history_lost=True, accept_lost_history=lambda: True))


@pytest.mark.asyncio
async def test_paused_bot_does_not_collect_or_publish(paused, monkeypatch):
    cycle = AsyncMock()
    monkeypatch.setattr(bot, '_check_news_cycle', cycle)
    await bot.check_news(NS(bot=None))
    cycle.assert_not_awaited()
    send = AsyncMock()
    monkeypatch.setattr(bot, '_publish_one_from_queue_locked', send)
    monkeypatch.setattr(bot, 'settings', NS(auto_enabled=True, publish_mode='channel', thread_mode=False))
    monkeypatch.setattr(bot, 'post_queue', object())
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: True)
    await bot.publisher_tick(NS(bot=None))
    send.assert_not_awaited()
    monkeypatch.setattr(bot, 'pending_posts', object())
    assert await bot._autopost_one_from_thread(None) is None


@pytest.mark.asyncio
async def test_only_the_owner_lifts_the_pause(paused, monkeypatch):
    accept = []
    bot.sent_links.accept_lost_history = lambda: accept.append(1) or True
    deny = AsyncMock()
    monkeypatch.setattr(bot, 'deny_access', deny)
    reply = AsyncMock()
    await bot.historyok_command(NS(effective_user=NS(id=bot.ADMIN_ID + 1),
                                   message=NS(reply_text=reply)), None)
    deny.assert_awaited_once()
    assert accept == []
    await bot.historyok_command(NS(effective_user=NS(id=bot.ADMIN_ID),
                                   message=NS(reply_text=reply)), None)
    assert accept == [1] and 'продолжена' in reply.await_args.args[0]


@pytest.mark.asyncio
async def test_failed_write_keeps_the_pause_and_says_so(paused):
    bot.sent_links.accept_lost_history = lambda: False
    reply = AsyncMock()
    await bot.historyok_command(NS(effective_user=NS(id=bot.ADMIN_ID),
                                   message=NS(reply_text=reply)), None)
    assert 'НЕ снята' in reply.await_args.args[0]
