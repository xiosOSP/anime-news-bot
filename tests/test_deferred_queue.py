import asyncio

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    bot.settings.require_image = False
    monkeypatch.setattr(bot, '_llm_deferred', {})


@pytest.mark.asyncio
async def test_deferral_survives_restart_and_keeps_ttl_and_send_retries(tmp_path, monkeypatch):
    path = tmp_path / 'queue.json'
    queue = bot.PostQueue(path)
    await queue.push_many([{'title': 'Test anime', 'link': 'https://example.invalid/1'}])
    news = await queue.pop_next()
    first_at = news['_queue_first_at']
    assert bot._llm_defer_news(news)
    assert await queue.defer(news)
    restored = bot.PostQueue(path)
    bot._llm_deferred.clear()
    assert await restored.pop_next() is None
    assert await restored.peek_next() == []
    saved = restored._items[0]['news']
    assert saved['_queue_first_at'] == first_at
    assert saved['_queue_send_failures'] == 0
    assert saved['_llm_defer_attempts'] == 1
    monkeypatch.setattr(bot, 'LLM_DEFER_MAX_ATTEMPTS', 1)
    assert not bot._llm_defer_news(saved)
    monkeypatch.setattr(bot.time, 'time', lambda: saved['_queue_retry_at'] + 1)
    assert (await restored.pop_next())['link'] == news['link']


@pytest.mark.asyncio
async def test_deferred_head_does_not_block_ready_news(tmp_path):
    queue = bot.PostQueue(tmp_path / 'queue.json')
    await queue.push_many([{'title': str(n), 'link': f'https://example.invalid/{n}'} for n in range(2)])
    first = await queue.pop_next()
    assert await queue.defer(first)
    assert (await queue.pop_next())['title'] == '1'


@pytest.mark.asyncio
async def test_storage_failure_keeps_inflight_for_recovery(tmp_path, monkeypatch):
    queue = bot.PostQueue(tmp_path / 'queue.json')
    await queue.push_many([{'title': 'Test anime', 'link': 'https://example.invalid/1'}])
    news = await queue.pop_next()
    monkeypatch.setattr(queue, '_save', lambda: False)
    assert not await queue.defer(news)
    assert queue._inflight['news'] == news
    assert queue._inflight_owner is asyncio.current_task()
    assert queue._items == []
    assert len(bot.PostQueue(queue.path)._items) == 1
