"""Restart recovery must distinguish preparation from actual Telegram delivery."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import news_deferral
from news_deferral import NewsDeferralStore
from publication_delivery import PublicationBot


@pytest.mark.parametrize('state', ['uncertain', 'sending', 'preparing'])
def test_full_pending_queue_keeps_unresolved_or_active_publication(tmp_path, monkeypatch, state):
    store = bot.PendingPosts(tmp_path / 'pending.json')
    monkeypatch.setattr(store, 'MAX_ITEMS', 1)
    key = store.add({'title': 'Old', 'link': 'https://example.com/old'})
    store._items[key]['ts'] = 1
    if state == 'preparing':
        monkeypatch.setattr(bot, '_publishing_now', {f'pending:{key}'})
    else:
        store._items[key]['channel_state'] = state
    with pytest.raises(OverflowError):
        store.add({'title': 'New', 'link': 'https://example.com/new'})
    assert store.get(key) is not None
    assert len(store._items) == 1


def test_wait_budget_survives_repeated_process_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bot, 'LLM_DEFER_MAX_ATTEMPTS', 3)
    results = []
    for _ in range(5):
        monkeypatch.setattr(bot, '_llm_deferral_store', None)
        monkeypatch.setattr(bot, '_llm_deferred', {})
        results.append(bot._llm_defer_news({'link': 'https://example.com/article'}))
    assert results == [True, True, True, False, False]
    assert 'example.com' not in (tmp_path / 'news_llm_deferrals.json').read_text()


def test_wait_deadline_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'wait.json'
    monkeypatch.setattr(news_deferral.time, 'time', lambda: 10000)
    assert NewsDeferralStore(path).reserve('one', 0, 3, 900) == (True, 1)
    monkeypatch.setattr(news_deferral.time, 'time', lambda: 10901)
    assert NewsDeferralStore(path).reserve('one', 0, 3, 900) == (False, 1)


def test_capacity_never_resets_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(NewsDeferralStore, 'MAX_ITEMS', 2)
    path = tmp_path / 'wait.json'
    store = NewsDeferralStore(path)
    assert store.reserve('a', 0, 1, 900)[0]
    assert store.reserve('b', 0, 1, 900)[0]
    assert not store.reserve('c', 0, 1, 900)[0]
    assert not NewsDeferralStore(path).reserve('a', 0, 1, 900)[0]


@pytest.mark.parametrize('content', ['broken', '[]', '{"items": {"a": {"attempts": -1}}}'])
def test_bad_wait_storage_disables_optional_waiting_only(tmp_path, content):
    path = tmp_path / 'wait.json'
    path.write_text(content)
    store = NewsDeferralStore(path)
    assert store.storage_error
    assert not store.reserve('a', 0, 3, 900)[0]
    assert path.read_text() == content


def test_failed_wait_write_does_not_hold_news(tmp_path, monkeypatch):
    store = NewsDeferralStore(tmp_path / 'wait.json')
    def fail():
        raise OSError('disk full')
    monkeypatch.setattr(store, '_save', fail)
    assert store.reserve('a', 0, 3, 900) == (False, 0)
    assert store.storage_error


@pytest.mark.asyncio
async def test_publication_proxy_guards_first_rpc_only():
    api = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock(), get_file=AsyncMock())
    reserve = AsyncMock(return_value=True)
    delivery = PublicationBot(api, reserve)
    await delivery.get_file('x')
    assert not delivery.started
    reserve.assert_not_called()
    await delivery.send_photo(chat_id=1, photo='x')
    await delivery.send_message(chat_id=1, text='x')
    reserve.assert_awaited_once()
    assert delivery.started


@pytest.mark.asyncio
async def test_failed_reservation_never_calls_telegram():
    api = SimpleNamespace(send_message=AsyncMock())
    delivery = PublicationBot(api, lambda: False)
    with pytest.raises(RuntimeError, match='reservation'):
        await delivery.send_message(chat_id=1, text='x')
    assert not delivery.started
    api.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('thread', [False, True])
@pytest.mark.parametrize('during_rpc', [False, True])
async def test_cancel_before_or_during_delivery(tmp_path, monkeypatch, thread, during_rpc):
    ledger = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, 'sent_links', ledger)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    async def send(delivery, news, *args):
        # Even after entering the high-level sender, formatting/media are pre-RPC.
        data = json.loads((tmp_path / 'links.json').read_text())
        assert 'sending' not in str(data)
        if during_rpc:
            await delivery.send_message(chat_id=1, text='x')
        raise asyncio.CancelledError()
    monkeypatch.setattr(bot, '_send_post_thread_split' if thread else '_send_channel_post', send)
    api = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
    news = {'title': 'New announcement', 'link': 'https://example.com/a', 'source': 'X'}
    with pytest.raises(asyncio.CancelledError):
        await (bot.send_news_to_thread(api, news) if thread else bot.send_news(api, news))
    assert ledger.uncertain_count() == int(during_rpc)
    assert (news['link'] in ledger) == during_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize('during_rpc', [False, True])
async def test_scheduled_cancel_preserves_retry_only_before_rpc(tmp_path, monkeypatch, during_rpc):
    store = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = store.add({'title': 'Announcement', 'link': 'https://example.com/a'},
                    bot.datetime.now(bot.timezone.utc) - bot.timedelta(minutes=1))
    monkeypatch.setattr(bot, 'scheduled_posts', store)
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    async def send(delivery, *args):
        assert store.meta(key)['state'] == 'pending'
        if during_rpc:
            await delivery.send_message(chat_id=1, text='x')
        raise asyncio.CancelledError()
    monkeypatch.setattr(bot, '_send_channel_post', send)
    api = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await bot.publish_scheduled(SimpleNamespace(bot=api))
    assert store.meta(key)['state'] == ('uncertain' if during_rpc else 'pending')
