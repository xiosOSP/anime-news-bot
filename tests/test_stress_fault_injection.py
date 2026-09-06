"""Stress/fault-injection regressions for the hardened monolith.

These tests intentionally exercise races, restart windows and malformed state that
normal happy-path unit tests do not cover.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import anime_news_bot as bot


@pytest.fixture
def fake_settings(monkeypatch):
    s = SimpleNamespace(require_image=False, image_dedup=True, dedup_final_text=True)
    monkeypatch.setattr(bot, 'settings', s)
    return s


@pytest.mark.asyncio
async def test_fuzzy_claim_is_atomic(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    a = ('https://a.example/1', 'Mamonotsukai no Musume Gets TV Anime')
    b = ('https://b.example/2', 'Mamono Tsukai no Musume Anime: Cast Announced')

    # Start together: only one similar title may own a reservation.
    results = await asyncio.gather(
        store.claim(*a, check_similar=True),
        store.claim(*b, check_similar=True),
    )
    assert sum(bool(x) for x in results) == 1


@pytest.mark.asyncio
async def test_queue_restores_inflight_after_restart(tmp_path, fake_settings):
    path = tmp_path / 'queue.json'
    q1 = bot.PostQueue(path)
    news = {'link': 'https://a.example/1', 'title': 'A', 'images': []}
    await q1.push_many([news])
    claimed = await q1.pop_next()
    assert claimed['link'] == news['link']

    # Simulate hard process death before ack/requeue.
    q2 = bot.PostQueue(path)
    assert await q2.peek_size() == 1
    restored = await q2.pop_next()
    assert restored['link'] == news['link']


@pytest.mark.asyncio
async def test_queue_priority_overflow_keeps_top_scores(tmp_path, fake_settings, monkeypatch):
    monkeypatch.setattr(bot, 'QUEUE_MAX_SIZE', 3)
    q = bot.PostQueue(tmp_path / 'q.json')
    items = [
        {'link': f'https://e/{i}', 'title': str(i), 'images': [], '_priority_score': score}
        for i, score in enumerate((100, 90, 80, 2, 1))
    ]
    await q.push_many(items)
    assert await q.peek_size() == 3
    got = []
    for _ in range(3):
        n = await q.pop_next()
        got.append(n['_priority_score'])
        await q.ack_done(n)
    assert got == [100, 90, 80]


@pytest.mark.asyncio
async def test_queue_does_not_issue_two_posts_to_different_tasks(tmp_path, fake_settings):
    q = bot.PostQueue(tmp_path / 'q.json')
    await q.push_many([
        {'link': 'https://e/1', 'title': '1', 'images': []},
        {'link': 'https://e/2', 'title': '2', 'images': []},
    ])
    first_ready = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        item = await q.pop_next()
        first_ready.set()
        await release.wait()
        await q.ack_done(item)
        return item

    task = asyncio.create_task(owner())
    await first_ready.wait()
    assert await q.pop_next() is None
    release.set()
    await task
    assert (await q.pop_next())['link'] == 'https://e/2'


@pytest.mark.asyncio
async def test_image_dedup_blocks_concurrent_inflight(tmp_path, fake_settings, monkeypatch):
    hashes = bot.ImageHashes(tmp_path / 'images.json')
    monkeypatch.setattr(bot, 'image_hashes', hashes)
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda _u: b'same-image')
    monkeypatch.setattr(bot, '_image_fingerprint', lambda _d: 'm:abc')

    reserved = asyncio.Event()
    release = asyncio.Event()
    first = {'title': 'First', 'images': ['https://img/1']}
    second = {'title': 'Second', 'images': ['https://img/2']}

    async def hold_first():
        assert await bot._image_duplicate(first) is None
        reserved.set()
        await release.wait()
        bot._release_publish_reservations(first)

    t = asyncio.create_task(hold_first())
    await reserved.wait()
    assert await bot._image_duplicate(second) == 'First'
    release.set()
    await t


@pytest.mark.asyncio
async def test_final_text_reservation_blocks_concurrent_duplicate(tmp_path):
    store = bot.PublishedTexts(tmp_path / 'texts.json')
    text1 = 'Новый трейлер Solo Leveling показал премьеру второго сезона'
    text2 = 'Новый трейлер Solo Leveling показал премьеру второго сезона'
    ready = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        assert store.reserve(text1) is None
        ready.set()
        await release.wait()
        store.release(text1)

    t = asyncio.create_task(owner())
    await ready.wait()
    assert store.reserve(text2) is not None
    release.set()
    await t


@pytest.mark.asyncio
async def test_subject_reservation_counts_inflight(tmp_path):
    store = bot.RecentSubjects(tmp_path / 'subjects.json')
    ready = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        store.reserve('Solo Leveling', 'трейлер', 'A')
        ready.set()
        await release.wait()
        store.release('Solo Leveling', 'трейлер')

    t = asyncio.create_task(owner())
    await ready.wait()
    assert store.seen_same_news('Solo Leveling', 'трейлер') is True
    assert store.count_today('Solo Leveling') == 1
    release.set()
    await t


@pytest.mark.asyncio
async def test_llm_quota_rechecked_inside_lock(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.llm_day = bot._local_now().strftime('%Y-%m-%d')
    settings.llm_calls_today = 0
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 1)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_last_call', 0.0)
    calls = 0

    def request(_messages, _max_tokens):
        nonlocal calls
        calls += 1
        return 'ok'

    monkeypatch.setattr(bot, '_llm_request', request)
    results = await asyncio.gather(*(bot._llm_call([], 10) for _ in range(10)))
    assert calls == 1
    assert sum(x == 'ok' for x in results) == 1
    assert settings.llm_calls_today == 1


@pytest.mark.asyncio
async def test_telegram_retryafter_can_repeat_before_success(monkeypatch):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise bot.RetryAfter(retry_after=0)
        return 'ok'

    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, 'sleep', sleep)
    assert await bot._tg_call_flood_safe(flaky) == 'ok'
    assert calls == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_telegram_huge_retryafter_does_not_freeze_job(monkeypatch):
    async def flaky():
        raise bot.RetryAfter(retry_after=bot.TG_FLOOD_MAX_WAIT_SEC + 100)

    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, 'sleep', sleep)
    with pytest.raises(bot.RetryAfter):
        await bot._tg_call_flood_safe(flaky)
    sleep.assert_not_awaited()


def test_stats_survive_semantically_corrupt_json(tmp_path):
    path = tmp_path / 'stats.json'
    path.write_text(json.dumps({
        'totals': ['bad'],
        'by_source': {'A': 'bad', 'B': {'collected': '-4', 'published': 2, 'errors': None}},
        'events': {'not': 'a-list'},
        'bot_started_at': 123,
    }))
    stats = bot.BotStats(path)
    totals = stats.get_totals()
    assert totals['published'] == 0
    assert stats.get_by_source()['B']['published'] == 2
    assert stats.get_by_source()['B']['collected'] == 0


@pytest.mark.asyncio
async def test_batched_skips_keep_event_count_semantics(tmp_path):
    stats = bot.BotStats(tmp_path / 'stats.json')
    await stats.record_skipped('duplicate', 'A', 50)
    assert stats.get_totals()['skipped_duplicate'] == 50
    from datetime import datetime, timedelta
    assert stats.count_events_since(datetime.now() - timedelta(minutes=1), 'skipped_duplicate') == 50


@pytest.mark.asyncio
async def test_channel_publications_are_serialized(monkeypatch):
    active = 0
    peak = 0

    async def fake_send(_bot, _news, _target, _video):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return True

    monkeypatch.setattr(bot, '_send_post', fake_send)
    await asyncio.gather(*(bot._send_channel_post(object(), {'title': str(i)}) for i in range(8)))
    assert peak == 1


@pytest.mark.asyncio
async def test_history_clear_removes_all_dedup_state(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    assert await store.claim('https://e/1', 'Some unique title')
    await store.reject('https://e/1', 'Some unique title', 'filter')
    assert store._rejected
    assert await store.claim('https://e/2', 'Another unique title')
    await store.clear()
    assert not store._urls
    assert not store._titles
    assert not store._recent_titles
    assert not store._reservations
    assert not store._rejected


def test_atomic_counter_helpers_only_save_once(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    saves = 0
    real_save = settings.save

    def counted():
        nonlocal saves
        saves += 1
        real_save()

    monkeypatch.setattr(settings, 'save', counted)
    settings.increment_llm_call('2099-01-01')
    assert saves == 1
    saves = 0
    assert settings.add_deepl_chars('2099-01', 123) == (0, 123)
    assert saves == 1

@pytest.mark.asyncio
async def test_channel_ledger_restart_keeps_ambiguous_send_blocked(tmp_path):
    path = tmp_path / 'links.json'
    first = bot.SentLinksStore(path)
    link = 'https://example.test/ambiguous'
    assert await first.claim(link, 'Ambiguous delivery title')
    assert await first.mark_sending(link)

    # Simulate hard process death after entering Telegram API, before commit().
    second = bot.SentLinksStore(path)
    assert link in second
    assert second.uncertain_count() == 1
    meta = second._reservations[bot.normalize_url(link)]
    assert meta['state'] == 'uncertain'


@pytest.mark.asyncio
async def test_scheduled_restart_does_not_auto_repeat_ambiguous_send(tmp_path):
    path = tmp_path / 'scheduled.json'
    sp = bot.ScheduledPosts(path)
    key = sp.add({'title': 'Maybe sent', 'link': 'https://e/1'},
                 bot.datetime.now(bot.timezone.utc) - bot.timedelta(minutes=1))
    assert sp.mark_sending(key)

    recovered = bot.ScheduledPosts(path)
    assert recovered.meta(key)['state'] == 'uncertain'
    assert recovered.uncertain_count() == 1
    assert recovered.due() == []
    # Explicit admin retry is still possible after checking the channel.
    assert recovered.mark_sending(key, force=True) is True


@pytest.mark.asyncio
async def test_private_news_view_does_not_consume_publish_history(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_post', AsyncMock(return_value=True))
    commit_media = patch.object(bot, '_commit_image_fingerprint')
    mark_pub = patch.object(bot, '_mark_published')
    news = {'title': 'Private check only', 'link': 'https://e/private', 'source': 'A'}
    with commit_media as cm, mark_pub as mp:
        result = await bot.send_news(object(), news, chat_id=123, track_history=False)
    assert result == 'sent'
    assert news['link'] not in store
    assert not store._reservations
    cm.assert_not_called()
    mp.assert_not_called()


@pytest.mark.asyncio
async def test_health_probe_refreshes_stale_readiness(monkeypatch):
    monkeypatch.setattr(bot, '_check_channel_access', AsyncMock(return_value=(True, 'ok')))
    monkeypatch.setattr(bot, '_storage_ready', lambda: True)
    bot._runtime_health.update(telegram_ok=False, storage_ok=False, last_error='old')
    await bot.health_probe_job(SimpleNamespace(bot=object()))
    assert bot._runtime_health['telegram_ok'] is True
    assert bot._runtime_health['storage_ok'] is True
    assert bot._runtime_health['last_error'] == ''

@pytest.mark.asyncio
async def test_cancel_during_channel_send_keeps_ledger_ambiguous(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_channel_post', AsyncMock(side_effect=asyncio.CancelledError()))
    commit_media = patch.object(bot, '_commit_image_fingerprint')
    news = {'title': 'Cancellation boundary', 'link': 'https://e/cancel', 'source': 'A'}
    with commit_media as cm, pytest.raises(asyncio.CancelledError):
        await bot.send_news(object(), news)
    assert news['link'] in store
    assert store.uncertain_count() == 1
    cm.assert_called_once_with(news)


@pytest.mark.asyncio
async def test_cancel_during_scheduled_send_becomes_uncertain(tmp_path, monkeypatch):
    store = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = store.add({'title': 'Scheduled cancel', 'link': 'https://e/sched-cancel'},
                    bot.datetime.now(bot.timezone.utc) - bot.timedelta(minutes=1))
    monkeypatch.setattr(bot, 'scheduled_posts', store)
    monkeypatch.setattr(bot, '_send_channel_post', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await bot.publish_scheduled(SimpleNamespace(bot=object()))
    assert store.meta(key)['state'] == 'uncertain'
    assert store.due() == []


def test_instance_lock_acquire_release(tmp_path, monkeypatch):
    if bot.fcntl is None:
        pytest.skip('POSIX flock unavailable')
    bot._release_instance_lock()
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    bot._acquire_instance_lock()
    try:
        assert bot._instance_lock_handle is not None
        assert (tmp_path / '.anime_news_bot.lock').read_text().strip().isdigit()
    finally:
        bot._release_instance_lock()
    assert bot._instance_lock_handle is None

@pytest.mark.asyncio
async def test_source_wall_timeout_prevents_cycle_hang(monkeypatch):
    import time as _time

    def slow_collector():
        _time.sleep(0.05)
        return []

    class _Settings:
        require_image = False
        def is_source_enabled(self, _name):
            return True

    class _Stats:
        async def record_collected(self, *_a, **_k): pass
        async def record_skipped(self, *_a, **_k): pass
        async def record_source_error(self, *_a, **_k): pass

    monkeypatch.setattr(bot, 'SOURCES', [('slow', slow_collector)])
    monkeypatch.setattr(bot, 'settings', _Settings())
    monkeypatch.setattr(bot, 'stats', _Stats())
    monkeypatch.setattr(bot, 'source_health', None)
    monkeypatch.setattr(bot, 'SOURCE_FETCH_WALL_TIMEOUT', 0.01)
    _items, _stats, errors = await bot.collect_all_news()
    assert errors and errors[0].startswith('slow:')


def test_jobs_are_configured_not_to_overlap():
    assert bot.JOB_KWARGS['max_instances'] == 1
    assert bot.JOB_KWARGS['coalesce'] is True


def test_scheduled_overflow_never_discards_earliest_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.ScheduledPosts, 'MAX_ITEMS', 2)
    sp = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    now = bot.datetime.now(bot.timezone.utc)
    first = sp.add({'title': 'first', 'link': 'https://e/first'}, now + bot.timedelta(minutes=1))
    second = sp.add({'title': 'second', 'link': 'https://e/second'}, now + bot.timedelta(minutes=2))
    with pytest.raises(OverflowError):
        sp.add({'title': 'third', 'link': 'https://e/third'}, now + bot.timedelta(minutes=3))
    assert sp.get(first)['title'] == 'first'
    assert sp.get(second)['title'] == 'second'
    assert len(sp.all()) == 2

@pytest.mark.asyncio
async def test_ledger_fails_closed_when_claim_cannot_be_persisted(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, '_atomic_write_json', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk full')))
    assert await store.claim('https://e/disk', 'Disk full claim') is False
    assert 'https://e/disk' not in store
    assert not store._reservations


@pytest.mark.asyncio
async def test_ledger_does_not_enter_telegram_boundary_without_durable_state(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    link = 'https://e/sending-disk'
    assert await store.claim(link, 'Durable boundary')
    monkeypatch.setattr(bot, '_atomic_write_json', lambda *_a, **_k: (_ for _ in ()).throw(OSError('read only')))
    assert await store.mark_sending(link) is False
    assert store._reservations[bot.normalize_url(link)]['state'] == 'claimed'


@pytest.mark.asyncio
async def test_commit_disk_failure_stays_ambiguous_in_memory(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    link = 'https://e/commit-disk'
    assert await store.claim(link, 'Commit disk fail')
    assert await store.mark_sending(link)
    monkeypatch.setattr(bot, '_atomic_write_json', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk full')))
    assert await store.commit(link, 'Commit disk fail') is False
    assert link in store
    assert store.uncertain_count() == 1


def test_html_escape_happens_after_truncation_boundary():
    raw = ('x' * (bot.TG_CAPTION_LIMIT - 1)) + '&tail'
    escaped = bot._escape_to_limit(raw, bot.TG_CAPTION_LIMIT)
    # The entity must be complete; truncation must never leave '&am…' etc.
    assert '&am…' not in escaped and '&lt…' not in escaped and '&gt…' not in escaped
    import html as _html
    parsed = _html.unescape(escaped)
    assert len(parsed) <= bot.TG_CAPTION_LIMIT
    assert parsed.endswith('…')


def test_network_timeout_is_classified_as_ambiguous_delivery():
    TimedOut = type('TimedOut', (bot.TelegramError,), {})
    with pytest.raises(bot.DeliveryUncertain):
        bot._raise_if_ambiguous_tg_error(TimedOut('response lost'))


@pytest.mark.asyncio
async def test_network_ambiguous_send_is_not_released_for_retry(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'links.json')
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_channel_post',
                        AsyncMock(side_effect=bot.DeliveryUncertain('timeout')))
    fake_stats = SimpleNamespace(
        record_skipped=AsyncMock(), record_failed_send=AsyncMock(), record_published=AsyncMock())
    monkeypatch.setattr(bot, 'stats', fake_stats)
    news = {'title': 'Maybe delivered', 'link': 'https://e/maybe', 'source': 'A'}
    result = await bot.send_news(object(), news)
    assert result == 'uncertain'
    assert news['link'] in store
    assert store.uncertain_count() == 1


@pytest.mark.asyncio
async def test_scheduled_network_timeout_disables_automatic_retry(tmp_path, monkeypatch):
    store = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = store.add({'title': 'Maybe scheduled', 'link': 'https://e/s-maybe'},
                    bot.datetime.now(bot.timezone.utc) - bot.timedelta(minutes=1))
    monkeypatch.setattr(bot, 'scheduled_posts', store)
    monkeypatch.setattr(bot, '_send_channel_post',
                        AsyncMock(side_effect=bot.DeliveryUncertain('timeout')))
    notice = AsyncMock(return_value=1)
    monkeypatch.setattr(bot, 'notify_admin', notice)
    monkeypatch.setattr(bot.asyncio, 'sleep', AsyncMock())
    await bot.publish_scheduled(SimpleNamespace(bot=object()))
    assert store.meta(key)['state'] == 'uncertain'
    assert store.due() == []
    assert 'неизвестен' in notice.await_args.args[1]


def test_pending_manual_send_recovers_as_uncertain_after_restart(tmp_path):
    path = tmp_path / 'pending.json'
    store = bot.PendingPosts(path)
    key = store.add({'title': 'Manual', 'link': 'https://e/manual'})
    assert store.mark_channel_sending(key)
    recovered = bot.PendingPosts(path)
    assert recovered.channel_state(key) == 'uncertain'
    assert recovered.uncertain_count() == 1
