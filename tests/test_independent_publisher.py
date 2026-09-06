"""Доставка не должна зависеть от сбора новостей.

Раньше публикация жила внутри цикла проверки: пока обход всех источников не
дойдёт до конца, готовый пост из очереди не уходил. Зависший парсер или упавший
цикл останавливали и доставку уже найденного — канал замолкал, хотя посты
лежали готовые.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Очередь с готовыми постами и подменённая отправка."""
    sent = []

    async def fake_send(bot_api, news, **kwargs):
        sent.append(news['link'])
        return 'sent'

    monkeypatch.setattr(bot, 'settings', MagicMock(
        thread_mode=False, auto_enabled=True, check_interval_sec=1800,
        last_publish_at='', require_image=True, quiet_mode=True))
    queue = bot.PostQueue(Path(tmp_path) / 'queue.json')
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, 'send_news', fake_send)
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock())
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
    ctx = SimpleNamespace(bot=MagicMock(), application=SimpleNamespace(job_queue=None))
    return SimpleNamespace(queue=queue, sent=sent, ctx=ctx)


def _overdue():
    return (datetime.now(timezone.utc) - timedelta(seconds=9000)).isoformat()


async def _fill(queue, count=4):
    await queue.push_many([
        {'title': f'News {i}', 'link': f'https://example.test/{i}',
         'images': ['img'], 'source': 'S'} for i in range(count)])


class TestDeliverySurvivesBrokenCollection:
    async def test_queue_drains_while_collection_keeps_failing(self, wired, monkeypatch):
        await _fill(wired.queue, 3)

        async def broken_collect():
            raise RuntimeError('все парсеры упали')

        monkeypatch.setattr(bot, 'collect_all_news', broken_collect)

        for _ in range(3):
            await bot.check_news(wired.ctx)          # цикл падает и это нормально
            bot.settings.last_publish_at = _overdue()
            await bot.publisher_tick(wired.ctx)

        assert len(wired.sent) == 3, 'доставка встала вместе со сбором'
        assert await wired.queue.peek_size() == 0

    async def test_publisher_is_idle_when_queue_is_empty(self, wired):
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        assert wired.sent == []


class TestPublisherNeverDuplicates:
    async def test_one_post_per_tick(self, wired):
        await _fill(wired.queue, 4)
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        assert len(wired.sent) == 1

    async def test_interval_is_respected(self, wired):
        await _fill(wired.queue, 4)
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        # Второй тик сразу же: интервал ещё не прошёл.
        await bot.publisher_tick(wired.ctx)
        assert len(wired.sent) == 1, 'publisher нарушил темп публикаций'

    async def test_parallel_ticks_do_not_race(self, wired):
        await _fill(wired.queue, 6)
        bot.settings.last_publish_at = _overdue()
        await asyncio.gather(*(bot.publisher_tick(wired.ctx) for _ in range(10)))
        assert len(wired.sent) == 1
        assert len(wired.sent) == len(set(wired.sent)), 'дубль в канале'

    async def test_publisher_stands_down_during_a_cycle(self, wired):
        """Цикл сам публикует в конце — двое из одной очереди тянуть не должны."""
        await _fill(wired.queue, 3)
        bot.settings.last_publish_at = _overdue()
        async with bot._check_news_lock:
            await bot.publisher_tick(wired.ctx)
        assert wired.sent == [], 'publisher влез в очередь во время цикла'


class TestPublisherGuards:
    async def test_disabled_in_thread_mode(self, wired):
        await _fill(wired.queue, 2)
        bot.settings.thread_mode = True
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        assert wired.sent == [], 'в режиме ветки очередь не используется'

    async def test_disabled_when_autosend_is_off(self, wired):
        await _fill(wired.queue, 2)
        bot.settings.auto_enabled = False
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        assert wired.sent == []

    async def test_feature_flag_turns_it_off(self, wired, monkeypatch):
        await _fill(wired.queue, 2)
        monkeypatch.setattr(bot, 'feature_enabled',
                            lambda name: name != 'independent_publisher')
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)
        assert wired.sent == []

    async def test_send_failure_does_not_kill_the_job(self, wired, monkeypatch):
        await _fill(wired.queue, 2)

        async def boom(bot_api, news, **kwargs):
            raise RuntimeError('Telegram недоступен')

        monkeypatch.setattr(bot, 'send_news', boom)
        bot.settings.last_publish_at = _overdue()
        await bot.publisher_tick(wired.ctx)      # не должно выбросить наружу

    def test_publish_due_handles_broken_timestamp(self, wired):
        bot.settings.last_publish_at = 'не дата'
        assert bot._publish_due() is True

    def test_publish_due_accepts_naive_timestamp(self, wired):
        bot.settings.last_publish_at = datetime.now().isoformat()
        assert bot._publish_due() is False


class TestJobRegistration:
    def test_publisher_job_registered_once(self, monkeypatch):
        monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
        queue = MagicMock()
        queue.get_jobs_by_name.return_value = []
        assert bot._ensure_publisher_job(queue) is True
        queue.get_jobs_by_name.return_value = [MagicMock()]
        assert bot._ensure_publisher_job(queue) is False
        assert queue.run_repeating.call_count == 1

    def test_no_job_when_feature_disabled(self, monkeypatch):
        monkeypatch.setattr(bot, 'feature_enabled', lambda name: False)
        queue = MagicMock()
        queue.get_jobs_by_name.return_value = []
        assert bot._ensure_publisher_job(queue) is False
        queue.run_repeating.assert_not_called()
