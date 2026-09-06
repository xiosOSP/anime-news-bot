"""Regression coverage for thread-mode autosend incident.

These tests intentionally exercise the *cycle*, not only the admission helper:
we already had a unit test that blessed the old thread cap while production
could still report deferred candidates and publish zero posts.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


class _NoHistory:
    def __contains__(self, _link):
        return False

    def has_title(self, _title):
        return False


class _Settings(SimpleNamespace):
    pass


def _news(i: int, *, review: bool = False) -> dict:
    return {
        'title': f'News {i}',
        'link': f'https://example.test/{i}',
        'source': 'Test',
        'images': ['https://img.test/a.jpg'],
        '_priority_score': 100 - i,
        '_needs_review': review,
    }


def _common_cycle_stubs(monkeypatch, items, *, thread_mode=True, quiet=True,
                        confidence=False):
    monkeypatch.setattr(bot, 'settings', _Settings(
        thread_mode=thread_mode,
        quiet_mode=quiet,
    ))
    monkeypatch.setattr(bot, 'sent_links', _NoHistory())
    monkeypatch.setattr(bot, 'collect_all_news', AsyncMock(return_value=(
        list(items), ['Test: %d' % len(items)], [])))
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_editorial_allowed', lambda _n: True)
    monkeypatch.setattr(bot, '_evaluate_adaptive_publishing', lambda *_a, **_k: None)
    monkeypatch.setattr(bot, 'cleanup_video_dir', lambda: None)
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    monkeypatch.setattr(bot, '_auto_disabled_pending', [])
    monkeypatch.setattr(bot, '_maybe_send_daily_summary', AsyncMock())
    monkeypatch.setattr(bot, '_check_silence', AsyncMock())
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, 'PAUSE_BETWEEN_SENDS', 0)
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: {
        'shadow_mode': False,
        'backpressure': True,
        'confidence_moderation': confidence,
    }.get(name, False))
    return SimpleNamespace(bot=MagicMock())


def test_thread_backpressure_is_disabled_before_final_filters(monkeypatch):
    items = [_news(i) for i in range(30)]
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: name == 'backpressure')
    kept, deferred, level = bot._backpressure_candidates(items, 999, thread_mode=True)
    assert kept == items
    assert deferred == 0
    assert level == 'thread_off'


@pytest.mark.asyncio
async def test_thread_cycle_attempts_candidates_behind_old_cap(monkeypatch):
    """20 early duplicates must not hide 10 useful candidates behind the old cap."""
    items = [_news(i) for i in range(30)]
    ctx = _common_cycle_stubs(monkeypatch, items)
    results = ['skipped_dup'] * 20 + ['sent'] * 10
    sender = AsyncMock(side_effect=results)
    monkeypatch.setattr(bot, 'send_news_to_thread', sender)

    await bot._check_news_cycle(ctx)

    assert sender.await_count == 30
    assert 'fresh=30' in bot._runtime_health['last_check_result']
    assert 'sent=10' in bot._runtime_health['last_check_result']


@pytest.mark.asyncio
async def test_one_broken_thread_item_does_not_abort_batch(monkeypatch):
    items = [_news(i) for i in range(3)]
    ctx = _common_cycle_stubs(monkeypatch, items)
    sender = AsyncMock(side_effect=[RuntimeError('bad item'), 'sent', 'sent'])
    monkeypatch.setattr(bot, 'send_news_to_thread', sender)

    await bot._check_news_cycle(ctx)

    assert sender.await_count == 3
    assert 'sent=2' in bot._runtime_health['last_check_result']
    assert 'failed=1' in bot._runtime_health['last_check_result']


@pytest.mark.asyncio
async def test_quiet_mode_explains_zero_sent_after_filtering(monkeypatch):
    items = [_news(i) for i in range(3)]
    ctx = _common_cycle_stubs(monkeypatch, items, quiet=True)
    monkeypatch.setattr(bot, 'send_news_to_thread', AsyncMock(
        side_effect=['skipped_dup', 'skipped_filter', 'skipped_dup']))

    await bot._check_news_cycle(ctx)

    notify = bot.notify_admin
    assert notify.await_count == 1
    text = notify.await_args.args[1]
    assert 'Новых отправлено этой проверкой: 0' in text
    assert 'Уже были опубликованы / распознаны как дубли: 2' in text
    assert 'Отсеяно после подготовки: 1' in text
    assert 'фильтр 1' in text
    assert 'backpressure' not in text.lower()


@pytest.mark.asyncio
async def test_confidence_review_exception_does_not_block_channel_queue(monkeypatch):
    review, normal = _news(1, review=True), _news(2, review=False)
    ctx = _common_cycle_stubs(monkeypatch, [review, normal], thread_mode=False,
                              quiet=True, confidence=True)
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_FROM_ENV', True)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_FROM_ENV', True)
    monkeypatch.setattr(bot, 'send_news_to_thread', AsyncMock(
        side_effect=RuntimeError('review failed unexpectedly')))
    monkeypatch.setattr(bot, 'send_news', AsyncMock(return_value='sent'))

    queue = MagicMock()
    queue.peek_size = AsyncMock(return_value=0)
    queue.push_many = AsyncMock(return_value=1)
    queue.pop_next = AsyncMock(side_effect=[normal, None])
    queue.ack_done = AsyncMock()
    queue.requeue_failed = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, 'post_queue', queue)

    await bot._check_news_cycle(ctx)

    bot.send_news.assert_awaited_once_with(ctx.bot, normal)
    queue.ack_done.assert_awaited_once_with(normal)
    assert bot._runtime_health['last_check_result'].startswith('channel:sent')


@pytest.mark.asyncio
async def test_skipped_items_do_not_consume_telegram_pause(monkeypatch):
    items = [_news(i) for i in range(4)]
    ctx = _common_cycle_stubs(monkeypatch, items, quiet=True)
    monkeypatch.setattr(bot, 'send_news_to_thread', AsyncMock(
        side_effect=['skipped_dup', 'skipped_filter', 'skipped_dup', 'skipped_filter']))
    fake_sleep = AsyncMock()
    monkeypatch.setattr(bot, 'asyncio', SimpleNamespace(
        sleep=fake_sleep, CancelledError=asyncio.CancelledError))

    await bot._check_news_cycle(ctx)

    assert fake_sleep.await_count == 0


@pytest.mark.asyncio
async def test_thread_batch_does_not_swallow_cancellation(monkeypatch):
    items = [_news(1), _news(2)]
    ctx = _common_cycle_stubs(monkeypatch, items)
    sender = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(bot, 'send_news_to_thread', sender)

    with pytest.raises(asyncio.CancelledError):
        await bot._check_news_cycle(ctx)
    assert sender.await_count == 1


class TestThreadSendBudget:
    """Потолок пачки в ветке считается по отправкам, а не по кандидатам.

    Исходный дефект был в порядке: список резался ДО финальных фильтров, и
    если первые N отсеивались дедупом, цикл заканчивался нулём отправок при
    полном списке отложенных. Снять потолок целиком — тоже не решение: без
    него всплеск из 25 источников выливается в ветку одной простынёй.
    """

    @pytest.mark.asyncio
    async def test_skips_do_not_consume_the_budget(self, monkeypatch):
        """40 дублей не должны съесть потолок: годные новости идут следом."""
        cap = bot.BACKPRESSURE_THREAD_MAX_PER_CYCLE
        items = [_news(i) for i in range(40 + cap + 5)]
        ctx = _common_cycle_stubs(monkeypatch, items)
        results = ['skipped_dup'] * 40 + ['sent'] * (cap + 5)
        sender = AsyncMock(side_effect=results)
        monkeypatch.setattr(bot, 'send_news_to_thread', sender)

        await bot._check_news_cycle(ctx)

        # Дубли прошли бесплатно, дальше ушёл ровно потолок отправок.
        assert sender.await_count == 40 + cap
        assert f'sent={cap}' in bot._runtime_health['last_check_result']

    @pytest.mark.asyncio
    async def test_batch_is_capped_by_actual_sends(self, monkeypatch):
        cap = bot.BACKPRESSURE_THREAD_MAX_PER_CYCLE
        items = [_news(i) for i in range(cap * 3)]
        ctx = _common_cycle_stubs(monkeypatch, items)
        sender = AsyncMock(return_value='sent')
        monkeypatch.setattr(bot, 'send_news_to_thread', sender)

        await bot._check_news_cycle(ctx)

        assert sender.await_count == cap
        assert f'sent={cap}' in bot._runtime_health['last_check_result']

    @pytest.mark.asyncio
    async def test_failed_and_uncertain_also_consume_the_budget(self, monkeypatch):
        """Они обращались к Telegram, значит стоили и времени, и лимита."""
        cap = bot.BACKPRESSURE_THREAD_MAX_PER_CYCLE
        items = [_news(i) for i in range(cap * 2)]
        ctx = _common_cycle_stubs(monkeypatch, items)
        results = (['failed'] * (cap // 2) + ['uncertain'] * (cap - cap // 2)
                   + ['sent'] * cap)
        sender = AsyncMock(side_effect=results)
        monkeypatch.setattr(bot, 'send_news_to_thread', sender)

        await bot._check_news_cycle(ctx)

        assert sender.await_count == cap
        assert 'sent=0' in bot._runtime_health['last_check_result']

    @pytest.mark.asyncio
    async def test_remainder_is_reported_as_deferred(self, monkeypatch):
        cap = bot.BACKPRESSURE_THREAD_MAX_PER_CYCLE
        items = [_news(i) for i in range(cap + 7)]
        ctx = _common_cycle_stubs(monkeypatch, items, quiet=False)
        monkeypatch.setattr(bot, 'send_news_to_thread', AsyncMock(return_value='sent'))
        notify = AsyncMock(return_value=1)
        monkeypatch.setattr(bot, 'notify_admin', notify)

        await bot._check_news_cycle(ctx)

        text = notify.await_args.args[1]
        assert 'Отложено backpressure-ом: 7' in text

    @pytest.mark.asyncio
    async def test_no_cap_when_backpressure_is_off(self, monkeypatch):
        cap = bot.BACKPRESSURE_THREAD_MAX_PER_CYCLE
        items = [_news(i) for i in range(cap * 2)]
        ctx = _common_cycle_stubs(monkeypatch, items)
        monkeypatch.setattr(bot, 'feature_enabled', lambda _name: False)
        sender = AsyncMock(return_value='sent')
        monkeypatch.setattr(bot, 'send_news_to_thread', sender)

        await bot._check_news_cycle(ctx)

        assert sender.await_count == cap * 2


class TestSourceDiscoveryQueueCheck:
    """`len(post_queue)` роняло весь автопоиск на каждом цикле.

    Падение ловилось выше и писалось предупреждением в лог, поэтому подсистема
    просто молча не работала — в проде это выглядело как строка
    `Source discovery cycle failed: TypeError: object of type 'PostQueue' has no len()`.
    """

    @pytest.mark.asyncio
    async def test_discovery_reads_queue_size_asynchronously(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'settings', _Settings(require_image=True))
        queue = bot.PostQueue(tmp_path / 'queue.json')
        monkeypatch.setattr(bot, 'post_queue', queue)
        monkeypatch.setattr(bot, 'source_discovery',
                            bot.SourceDiscoveryStore(tmp_path / 'discovery.json'))
        monkeypatch.setattr(bot, 'feature_enabled',
                            lambda name: name in ('source_discovery', 'backpressure'))

        result = await bot._run_source_discovery(
            [{'source': 'Test', 'link': 'https://example.test/a', 'title': 't'}])

        assert result['skipped'] == ''
        assert result['scanned'] == 1

    @pytest.mark.asyncio
    async def test_discovery_still_defers_under_backpressure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'settings', _Settings(require_image=True))
        queue = MagicMock()
        queue.peek_size = AsyncMock(return_value=bot.BACKPRESSURE_SOFT_QUEUE + 1)
        monkeypatch.setattr(bot, 'post_queue', queue)
        monkeypatch.setattr(bot, 'source_discovery',
                            bot.SourceDiscoveryStore(tmp_path / 'discovery.json'))
        monkeypatch.setattr(bot, 'feature_enabled',
                            lambda name: name in ('source_discovery', 'backpressure'))

        result = await bot._run_source_discovery([])

        assert result['skipped'] == 'backpressure'
