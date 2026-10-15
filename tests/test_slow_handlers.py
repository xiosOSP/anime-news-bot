"""Долгие команды и кнопки не держат остальные обновления; запросы к Bot API
ждут столько, сколько нужно большому ролику; склейка историй идёт не в цикле
событий; таймаут источника считается с момента, когда ему дали слот.

Аудит октября 2026: PTB разбирает обновления по одному, и /news или нажатие
«📢 В канал» (минута на сбор и видео) задерживали всё остальное — кнопки
админов и модерацию чата.
"""
import asyncio
import inspect
import re
import threading
import time
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot

SLOW = ('news', 'preview', 'backup', 'videocheck', 'llmping', 'llmclean', 'mediaping', 'audit',
        'verifybackup', 'selftest', 'canary', 'discover', 'posts', 'replay', 'golden',
        'modllmping', 'modtest')


def _main_source() -> str:
    return inspect.getsource(bot.main)


@pytest.mark.parametrize('name', SLOW)
def test_slow_command_runs_as_a_task(name):
    assert f'CommandHandler("{name}", {name}_command, block=False)' in _main_source()


def test_quick_settings_stay_in_order():
    assert 'CommandHandler("settings", settings_command)' in _main_source()


@pytest.mark.parametrize('data', ['pub:12', 'snow:5', 'queue:send_now'])
def test_publishing_buttons_run_as_a_task(data):
    assert re.match(bot.SLOW_CALLBACK_RE, data)


@pytest.mark.parametrize('data', ['settings:queue', 'sch:12', 'edit:12', 'dis:12', 'mod:ban:1',
                                  'queue:send_now_later', 'llmslot:primary'])
def test_other_buttons_keep_their_order(data):
    assert not re.match(bot.SLOW_CALLBACK_RE, data)


def test_slow_buttons_are_registered_before_the_catch_all():
    source = _main_source()
    slow = source.index('CallbackQueryHandler(settings_callback, pattern=SLOW_CALLBACK_RE, block=False)')
    rest = source.index('CallbackQueryHandler(settings_callback))')
    assert slow < rest


def test_reply_keyboard_buttons_run_as_a_task():
    # «🔍 Свежие новости» на клавиатуре — тот же /news.
    assert 'MessageHandler(reply_filter, reply_button_handler, block=False)' in _main_source()


def test_bot_api_waits_long_enough_for_big_videos():
    request = bot._application_builder().build().bot.request
    timeout = request._client.timeout
    assert timeout.read >= 30 and timeout.write >= 30 and timeout.connect >= 10
    assert request._media_write_timeout >= 180
    assert 'job_queue(JobQueue())' in _main_source()
    assert '_application_builder()' in _main_source()


@pytest.mark.asyncio
async def test_story_clustering_runs_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen = []

    def cluster(items, persist_intelligence=True):
        seen.append(threading.get_ident())
        return items
    monkeypatch.setattr(bot, '_cluster_news', cluster)
    monkeypatch.setattr(bot, 'SOURCES', [])
    monkeypatch.setattr(bot, 'settings', NS(is_source_enabled=lambda _n: True, require_image=False))
    await bot.collect_all_news()
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_source_gets_its_full_time_after_waiting_for_a_slot(monkeypatch):
    """Пять медленных источников заняли пул; шестой ждал слот и всё равно успел."""
    monkeypatch.setattr(bot, 'SOURCE_FETCH_CONCURRENCY', 2)
    budget = 1.0

    def slow():
        time.sleep(0.7)
        return []

    def healthy():
        time.sleep(0.5)
        return ['ok']
    jobs = [('slow-a', slow), ('slow-b', slow), ('healthy', healthy)]
    results = await asyncio.gather(*(bot._run_source_collector_bounded(n, c, budget) for n, c in jobs),
                                   return_exceptions=True)
    assert results[2] == ['ok'], f'здоровый источник оборван чужой медлительностью: {results[2]!r}'


@pytest.mark.asyncio
async def test_hung_source_is_still_cut_off(monkeypatch):
    monkeypatch.setattr(bot, 'SOURCE_FETCH_CONCURRENCY', 2)

    def hung():
        time.sleep(1.0)
        return []
    with pytest.raises(TimeoutError):
        await bot._run_source_collector_bounded('hung-source', hung, 0.2)


@pytest.mark.asyncio
async def test_second_news_command_does_not_start_a_second_collection(monkeypatch):
    """/news идёт задачей; второй вызов, пока первый собирает, только отвечает."""
    gate = asyncio.Event()
    collected = []

    async def collect():
        collected.append(1)
        await gate.wait()
        return [], ['A: 0'], []
    monkeypatch.setattr(bot, 'collect_all_news', collect)
    replies = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)
            return self

        async def edit_text(self, text, **kw):
            replies.append(text)
    update = NS(message=Msg())
    news = inspect.unwrap(bot.news_command)
    preview = inspect.unwrap(bot.preview_command)
    first = asyncio.create_task(news(update, NS(bot=None)))
    await asyncio.sleep(0.01)
    await news(update, NS(bot=None))
    await preview(update, NS(bot=None))
    gate.set()
    await first
    assert collected == [1]
    assert replies.count(bot._MANUAL_COLLECT_BUSY) == 2
