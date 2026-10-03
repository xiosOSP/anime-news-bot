"""Сторож эксплуатации: поломки, которые раньше были видны только в логах.

Очередь стоит, диск не пишет, растут неизвестные отправки, медиа в чате идут
без проверки — владелец узнаёт об этом письмом, один раз, а не из /health,
куда смотрят, когда уже что-то заметили.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(bot, '_ops_alerted', {})
    monkeypatch.setattr(bot, '_ops_uncertain_samples', bot.deque(maxlen=400))
    monkeypatch.setattr(bot, '_disk_write_failures', bot.deque(maxlen=200))
    monkeypatch.setattr(bot, '_moderation_unchecked_times', bot.deque(maxlen=2000))
    for name in ('post_queue', 'sent_links', 'pending_posts', 'scheduled_posts'):
        monkeypatch.setattr(bot, name, None)


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def queue_with_head(monkeypatch, waited_h, last_publish_h, *, auto=True, thread=False):
    head = {'news': {'title': 'Тайтл'}, 'queued_at': ago(waited_h)}
    monkeypatch.setattr(bot, 'post_queue', NS(oldest=lambda: head, _items=[head, head]))
    monkeypatch.setattr(bot, 'settings', NS(
        auto_enabled=auto, thread_mode=thread, check_interval_sec=1800,
        last_publish_at=ago(last_publish_h) if last_publish_h is not None else ''))


# ------------------------------------------------------------------ очередь

def test_stuck_queue_is_reported(monkeypatch):
    queue_with_head(monkeypatch, 7, 3)
    text = bot._ops_signals()['queue_stale']
    assert 'первый пост ждёт 7 ч' in text and 'Тайтл' in text and 'Постов в очереди: 2' in text


def test_long_queue_that_moves_is_not_stuck(monkeypatch):
    # Голова ждёт долго, но посты выходят по расписанию — это просто длинная очередь.
    queue_with_head(monkeypatch, 7, 0.5)
    assert 'queue_stale' not in bot._ops_signals()


def test_fresh_head_is_fine(monkeypatch):
    queue_with_head(monkeypatch, 5, 3)
    assert 'queue_stale' not in bot._ops_signals()


@pytest.mark.parametrize('auto, thread', [(False, False), (True, True)])
def test_queue_signal_only_when_the_queue_is_in_use(monkeypatch, auto, thread):
    queue_with_head(monkeypatch, 9, 9, auto=auto, thread=thread)
    assert 'queue_stale' not in bot._ops_signals()


def test_never_published_counts_as_stuck(monkeypatch):
    queue_with_head(monkeypatch, 7, None)
    assert 'ещё не было' in bot._ops_signals()['queue_stale']


# ------------------------------------------------------------------ диск

def test_failed_writes_are_counted_by_the_writer(tmp_path, monkeypatch):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    for _ in range(3):
        with pytest.raises(OSError):
            bot._atomic_write_json(blocker / 'sub' / 'queue.json', {})   # родитель — файл
    text = bot._ops_signals()['disk_writes']
    assert '3 сбоев' in text and 'queue.json' in text


def test_failure_while_writing_is_counted_too(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(bot.json, 'dumps', boom)
    with pytest.raises(OSError):
        bot._atomic_write_json(tmp_path / 'stats.json', {})
    assert bot._disk_write_failures[-1][1] == 'stats.json'
    assert list(tmp_path.iterdir()) == []                 # временный файл убран


def test_old_write_failures_are_forgotten():
    now = 10_000.0
    for at in (now - 2000, now - 1900, now - 10):
        bot._disk_write_failures.append((at, 'a.json', 'OSError'))
    assert 'disk_writes' not in bot._ops_signals(now)


# ------------------------------------------------------------------ неизвестные отправки

def test_growth_of_uncertain_deliveries(monkeypatch):
    count = {'n': 1}
    monkeypatch.setattr(bot, 'sent_links', NS(uncertain_count=lambda: count['n']))
    assert 'uncertain' not in bot._ops_signals(0.0)
    count['n'] = 3
    assert 'uncertain' not in bot._ops_signals(3600.0)
    count['n'] = 4
    assert 'прибавилось 3' in bot._ops_signals(7200.0)['uncertain']
    # Через сутки старый замер выпадает: долго висящие неизвестные — не рост.
    assert 'uncertain' not in bot._ops_signals(7200.0 + 86400 + 1)


# ------------------------------------------------------------------ медиа

def test_series_of_unchecked_media(monkeypatch):
    now = 50_000.0
    bot._moderation_unchecked_times.extend([now - 4000] * 30 + [now - 10] * 14)
    assert 'unchecked_media' not in bot._ops_signals(now)
    bot._moderation_unchecked_times.append(now - 5)
    assert 'За час 15 медиа' in bot._ops_signals(now)['unchecked_media']


@pytest.mark.asyncio
async def test_unchecked_media_is_noted_even_without_a_letter(monkeypatch):
    store = NS(is_enabled=lambda _c: True, log_decision=lambda *a, **k: None)
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: True)
    message = NS(chat_id=-100, message_id=1, text=None, caption=None, sender_chat=None,
                 from_user=NS(id=5, full_name='У', is_bot=False))
    await bot._mod_media_unchecked(None, message, 'Детектор занят', notify=False)
    assert len(bot._moderation_unchecked_times) == 1


# ------------------------------------------------------------------ письма

@pytest.mark.parametrize('name', ['OPS_QUEUE_STALE_HOURS', 'OPS_WRITE_FAILURES',
                                  'OPS_UNCERTAIN_GROWTH', 'OPS_UNCHECKED_MEDIA_PER_HOUR'])
def test_zero_threshold_turns_a_signal_off(monkeypatch, name):
    monkeypatch.setattr(bot, name, 0)
    queue_with_head(monkeypatch, 9, 9)
    for _ in range(5):
        bot._disk_write_failures.append((0.0, 'a.json', 'OSError'))
    bot._moderation_unchecked_times.extend([0.0] * 50)
    bot._ops_uncertain_samples.append((-10.0, 0))
    monkeypatch.setattr(bot, 'sent_links', NS(uncertain_count=lambda: 9))
    key = {'OPS_QUEUE_STALE_HOURS': 'queue_stale', 'OPS_WRITE_FAILURES': 'disk_writes',
           'OPS_UNCERTAIN_GROWTH': 'uncertain', 'OPS_UNCHECKED_MEDIA_PER_HOUR': 'unchecked_media'}[name]
    signals = bot._ops_signals(1.0)
    assert key not in signals and len(signals) == 3


@pytest.mark.asyncio
async def test_each_signal_is_sent_once_then_after_the_repeat_window(monkeypatch):
    clock = {'t': 1000.0}
    monkeypatch.setattr(bot.time, 'monotonic', lambda: clock['t'])
    signals = {'disk_writes': 'диск'}
    monkeypatch.setattr(bot, '_ops_signals', lambda now=None: dict(signals))
    notify = AsyncMock(return_value=1)
    monkeypatch.setattr(bot, 'notify_admin', notify)
    ctx = NS(bot=None)
    await bot.ops_watch_job(ctx)
    await bot.ops_watch_job(ctx)
    assert notify.await_count == 1
    clock['t'] += bot.OPS_ALERT_REPEAT_HOURS * 3600
    await bot.ops_watch_job(ctx)
    assert notify.await_count == 2
    # Прошло — сигнал снова взведён: следующая поломка придёт сразу.
    signals.clear()
    await bot.ops_watch_job(ctx)
    signals['disk_writes'] = 'диск'
    clock['t'] += 1
    await bot.ops_watch_job(ctx)
    assert notify.await_count == 3


@pytest.mark.asyncio
async def test_undelivered_alert_is_retried_next_tick(monkeypatch):
    monkeypatch.setattr(bot, '_ops_signals', lambda now=None: {'uncertain': 'x'})
    notify = AsyncMock(side_effect=[0, 1])
    monkeypatch.setattr(bot, 'notify_admin', notify)
    await bot.ops_watch_job(NS(bot=None))
    await bot.ops_watch_job(NS(bot=None))
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_watch_job_is_registered(monkeypatch):
    names = []
    jq = NS(run_repeating=lambda *a, name=None, **k: names.append(name), run_once=lambda *a, **k: None,
            get_jobs_by_name=lambda *_: [])
    app = NS(job_queue=jq, bot=NS(set_my_commands=AsyncMock(), set_my_description=AsyncMock(),
                                  set_my_short_description=AsyncMock()))
    monkeypatch.setattr(bot, 'settings', NS(auto_enabled=False))
    monkeypatch.setattr(bot, '_start_event_loop_lag_monitor', lambda: None)
    try:
        await bot.setup_bot_commands(app)
    except Exception:
        pass
    assert 'ops_watch' in names


@pytest.mark.asyncio
async def test_queue_head_includes_the_post_being_sent(tmp_path, monkeypatch):
    # Застрявшая отправка — тоже «стоящая очередь»: её пост вышел из списка,
    # но не из очереди.
    monkeypatch.setattr(bot, 'settings', NS(require_image=False))
    queue = bot.PostQueue(tmp_path / 'queue.json')
    assert queue.oldest() is None
    await queue.push_many([{'title': f'N{i}', 'link': f'https://a.test/{i}', 'source': 'S',
                            'images': ['i']} for i in range(2)])
    assert queue.oldest()['news']['title'] == 'N0'
    await queue.pop_next()
    assert queue.oldest()['news']['title'] == 'N0'
