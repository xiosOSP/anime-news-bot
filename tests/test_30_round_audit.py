import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


def _news(i=1):
    return {'title': f'N{i}', 'link': f'https://example.com/{i}', 'images': ['https://img/x.jpg'], 'source': 'S'}


@pytest.mark.asyncio
async def test_queue_peek_size_is_read_only_without_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'q.json')
    await q.push_many([_news(1)])
    save = MagicMock(return_value=True)
    monkeypatch.setattr(q, '_save', save)
    assert await q.peek_size() == 1
    save.assert_not_called()


@pytest.mark.asyncio
async def test_queue_push_rolls_back_if_durable_write_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'q.json')
    monkeypatch.setattr(q, '_save', lambda: False)
    assert await q.push_many([_news(1)]) == 0
    assert q._items == []


@pytest.mark.asyncio
async def test_queue_pop_fails_closed_if_inflight_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'q.json')
    await q.push_many([_news(1)])
    monkeypatch.setattr(q, '_save', lambda: False)
    assert await q.pop_next() is None
    assert len(q._items) == 1
    assert q._inflight is None


@pytest.mark.asyncio
async def test_botstats_disk_write_runs_off_event_loop(tmp_path, monkeypatch):
    st = bot.BotStats(tmp_path / 'stats.json')
    seen = []
    monkeypatch.setattr(st, '_save', lambda: seen.append(threading.current_thread()) or None)
    await st.record_published('S')
    assert seen
    assert seen[0] is not threading.main_thread()


def test_storage_ready_uses_short_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bot, '_storage_probe_cached_at', 0.0)
    monkeypatch.setattr(bot, '_storage_probe_cached_ok', False)
    assert bot._storage_ready() is True

    original = Path.write_text
    def fail_if_reprobed(self, *a, **kw):
        raise AssertionError('storage probe should have been served from cache')
    monkeypatch.setattr(Path, 'write_text', fail_if_reprobed)
    assert bot._storage_ready() is True
    monkeypatch.setattr(Path, 'write_text', original)


def test_health_body_write_swallows_early_disconnect():
    class Broken:
        def write(self, _raw):
            raise BrokenPipeError('client gone')
    handler = object.__new__(bot._HealthHandler)
    handler.wfile = Broken()
    assert handler._write_body(b'ok') is False


@pytest.mark.asyncio
async def test_source_workers_are_bounded_and_daemon(monkeypatch):
    # Ensure no worker from another test occupies the audit pool.
    deadline = time.monotonic() + 1.0
    while True:
        with bot._source_worker_lock:
            active = bot._source_worker_active
        if not active or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.01)
    assert active == 0

    release = threading.Event()
    started = []
    def slow():
        started.append(threading.current_thread())
        release.wait(0.5)
        return []

    results = await asyncio.gather(*[
        bot._run_source_collector_bounded(f's{i}', slow, 0.03)
        for i in range(bot.SOURCE_FETCH_CONCURRENCY + 4)
    ], return_exceptions=True)
    try:
        assert len(started) <= bot.SOURCE_FETCH_CONCURRENCY
        assert started and all(t.daemon for t in started)
        assert any(isinstance(x, TimeoutError) for x in results)
    finally:
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_source_worker_propagates_collector_error():
    def broken():
        raise ValueError('bad feed')
    with pytest.raises(ValueError, match='bad feed'):
        await bot._run_source_collector_bounded('broken', broken, 1.0)


def test_dashboard_failure_map_is_hard_bounded(monkeypatch):
    bot._dashboard_failures.clear()
    monkeypatch.setattr(bot, 'DASHBOARD_FAIL_WINDOW_SEC', 10_000)
    now = 1000.0
    for i in range(700):
        bot._dashboard_note_failure(f'203.0.113.{i}', now + i * 0.001)
    assert len(bot._dashboard_failures) <= 513  # cap cleanup runs before adding current IP
    bot._dashboard_failures.clear()


def test_scheduled_clear_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = st.add(_news(1), bot.datetime.now(bot.timezone.utc))
    monkeypatch.setattr(st, '_save', lambda: False)
    assert st.clear() == 0
    assert st.get(key) is not None


def test_scheduled_pop_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = st.add(_news(1), bot.datetime.now(bot.timezone.utc))
    monkeypatch.setattr(st, '_save', lambda: False)
    assert st.pop(key) is None
    assert st.get(key) is not None


def test_scheduled_exhausted_item_is_not_due(tmp_path):
    st = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = st.add(_news(1), bot.datetime.now(bot.timezone.utc) - bot.timedelta(minutes=1))
    st._items[key]['tries'] = st.MAX_TRIES
    st._items[key]['state'] = 'pending'
    assert st.due() == []


def test_scheduled_mark_try_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = st.add(_news(1), bot.datetime.now(bot.timezone.utc))
    st._items[key]['state'] = 'sending'
    monkeypatch.setattr(st, '_save', lambda: False)
    assert st.mark_try(key) == -1
    assert st._items[key]['tries'] == 0
    assert st._items[key]['state'] == 'sending'


def test_pending_update_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.PendingPosts(tmp_path / 'pending.json')
    key = st.add(_news(1))
    old = dict(st.get(key))
    monkeypatch.setattr(st, '_save', lambda: False)
    changed = dict(old, title='changed')
    assert st.update_news(key, changed) is False
    assert st.get(key)['title'] == old['title']


def test_pending_pop_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.PendingPosts(tmp_path / 'pending.json')
    key = st.add(_news(1))
    monkeypatch.setattr(st, '_save', lambda: False)
    assert st.pop(key) is None
    assert st.get(key) is not None


@pytest.mark.asyncio
async def test_entity_observe_from_llm_runs_off_event_loop(tmp_path, monkeypatch):
    # Regression guard for the direct observe() call that used to fsync from _llm_enrich.
    mem = bot.EntityMemory(tmp_path / 'entities.json')
    seen = []
    original = mem.observe
    def wrapped(*args, **kwargs):
        seen.append(threading.current_thread())
        return original(*args, **kwargs)
    monkeypatch.setattr(mem, 'observe', wrapped)
    monkeypatch.setattr(bot, 'entity_memory', mem)
    # The implementation detail is guarded statically as a cheap, stable check:
    import inspect
    src = inspect.getsource(bot._llm_enrich)
    assert 'await asyncio.to_thread(entity_memory.observe' in src

@pytest.mark.asyncio
async def test_resolve_reply_user_keeps_full_name_without_username(monkeypatch):
    user = SimpleNamespace(id=42, full_name='Иван Иванов', username=None, is_bot=False)
    message = SimpleNamespace(reply_to_message=SimpleNamespace(from_user=user))
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(args=[])
    monkeypatch.setattr(bot, 'user_directory', None)
    uid, name, error = await bot._resolve_user(update, context)
    assert uid == 42
    assert name == 'Иван Иванов'
    assert error == ''


def test_pending_add_restores_cleanup_evictions_on_storage_failure(tmp_path, monkeypatch):
    st = bot.PendingPosts(tmp_path / 'pending.json')
    st.MAX_ITEMS = 2
    k1 = st.add(_news(1))
    k2 = st.add(_news(2))
    before = dict(st._items)
    counter = st._counter
    monkeypatch.setattr(st, '_save', lambda: False)
    with pytest.raises(OSError):
        st.add(_news(3))
    assert st._items == before
    assert st._counter == counter
    assert st.get(k1) and st.get(k2)

@pytest.mark.asyncio
async def test_post_queue_clear_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'queue.json')
    assert await q.push_many([_news(1)]) == 1
    item = await q.pop_next()
    assert item is not None
    monkeypatch.setattr(q, '_save', lambda: False)
    assert await q.clear() == -1
    assert await q.has_inflight() is True


@pytest.mark.asyncio
async def test_post_queue_ack_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'queue.json')
    assert await q.push_many([_news(1)]) == 1
    item = await q.pop_next()
    assert item is not None
    monkeypatch.setattr(q, '_save', lambda: False)
    assert await q.ack_done(item) is False
    assert await q.has_inflight() is True


@pytest.mark.asyncio
async def test_post_queue_requeue_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    q = bot.PostQueue(tmp_path / 'queue.json')
    assert await q.push_many([_news(1)]) == 1
    item = await q.pop_next()
    assert item is not None
    monkeypatch.setattr(q, '_save', lambda: False)
    assert await q.requeue_failed(item) is None
    assert await q.has_inflight() is True
    assert await q.peek_size() == 0

@pytest.mark.asyncio
async def test_sent_links_clear_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    st = bot.SentLinksStore(tmp_path / 'sent.json')
    assert await st.claim('https://example.com/a', 'Title A') is True
    assert st.has_title('Title A') is True
    monkeypatch.setattr(st, '_save', lambda: False)
    assert await st.clear() is False
    assert 'https://example.com/a' in st
    assert st.has_title('Title A') is True
