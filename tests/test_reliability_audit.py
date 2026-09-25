"""Аудит надёжности: десять воспроизведённых сбоев публикации и хранилищ.

Каждый тест падает на коде до исправления. Номера в заголовках разделов —
номера находок аудита.
"""
import asyncio
import copy
import json
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, TimedOut
from telegram.ext import ApplicationHandlerStop

import anime_news_bot as bot


def _news(title='Some off-topic celebrity gossip piece', link='https://example.com/x'):
    return {'title': title, 'link': link, 'source': 'Test',
            'images': ['https://img.test/a.jpg'], 'summary': ''}


def _cycle_stubs(monkeypatch, tmp_path, items, *, thread_mode=True):
    ledger = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, 'sent_links', ledger)
    monkeypatch.setattr(bot, 'settings', NS(thread_mode=thread_mode, quiet_mode=True,
                                            require_image=False, post_max_age_hours=72))
    monkeypatch.setattr(bot, 'collect_all_news',
                        AsyncMock(side_effect=lambda: ([dict(n) for n in items], ['Test: 1'], [])))
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_editorial_allowed', lambda _n: True)
    monkeypatch.setattr(bot, '_evaluate_adaptive_publishing', lambda *_a, **_k: None)
    monkeypatch.setattr(bot, 'cleanup_video_dir', lambda: None)
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    monkeypatch.setattr(bot, '_auto_disabled_pending', [])
    monkeypatch.setattr(bot, '_maybe_send_daily_summary', AsyncMock())
    monkeypatch.setattr(bot, '_check_silence', AsyncMock())
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, '_llm_prefetch_for_cycle', AsyncMock())
    monkeypatch.setattr(bot, 'PAUSE_BETWEEN_SENDS', 0)
    monkeypatch.setattr(bot, 'stats', NS(record_skipped=AsyncMock(), record_failed_send=AsyncMock()))
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: False)
    monkeypatch.setattr(bot, '_release_publish_reservations', lambda _n: None)
    monkeypatch.setattr(bot, '_quiet_seen_skips', {})
    return ledger


# ---------- 1. Отсеянная ссылка возвращалась каждый цикл ----------

@pytest.mark.asyncio
async def test_rejected_link_is_judged_and_reported_once(tmp_path, monkeypatch):
    """Модель отсеяла новость: второй раз её не судим и админу не пишем."""
    _cycle_stubs(monkeypatch, tmp_path, [_news()])
    prepare = AsyncMock(return_value='skipped_filter')
    monkeypatch.setattr(bot, '_prepare_news_for_send', prepare)
    ctx = NS(bot=MagicMock())
    for _ in range(4):                       # четыре цикла, два часа
        await bot._check_news_cycle(ctx)
    assert prepare.await_count == 1
    assert bot.notify_admin.await_count == 1


@pytest.mark.asyncio
async def test_rejected_link_is_not_queued_again_in_channel_mode(tmp_path, monkeypatch):
    ledger = _cycle_stubs(monkeypatch, tmp_path, [_news()], thread_mode=False)
    await ledger.reject('https://example.com/x', _news()['title'], 'skipped_filter')
    queue = bot.PostQueue(tmp_path / 'queue.json')
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, '_publish_one_from_queue', AsyncMock(return_value=(None, None)))
    await bot._check_news_cycle(NS(bot=MagicMock()))
    assert await queue.peek_size() == 0


@pytest.mark.asyncio
async def test_news_command_skips_rejected_links(tmp_path, monkeypatch):
    """/news — тот же фильтр: отсеянное не присылается админу снова."""
    ledger = _cycle_stubs(monkeypatch, tmp_path, [_news()])
    await ledger.reject('https://example.com/x', _news()['title'], 'skipped_filter')
    sender = AsyncMock(return_value='sent')
    monkeypatch.setattr(bot, 'send_news', sender)
    progress = MagicMock(edit_text=AsyncMock())
    upd = MagicMock()
    upd.message.reply_text = AsyncMock(return_value=progress)
    await bot.news_command.__wrapped__(upd, NS(bot=MagicMock()))
    assert sender.await_count == 0
    assert 'Новых новостей нет' in progress.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_quiet_mode_reports_a_repeated_skip_only_once(tmp_path, monkeypatch):
    """Дубль по похожему заголовку в историю не пишется и приходит каждый цикл.

    Письмо «0 отправлено, 1 отсеяно» — только в первый раз; новый отсев —
    снова повод написать.
    """
    items = [_news('Same story again from another source', 'https://a.test/1')]
    _cycle_stubs(monkeypatch, tmp_path, items)
    monkeypatch.setattr(bot, 'send_news_to_thread', AsyncMock(return_value='skipped_dup'))
    ctx = NS(bot=MagicMock())
    for _ in range(3):
        await bot._check_news_cycle(ctx)
    assert bot.notify_admin.await_count == 1
    items.append(_news('A completely different fresh story', 'https://a.test/2'))
    await bot._check_news_cycle(ctx)
    assert bot.notify_admin.await_count == 2


@pytest.mark.asyncio
async def test_rejection_lasts_as_long_as_the_freshness_window(tmp_path, monkeypatch):
    """Отказ жил 24 ч, а новость в источнике — 72 ч: модель судила её заново."""
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    ledger = bot.SentLinksStore(tmp_path / 'links.json')
    link, title = 'https://example.com/x', 'Off-topic piece nobody wants'
    await ledger.reject(link, title, 'skipped_filter')
    norm = bot.normalize_url(link)
    ledger._rejected[norm]['at'] = time.time() - 30 * 3600      # 30 ч: сутки прошли
    assert ledger.is_rejected(link)
    assert await ledger.claim(link, title) is False
    ledger._rejected[norm]['at'] = time.time() - 73 * 3600      # окно свежести вышло
    assert not ledger.is_rejected(link)
    assert await ledger.claim(link, title) is True


# ---------- 2. Рестарт во время подготовки терял пост ----------

@pytest.mark.asyncio
async def test_restart_during_preparation_still_publishes_the_post(tmp_path, monkeypatch):
    news = {'title': 'Studio announces second season of a show', 'link': 'https://example.com/a',
            'source': 'Test', 'images': ['https://img.test/1.jpg']}
    monkeypatch.setattr(bot, 'settings', NS(require_image=False))
    ledger = bot.SentLinksStore(tmp_path / 'links.json')
    queue = bot.PostQueue(tmp_path / 'queue.json')
    await queue.push_many([news])
    popped = await queue.pop_next()
    assert await ledger.claim(popped['link'], popped['title'], check_similar=True)
    # процесс убит, пока модель готовила текст — до Telegram не дошло

    ledger2 = bot.SentLinksStore(tmp_path / 'links.json')
    queue2 = bot.PostQueue(tmp_path / 'queue.json')
    monkeypatch.setattr(bot, 'sent_links', ledger2)
    monkeypatch.setattr(bot, 'post_queue', queue2)
    monkeypatch.setattr(bot, 'stats', NS(record_skipped=AsyncMock(), record_failed_send=AsyncMock(),
                                         record_published=AsyncMock()))
    for name in ('analytics_store', 'story_history', 'experiments', 'source_yield', 'story_registry'):
        monkeypatch.setattr(bot, name, None)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_llm_prefetch_queue_head', AsyncMock())
    monkeypatch.setattr(bot, '_maybe_mirror_canary', AsyncMock())
    monkeypatch.setattr(bot, '_mark_published', lambda: None)
    monkeypatch.setattr(bot, '_commit_image_fingerprint', lambda _n: None)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_send_channel_post', sender)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    result, _ = await bot._publish_one_from_queue(NS())
    assert result == 'sent'
    assert sender.await_count == 1
    assert await queue2.peek_size() == 0


@pytest.mark.asyncio
async def test_restart_frees_claimed_but_keeps_sending_as_uncertain(tmp_path):
    path = tmp_path / 'links.json'
    ledger = bot.SentLinksStore(path)
    assert await ledger.claim('https://a.test/prepared', 'Prepared but never sent headline')
    assert await ledger.claim('https://a.test/sending', 'Telegram call already started')
    assert await ledger.mark_sending('https://a.test/sending')

    again = bot.SentLinksStore(path)
    assert 'https://a.test/prepared' not in again
    assert not again.has_title('Prepared but never sent headline')
    assert 'https://a.test/sending' in again and again.uncertain_count() == 1
    on_disk = json.loads(path.read_text(encoding='utf-8'))['reservations']
    assert list(on_disk) == [bot.normalize_url('https://a.test/sending')]
    assert await again.claim('https://a.test/prepared', 'Prepared but never sent headline')
    assert not await again.claim('https://a.test/sending', 'Telegram call already started')


# ---------- 3. Ledger менялся из потоков без замка ----------

def _stale_ledger(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    now = time.time()
    url = bot.normalize_url('https://a.test/stale')
    title = bot.normalize_title('Stale reservation left by a crashed task')
    store._add_unlocked(url, title, save=False)
    store._recent_titles.append((now, title, bot._title_tokens(title)))
    store._reservations[url] = {'title': title, 'state': 'claimed',
                                'at': now - bot.DEDUP_RESERVATION_TTL_SEC - 60}
    unsure = bot.normalize_url('https://a.test/unsure')
    store._add_unlocked(unsure, '', save=False)
    store._reservations[unsure] = {'title': '', 'state': 'uncertain',
                                   'at': now - bot.DEDUP_UNCERTAIN_TTL_SEC - 60}
    store._rejected[bot.normalize_url('https://b.test/old')] = {
        'title': 'old', 'at': 0, 'reason': 'skipped_filter'}
    return store


def _ledger_state(store):
    return copy.deepcopy((store._urls, store._url_set, store._titles, store._title_set,
                          list(store._recent_titles), store._reservations, store._rejected))


def test_ledger_reads_honour_expiry_without_changing_anything(tmp_path):
    """Чтение из потока сборщика не должно чистить словари: этим оно и ломало запись."""
    store = _stale_ledger(tmp_path)
    before = _ledger_state(store)
    assert 'https://a.test/stale' not in store
    assert 'https://a.test/unsure' not in store
    assert not store.has_title('Stale reservation left by a crashed task')
    assert not store.has_similar_title('Stale reservation left by a crashed task')
    assert store.uncertain_count() == 0
    assert not store.is_rejected('https://b.test/old')
    assert _ledger_state(store) == before


def _blocks_while_locked(store, action) -> bool:
    """True, если action ждёт, пока другой поток держит замок ledger."""
    done = threading.Event()
    worker = threading.Thread(target=lambda: (action(), done.set()), daemon=True)
    with store._state_lock:
        worker.start()
        waited = not done.wait(0.2)
    finished = done.wait(5)
    worker.join(5)
    return waited and finished


@pytest.mark.parametrize('read', [
    lambda s: 'https://a.test/1' in s,
    lambda s: s.has_title('Some title'),
    lambda s: s.has_similar_title('Some reasonably long title'),
    lambda s: s.uncertain_count(),
    lambda s: s.is_rejected('https://a.test/1'),
], ids=['contains', 'has_title', 'has_similar_title', 'uncertain_count', 'is_rejected'])
def test_ledger_readers_take_the_lock(tmp_path, read):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    assert _blocks_while_locked(store, lambda: read(store))


def test_ledger_snapshot_and_claim_take_the_lock(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    assert _blocks_while_locked(store, store._save)
    assert asyncio.run(store.claim('https://a.test/new', 'Brand new headline'))
    # Повторный claim выходит до записи на диск, но чистит просроченное —
    # замок обязан держать сама транзакция, а не только _save().
    assert _blocks_while_locked(
        store, lambda: asyncio.run(store.claim('https://a.test/new', 'Brand new headline')))


@pytest.mark.asyncio
async def test_unexpected_save_error_is_a_failed_write_not_a_crash(tmp_path, monkeypatch):
    """RuntimeError из записи вылетал из commit() уже после отправки → повтор поста."""
    store = bot.SentLinksStore(tmp_path / 'links.json')
    assert await store.claim('https://a.test/1', 'Headline one')

    def racing_write(*_a, **_k):
        raise RuntimeError('dictionary changed size during iteration')

    monkeypatch.setattr(bot, '_atomic_write_json', racing_write)
    assert store._save() is False
    assert await store.commit('https://a.test/1', 'Headline one') is False


def test_ledger_survives_reader_threads_during_saves(tmp_path):
    """Сценарий аудита: поток читает, loop пишет, записи отказов истекают."""
    store = bot.SentLinksStore(tmp_path / 'links.json')
    for i in range(2000):
        store._urls.append(f'https://example.com/{i}')
    store._url_set = set(store._urls)

    def refill():
        base = time.time() - bot.SentLinksStore._reject_ttl_sec()
        for i in range(500):
            store._rejected[f'https://rej.test/{time.time_ns()}/{i}'] = {
                'title': f'rej {i}', 'at': base + i * 0.001, 'reason': 'x'}

    refill()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            'https://example.com/new' in store      # noqa: B015
            store.uncertain_count()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    failures = 0
    deadline = time.time() + 0.6
    try:
        while time.time() < deadline:
            if not store._save():
                failures += 1
            if len(store._rejected) < 50:
                refill()
    finally:
        stop.set()
        thread.join(2)
    assert failures == 0


# ---------- 4. Реестр историй переписывался на каждый кластер ----------

def test_story_registry_writes_once_per_flush_in_compact_json(tmp_path, monkeypatch):
    path = tmp_path / 'stories.json'
    registry = bot.StoryRegistry(path)
    writes = []
    real_write = bot._atomic_write_json

    def counting_write(target, data, **kwargs):
        writes.append(kwargs)
        return real_write(target, data, **kwargs)

    monkeypatch.setattr(bot, '_atomic_write_json', counting_write)
    titles = ['Frieren season 2 trailer revealed', 'One Piece film gets release date',
              'Chainsaw Man manga ends in March', 'Kaiju No. 8 casts new villain',
              'Studio Trigger announces original movie']
    for i, title in enumerate(titles):
        registry.observe({'title': title}, ['S'], [f'https://s.test/{i}'])
    assert writes == [] and not path.exists()
    registry.flush()
    registry.flush()                          # нечего писать — второй записи нет
    assert len(writes) == 1 and writes[0].get('indent') is None
    assert '\n' not in path.read_text(encoding='utf-8')
    assert len(bot.StoryRegistry(path)._items) == len(registry._items) == len(titles)


@pytest.mark.asyncio
async def test_collection_flushes_story_registry_off_the_event_loop(tmp_path, monkeypatch):
    registry = bot.StoryRegistry(tmp_path / 'stories.json')
    flushed_in = []
    real_flush = registry.flush

    def spy():
        flushed_in.append(threading.current_thread())
        real_flush()

    monkeypatch.setattr(registry, 'flush', spy)
    monkeypatch.setattr(bot, 'story_registry', registry)
    flags = dict(bot.FEATURE_FLAGS)
    flags.update(story_clustering=True, story_registry=True, source_intelligence=False,
                 active_verification=False, replay=False, editorial_rules=False,
                 editorial_learning=False, story_updates=False)
    monkeypatch.setattr(bot, 'FEATURE_FLAGS', flags)
    item = {'title': 'Frieren Season 2 Gets Official Trailer', 'link': 'https://a.test/1',
            'source': 'A', 'summary': '', 'images': []}
    monkeypatch.setattr(bot, 'SOURCES', [('A', lambda: [dict(item)])])
    monkeypatch.setattr(bot, 'settings', NS(is_source_enabled=lambda _n: True, require_image=False))
    for name in ('source_health', 'error_fingerprints', 'replay_buffer', 'moderation_feedback',
                 'story_history', 'source_intelligence', 'source_yield'):
        monkeypatch.setattr(bot, name, None)

    class Stats:
        async def record_collected(self, *a, **k): pass
        async def record_skipped(self, *a, **k): pass
        async def record_source_error(self, *a, **k): pass
        def get_by_source(self): return {}

    monkeypatch.setattr(bot, 'stats', Stats())
    await bot.collect_all_news()
    assert flushed_in and all(t is not threading.main_thread() for t in flushed_in)
    assert (tmp_path / 'stories.json').exists()


@pytest.mark.asyncio
async def test_graceful_shutdown_flushes_story_registry(tmp_path, monkeypatch):
    registry = bot.StoryRegistry(tmp_path / 'stories.json')
    registry.observe({'title': 'Story observed right before shutdown'}, ['S'], ['https://s.test/1'])
    monkeypatch.setattr(bot, 'story_registry', registry)
    monkeypatch.setattr(bot, '_mark_lifecycle_exit', lambda *a, **k: None)
    monkeypatch.setattr(bot, '_stop_health_server', lambda: None)
    monkeypatch.setattr(bot, '_release_instance_lock', lambda: None)
    monkeypatch.setattr(bot, 'cleanup_video_dir', lambda **k: None)
    for name in ('user_directory', 'settings', 'experiments', 'chat_moderation', 'post_queue',
                 'scheduled_posts', 'pending_posts', 'sent_links'):
        monkeypatch.setattr(bot, name, None)
    await bot._post_shutdown(MagicMock())
    assert len(bot.StoryRegistry(tmp_path / 'stories.json')._items) == 1


# ---------- 5. Автопостинг «и в ветку, и в канал» брал недельные новости ----------

@pytest.mark.asyncio
async def test_autopost_skips_week_old_post_and_takes_fresh_one(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'pending.json')
    old = store.add({'title': 'Old news from 6 days ago', 'link': 'https://e.com/old'})
    store._items[old]['ts'] = time.time() - 6 * 86400
    store.add({'title': 'Fresh news', 'link': 'https://e.com/fresh'})
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', NS(last_channel_post_at='', channel_interval_sec=3600,
                                            last_publish_at='', post_max_age_hours=72,
                                            save=lambda: None))
    for name in ('stats', 'source_yield', 'story_registry', 'story_history'):
        monkeypatch.setattr(bot, name, None)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: False)
    sent = []

    async def fake_send(delivery, news):
        sent.append(news['title'])
        return True

    monkeypatch.setattr(bot, '_prepare_and_send_channel_post', fake_send)
    assert await bot._autopost_one_from_thread(NS()) == 'sent'
    assert sent == ['Fresh news']


def test_autopost_offers_nothing_when_only_stale_posts_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    store = bot.PendingPosts(tmp_path / 'pending.json')
    key = store.add({'title': 'Old', 'link': 'https://e.com/old'})
    store._items[key]['ts'] = time.time() - 4 * 86400
    assert store.next_for_autopost() is None
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=120))   # предел — из настройки
    assert store.next_for_autopost()[0] == key


def test_autopost_ages_posts_by_the_source_date(tmp_path, monkeypatch):
    """Вчера попала в ветку, но в источнике вышла пять дней назад — в канал не идёт."""
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    store = bot.PendingPosts(tmp_path / 'pending.json')
    store.add({'title': 'Republished old piece', 'link': 'https://e.com/r',
               'published_parsed': time.gmtime(time.time() - 5 * 86400)})
    assert store.next_for_autopost() is None


def test_autopost_prefers_the_newest_by_source_date(tmp_path, monkeypatch):
    """Первый в ветке — самый старый по ветке, последний — самый старый по источнику."""
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    store = bot.PendingPosts(tmp_path / 'pending.json')
    now = time.time()
    store.add({'title': 'Published two hours ago', 'link': 'https://e.com/a',
               'published_parsed': time.gmtime(now - 7200)})
    fresher = store.add({'title': 'Published an hour ago', 'link': 'https://e.com/b',
                         'published_parsed': time.gmtime(now - 3600)})
    store.add({'title': 'Published three hours ago', 'link': 'https://e.com/c',
               'published_parsed': time.gmtime(now - 3 * 3600)})
    assert store.next_for_autopost()[0] == fresher


# ---------- 6. Кеш картинок из параллельных потоков ----------

class _OverlapDict(dict):
    """Словарь, который замечает одновременный доступ из двух потоков."""

    def __init__(self):
        super().__init__()
        self._guard = threading.Lock()
        self._inside = 0
        self.overlaps = 0

    def _guarded(self, method, *args):
        with self._guard:
            self._inside += 1
            if self._inside > 1:
                self.overlaps += 1
        try:
            time.sleep(0.0005)                     # окно, в котором и случалась гонка
            return method(self, *args)
        finally:
            with self._guard:
                self._inside -= 1

    def __contains__(self, key):
        return self._guarded(dict.__contains__, key)

    def __getitem__(self, key):
        return self._guarded(dict.__getitem__, key)

    def __setitem__(self, key, value):
        return self._guarded(dict.__setitem__, key, value)

    def pop(self, *args):
        return self._guarded(dict.pop, *args)

    def values(self):
        return self._guarded(dict.values)

    def __iter__(self):
        return self._guarded(dict.__iter__)


def test_parallel_image_downloads_share_the_cache_safely(monkeypatch):
    cache = _OverlapDict()
    monkeypatch.setattr(bot, '_image_bytes_cache', cache)
    monkeypatch.setattr(bot, '_download_image_bytes', lambda _url: b'x' * 1000)
    missing = []

    def worker(tid):
        for i in range(25):
            if bot._cached_image_bytes(f'https://img.test/{tid}/{i}.jpg') is None:
                missing.append((tid, i))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cache.overlaps == 0 and missing == []
    assert len(cache) <= bot.IMAGE_BYTES_CACHE_MAX


def test_image_cache_failure_is_just_a_missing_picture(monkeypatch):
    def racing(*_a, **_k):
        raise RuntimeError('dictionary changed size during iteration')

    monkeypatch.setattr(bot, '_image_bytes_cache', {})
    monkeypatch.setattr(bot, '_download_image_bytes', lambda _url: b'x')
    monkeypatch.setattr(bot, '_bounded_bytes_cache_put', racing)
    assert bot._cached_image_bytes('https://img.test/a.jpg') is None


@pytest.mark.asyncio
async def test_cycle_clears_image_cache_only_under_the_lock(tmp_path, monkeypatch):
    """Цикл чистит кеш, пока публикатор может качать картинки в потоках."""
    _cycle_stubs(monkeypatch, tmp_path, [])
    monkeypatch.setattr(bot, '_image_bytes_cache', {})
    holding, seen = threading.Event(), []

    def downloader_thread():
        with bot._image_bytes_cache_lock:
            bot._image_bytes_cache['https://img.test/busy.jpg'] = b'x'
            holding.set()
            time.sleep(0.3)
            seen.append('https://img.test/busy.jpg' in bot._image_bytes_cache)

    worker = threading.Thread(target=downloader_thread, daemon=True)
    worker.start()
    assert holding.wait(2)
    await bot._check_news_cycle(NS(bot=MagicMock()))
    worker.join(2)
    assert seen == [True]                          # под замком кеш никто не трогал
    assert bot._image_bytes_cache == {}


class _VanishingOldest(dict):
    """Первый обход отдаёт ключ, который уже вытеснил другой поток."""
    ghost = True

    def __iter__(self):
        if self.ghost:
            self.ghost = False
            return iter(['ghost', *dict.__iter__(self)])
        return dict.__iter__(self)


def test_eviction_tolerates_a_key_that_already_vanished():
    cache = _VanishingOldest(a=b'1' * 10, b=b'2' * 10)
    bot._bounded_bytes_cache_put(cache, 'c', b'3' * 10, max_items=2, max_bytes=10 ** 6)
    assert 'c' in cache and len(cache) <= 2


# ---------- 7. «Серии дня»: дубль после TimedOut и повтор по одному адресу ----------

CAL = [{'next_episode': 5, 'next_episode_at': '2026-09-24T12:00:00+03:00',
        'anime': {'name': 'Grand Blue', 'russian': 'Необъятный океан', 'kind': 'tv',
                  'status': 'ongoing', 'score': '8.1', 'episodes': 12}}]


@pytest.fixture
def digest(monkeypatch, tmp_path):
    from zoneinfo import ZoneInfo
    state = NS(settings=NS(episodes_digest=True, auto_enabled=True, thread_mode=False,
                           channel_autopost=True),
               fetch=MagicMock(return_value=CAL), calls=[], fail={})

    async def send_message(chat_id, text, **kw):
        state.calls.append(chat_id)
        error = state.fail.pop(chat_id, None)
        if error is not None:
            raise error
        return NS(message_id=len(state.calls))

    monkeypatch.setattr(bot, 'settings', state.settings)
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_FILE', tmp_path / 'digest.json')
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_TIME', '10:00')
    monkeypatch.setattr(bot, '_local_now', lambda: datetime(2026, 9, 24, 11, 0))
    monkeypatch.setattr(bot, '_admin_tz', lambda: ZoneInfo('Europe/Moscow'))
    monkeypatch.setattr(bot, '_fetch_episode_calendar', state.fetch)
    state.run = lambda: bot.episodes_digest_job(NS(bot=NS(send_message=send_message)))
    return state


@pytest.mark.asyncio
async def test_digest_timed_out_after_delivery_is_not_posted_again(digest):
    digest.fail[bot.CHANNEL_ID] = TimedOut()          # Telegram принял, ответ потерялся
    await digest.run()
    await digest.run()                                # следующий тик через 10 минут
    assert digest.calls == [bot.CHANNEL_ID]
    saved = json.loads(bot.EPISODES_DIGEST_FILE.read_text(encoding='utf-8'))
    assert saved['targets'][str(bot.CHANNEL_ID)]['status'] == 'uncertain'


@pytest.mark.asyncio
async def test_digest_both_mode_retries_only_the_failed_target(digest):
    digest.settings.thread_mode = True
    digest.fail[bot.CHANNEL_ID] = BadRequest('Chat not found')   # отказ: сообщения точно нет
    await digest.run()
    await digest.run()
    await digest.run()
    assert digest.calls == [bot.DISCUSSION_CHAT_ID, bot.CHANNEL_ID, bot.CHANNEL_ID]


@pytest.mark.asyncio
async def test_digest_without_targets_does_not_download_the_calendar(digest):
    digest.settings.channel_autopost = False
    for _ in range(3):
        await digest.run()
    assert digest.fetch.call_count == 0 and digest.calls == []


@pytest.mark.asyncio
async def test_digest_old_state_format_still_closes_the_day(digest):
    bot.EPISODES_DIGEST_FILE.write_text(
        json.dumps({'date': date(2026, 9, 24).isoformat(), 'messages': [5]}), encoding='utf-8')
    await digest.run()
    assert digest.fetch.call_count == 0 and digest.calls == []


# ---------- 8. Провал проверки ежедневного бэкапа был молчаливым ----------

@pytest.fixture
def backup(monkeypatch):
    state = NS(now=datetime(2026, 9, 24, 9, 0),
               settings=MagicMock(daily_backup=True, last_backup_date='', tz_offset=3))
    monkeypatch.setattr(bot, 'settings', state.settings)
    monkeypatch.setattr(bot, '_local_now', lambda: state.now)
    monkeypatch.setattr(bot, '_build_backup_archive', lambda: (b'zip-bytes', 'backup.zip'))
    monkeypatch.setattr(bot, '_verify_backup_archive',
                        lambda _d: {'ok': False, 'errors': ['bot_settings.json: битый JSON']})
    monkeypatch.setattr(bot, '_backup_restore_selftest', lambda _d: {'ok': True, 'errors': []})
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, '_backup_check_alerted_day', '')
    state.bot = MagicMock(send_document=AsyncMock())
    state.run = lambda: bot.daily_backup_job(NS(bot=state.bot))
    return state


@pytest.mark.asyncio
async def test_failed_backup_check_is_reported_once_a_day(backup):
    await backup.run()
    backup.now = datetime(2026, 9, 24, 10, 0)          # следующий часовой тик
    await backup.run()
    assert bot.notify_admin.await_count == 1
    text = bot.notify_admin.await_args.args[1]
    assert 'бэкап не отправлен' in text and 'битый JSON' in text
    assert backup.bot.send_document.await_count == 0
    backup.now = datetime(2026, 9, 25, 9, 0)           # назавтра — снова
    await backup.run()
    assert bot.notify_admin.await_count == 2


@pytest.mark.asyncio
async def test_failed_restore_test_is_reported(backup, monkeypatch):
    monkeypatch.setattr(bot, '_verify_backup_archive', lambda _d: {'ok': True, 'errors': []})
    monkeypatch.setattr(bot, '_backup_restore_selftest',
                        lambda _d: {'ok': False, 'errors': ['pending_posts.json не читается']})
    await backup.run()
    assert 'pending_posts.json не читается' in bot.notify_admin.await_args.args[1]


# ---------- 9. Отложка после неоднозначного автопостинга ----------

@pytest.mark.asyncio
async def test_schedule_refuses_post_whose_channel_delivery_is_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(tz_offset=3, extra_admins=[]))
    monkeypatch.setattr(bot, 'pending_posts', bot.PendingPosts(tmp_path / 'p.json'))
    monkeypatch.setattr(bot, 'scheduled_posts', bot.ScheduledPosts(tmp_path / 's.json'))
    key = bot.pending_posts.add({'title': 'Новость', 'link': 'https://e.com/1'})
    # Админ нажал 📅, а пока вводил время, автопостинг ушёл в канал без ответа.
    assert bot.pending_posts.mark_channel_uncertain(key)
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.effective_user = MagicMock(id=555, full_name='Dobe', username='dobe')
    upd.message = MagicMock(text='+2ч', message_thread_id=10138)
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock(bot=MagicMock(), user_data={'await_input': {
        'mode': 'schedule', 'key': key, 'chat_id': -100, 'thread_id': 10138}})
    with pytest.raises(ApplicationHandlerStop):
        await bot.awaiting_input_handler(upd, ctx)
    assert bot.scheduled_posts.all() == []
    assert bot.pending_posts.get(key) is not None
    assert 'await_input' not in ctx.user_data
    assert 'не подтвердил' in upd.message.reply_text.await_args.args[0]


# ---------- 10. Очередь писалась на диск каждый тик ----------

@pytest.mark.asyncio
async def test_queue_with_only_waiting_posts_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(require_image=False))
    queue = bot.PostQueue(tmp_path / 'queue.json')
    await queue.push_many([_news('Waits for the model', 'https://a.test/1'),
                           _news('Also waits for the model', 'https://a.test/2')])
    for item in queue._items:
        item['news']['_queue_retry_at'] = time.time() + 600
    saves = []
    real_save = queue._save
    monkeypatch.setattr(queue, '_save', lambda: (saves.append(1), real_save())[1])
    for _ in range(3):                                  # три тика публикатора
        assert await queue.pop_next() is None
    assert saves == []
    assert [i['news']['link'] for i in queue._items] == ['https://a.test/1', 'https://a.test/2']


@pytest.mark.asyncio
async def test_queue_still_saves_when_it_drops_a_post(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(require_image=False))
    queue = bot.PostQueue(tmp_path / 'queue.json')
    no_media = {'title': 'No media at all', 'link': 'https://a.test/3', 'source': 'Test'}
    await queue.push_many([no_media])
    monkeypatch.setattr(bot, 'settings', NS(require_image=True))
    saves = []
    real_save = queue._save
    monkeypatch.setattr(queue, '_save', lambda: (saves.append(1), real_save())[1])
    assert await queue.pop_next() is None
    assert saves == [1] and queue._items == []
