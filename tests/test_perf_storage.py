"""Оптимизация записи хранилищ: быстрее, но на диске то же самое."""
import json

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('data', [
    {'a': 1, 2: 'ключ-число', 'вложенное': {'x': [1, 2.5, None, True], 'y': 'ё"\\\\\\n'}},
    [{'a': 1}, 'строка', 3],
    {},
    {'one': {'deep': list(range(50))}},
    'просто строка',
])
def test_written_json_reads_back_identically(tmp_path, data):
    path = tmp_path / 'x.json'
    bot._atomic_write_json(path, data)
    assert json.loads(path.read_text(encoding='utf-8')) == json.loads(json.dumps(data))
    bot._atomic_write_json(path, data, indent=2)
    assert json.loads(path.read_text(encoding='utf-8')) == json.loads(json.dumps(data))


def test_small_files_stay_readable_large_ones_are_compact(tmp_path):
    path = tmp_path / 'settings.json'
    bot._atomic_write_json(path, {'a': 1, 'b': [1, 2]}, indent=2)
    assert '\n  "a": 1' in path.read_text(encoding='utf-8')
    big = {'items': ['x' * 100] * 2000}
    bot._atomic_write_json(path, big, indent=2)
    text = path.read_text(encoding='utf-8')
    assert '\n' not in text and json.loads(text) == big
    big_list = ['x' * 100] * 2000
    bot._atomic_write_json(path, big_list, indent=2)
    assert '\n' not in path.read_text(encoding='utf-8')


def test_dump_streams_by_section_with_the_fast_encoder(tmp_path, monkeypatch):
    # json.dump(f) идёт через кодировщик на чистом Python — его не зовём вовсе.
    def slow(*_a, **_k):
        raise AssertionError('медленный json.dump')
    monkeypatch.setattr(bot.json, 'dump', slow)
    sections = []
    real = bot.json.dumps

    def spy(obj, **kw):
        sections.append(obj)
        return real(obj, **kw)
    monkeypatch.setattr(bot.json, 'dumps', spy)
    bot._atomic_write_json(tmp_path / 'm.json', {'users': {'a': 1}, 'log': [1, 2]})
    assert sections == [{'users': {'a': 1}}, {'log': [1, 2]}]


def test_unserialisable_data_leaves_the_old_file(tmp_path):
    path = tmp_path / 'm.json'
    bot._atomic_write_json(path, {'a': 1})
    with pytest.raises(TypeError):
        bot._atomic_write_json(path, {'a': 2, 'b': object()})
    assert json.loads(path.read_text(encoding='utf-8')) == {'a': 1}
    assert [p.name for p in tmp_path.iterdir()] == ['m.json']


# ------------------------------------- хранилище модерации: откат по разделам

def _store(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    store.reserve_incident('i1', -100, 5, 'spam', 'warn')
    store.update_incident('i1', status='confirmed', add_warning=True)
    store.ensure_review('r1', -100, 5, 'nsfw', 'delete', 'детектор', 'x', 'текст')
    store.block_media('b1', -100, 'nsfw', ['f1'], ['d' * 16], 'admin')
    return store


CALLS = {
    'ensure_review': lambda s: s.ensure_review('r2', -100, 6, 'nsfw', 'delete', 'x', 'y', 'z'),
    'block_media': lambda s: s.block_media('b2', -100, 'nsfw', ['f2'], [], 'admin'),
    'unblock_media': lambda s: s.unblock_media('b1'),
    'record_review_feedback': lambda s: s.record_review_feedback('r1', 'correct'),
    'record_missed_violation': lambda s: s.record_missed_violation('m1', -100, 5, 'spam', 'т'),
    'reserve_incident': lambda s: s.reserve_incident('i2', -100, 6, 'spam', 'warn'),
    'update_incident': lambda s: s.update_incident('i1', status='undone', add_warning=True),
    'undo_incident': lambda s: s.undo_incident('i1'),
    'set_mode': lambda s: s.set_mode('observe'),
    'set_moderate_admins': lambda s: s.set_moderate_admins(not s.moderate_admins),
    'set_chat': lambda s: s.set_chat(-200, True),
    'add_warn': lambda s: s.add_warn(-100, 6, 'spam'),
    'clear_warns': lambda s: s.clear_warns(-100, 5),
}


@pytest.mark.parametrize('name', sorted(CALLS))
def test_failed_write_rolls_back_only_what_the_method_changed(tmp_path, monkeypatch, name):
    import copy
    store = _store(tmp_path)
    store._last_save = bot.time.monotonic()
    store.log_decision(-100, 5, 'У', 'spam', 'warn', 'правила', 'r', 'ждёт записи')
    before = copy.deepcopy({k: v for k, v in store._data.items() if k != 'log'})

    def broken(*_a, **_k):
        raise OSError('диск переполнен')
    monkeypatch.setattr(bot, '_atomic_write_json', broken)
    try:
        result = CALLS[name](store)
    except OSError:
        result = None                     # add_warn/clear_warns сообщают о сбое исключением
    assert not result
    assert {k: v for k, v in store._data.items() if k != 'log'} == before
    # Строка журнала, ещё не ушедшая на диск, откатом не выброшена.
    assert any(row.get('text') == 'ждёт записи' for row in store._data['log'])


def test_save_does_not_copy_the_whole_store(tmp_path, monkeypatch):
    store = _store(tmp_path)
    copies = []
    real = bot.copy.deepcopy
    monkeypatch.setattr(bot.copy, 'deepcopy', lambda obj, *a: copies.append(type(obj)) or real(obj, *a))
    store.add_warn(-100, 6, 'spam')
    # Копируется только раздел users для отката — не всё хранилище.
    assert copies == [dict]
    assert store._save()
    assert copies == [dict]


# ------------------------------------- статистика сбора: одна запись за цикл

def _count_writes(monkeypatch):
    writes = []
    real = bot._atomic_write_json

    def counting(path, data, **kw):
        writes.append(bot.Path(path).name)
        return real(path, data, **kw)
    monkeypatch.setattr(bot, '_atomic_write_json', counting)
    return writes


def test_source_yield_marks_and_flushes_once(tmp_path, monkeypatch):
    store = bot.SourceYieldStore(tmp_path / 'yield.json')
    writes = _count_writes(monkeypatch)
    for i in range(30):
        store.record_story(f'story{i}', ['A', 'B'])
        store.record_fetch('A', raw=5, fresh=2, duplicates=1, no_image=0, duration_sec=.1)
    store.record_error('B')
    assert writes == []
    store.flush()
    store.flush()                                   # нечего писать — не пишет
    assert writes == ['yield.json']
    again = bot.SourceYieldStore(tmp_path / 'yield.json')
    assert again._rows['A']['unique_stories'] == 30 and again._rows['B']['errors'] == 1
    store.record_published('A')                     # редкое событие — сразу
    assert writes == ['yield.json', 'yield.json']


@pytest.mark.asyncio
async def test_collection_stats_are_written_once_per_cycle(tmp_path, monkeypatch):
    stats = bot.BotStats(tmp_path / 'stats.json')
    replay = bot.ReplayBuffer(tmp_path / 'replay.json')
    monkeypatch.setattr(bot, 'stats', stats)
    monkeypatch.setattr(bot, 'replay_buffer', replay)
    writes = _count_writes(monkeypatch)
    for name in ('A', 'B', 'C'):
        await stats.record_collected(name, 3, save=False)
        await stats.record_skipped('duplicate', name, 2, save=False)
        replay.capture_many([{'title': f'{name} {i}', 'link': f'https://{name}/{i}'} for i in range(3)],
                            save=False)
    assert writes == []
    await bot._flush_collection_stats()
    assert sorted(writes) == ['replay.json', 'stats.json']
    assert bot.BotStats(tmp_path / 'stats.json')._data['totals']['collected'] == 9
    assert len(bot.ReplayBuffer(tmp_path / 'replay.json')._items) == 9
    await stats.record_skipped('filtered', 'A')     # по умолчанию — сразу, как раньше
    assert writes.count('stats.json') == 2


def test_replay_ids_keep_order_and_duplicates():
    buffer = bot.ReplayBuffer.__new__(bot.ReplayBuffer)
    buffer._items, buffer.max_items, buffer._lock = [], 50, bot.threading.RLock()
    buffer.path = bot.Path('/nonexistent/never-written.json')
    items = [{'title': 'a', 'link': 'https://x/1'}, {'title': 'b', 'link': 'https://x/2'},
             {'title': 'a', 'link': 'https://x/1'}]
    ids = buffer.capture_many(items, save=False)
    assert len(ids) == 3 and ids[0] == ids[2] != ids[1]
    assert len(buffer._items) == 2


def test_collector_and_shutdown_flush_the_stats():
    import inspect
    collect = inspect.getsource(bot.collect_all_news)
    assert 'await _flush_collection_stats()' in collect
    assert 'source_yield.flush' in collect
    shutdown = inspect.getsource(bot._post_shutdown)
    assert 'await _flush_collection_stats()' in shutdown and 'source_yield.flush()' in shutdown
    assert "record_collected(name, len(unique_items), save=False)" in collect


def test_story_memory_keeps_every_window_that_is_read():
    import inspect
    import re
    source = inspect.getsource(bot)
    windows = [int(n) for n in re.findall(r'story_history\._items\[-(\d+):\]', source)]
    assert windows and max(windows) <= bot.PublishedStoryStore.MAX_ITEMS
    assert bot.POSTS_EXPORT_MAX <= bot.PublishedStoryStore.MAX_ITEMS
    assert bot.PublishedStoryStore.MAX_ITEMS <= 500


# ------------------------------------- кеш AniList

def _anilist(tmp_path, monkeypatch, answers=None):
    client = bot.AniListClient(tmp_path / 'anilist.json')
    monkeypatch.setattr(client, '_query_api', lambda q, manga=False: (answers or {}).get(q))
    return client


def test_anilist_cache_is_capped(tmp_path, monkeypatch):
    client = _anilist(tmp_path, monkeypatch)
    monkeypatch.setattr(client, 'MAX_ENTRIES', 10)
    for i in range(25):
        client.lookup(f'title {i}')
    assert len(client._cache) == 10
    assert 'title 24' in client._cache and 'title 0' not in client._cache


def test_anilist_expired_entries_go_first(tmp_path, monkeypatch):
    client = _anilist(tmp_path, monkeypatch)
    monkeypatch.setattr(client, 'MAX_ENTRIES', 3)
    client._cache['old fresh'] = {'found': False, 'checked_at': bot.datetime.now().isoformat()}
    client._cache['expired'] = {'found': False, 'checked_at': '2001-01-01T00:00:00'}
    client.lookup('aa title')
    client.lookup('bb title')
    assert 'expired' not in client._cache and 'old fresh' in client._cache


def test_anilist_writes_at_most_once_a_minute(tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(bot, '_atomic_write_json', lambda path, data, **kw: writes.append(len(data)))
    client = _anilist(tmp_path, monkeypatch)
    for i in range(20):
        client.lookup(f'title {i}')
    assert len(writes) == 1                 # первая запись, дальше — копится
    client.flush()
    assert writes[-1] == 20 and len(writes) == 2
    client.flush()
    assert len(writes) == 2


def test_anilist_concurrent_lookups_do_not_break_the_save(tmp_path, monkeypatch):
    import threading
    client = _anilist(tmp_path, monkeypatch)
    monkeypatch.setattr(client, 'SAVE_EVERY_SEC', 0)
    errors = []

    def worker(n):
        try:
            for i in range(200):
                client.lookup(f'w{n} {i}')
        except Exception as e:                       # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    client.flush()
    import json
    assert len(json.loads((tmp_path / 'anilist.json').read_text(encoding='utf-8'))) == 800


def test_shutdown_flushes_anilist():
    import inspect
    assert 'anilist.flush()' in inspect.getsource(bot._post_shutdown)


def test_yt_dlp_is_not_imported_at_startup():
    import os
    import subprocess
    import sys
    code = 'import sys, anime_news_bot as b; print("yt_dlp" in sys.modules, b.YT_DLP_AVAILABLE)'
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                         cwd=str(bot.Path(bot.__file__).parent), timeout=120,
                         env=dict(os.environ, BOT_TOKEN='1:x')).stdout.strip().splitlines()[-1]
    assert out == 'False True'


@pytest.mark.asyncio
async def test_image_fingerprint_runs_off_the_event_loop(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(image_dedup=True))
    monkeypatch.setattr(bot, 'image_hashes', bot.ImageHashes(tmp_path / 'h.json'))
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: b'jpeg')
    main = threading.get_ident()
    seen = {}

    def fingerprint(data):
        seen['thread'] = threading.get_ident()
        return 'd:' + '0' * 16
    monkeypatch.setattr(bot, '_image_fingerprint', fingerprint)
    await bot._image_duplicate({'title': 'T', 'images': ['u']})
    assert seen['thread'] != main
