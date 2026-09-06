"""Тесты PostQueue — очередь постов с TTL и фильтром по картинкам."""
import json
from datetime import datetime, timedelta

import pytest

import anime_news_bot
from anime_news_bot import PostQueue, QUEUE_MAX_SIZE, QUEUE_POST_TTL_HOURS


@pytest.fixture
def queue(tmp_json, fake_settings):
    return PostQueue(tmp_json)


@pytest.fixture
def fake_settings(monkeypatch):
    """Подсовываем BotSettings-like объект с require_image атрибутом."""
    class FakeSettings:
        require_image = True
    fake = FakeSettings()
    monkeypatch.setattr(anime_news_bot, 'settings', fake)
    return fake


def make_news(i, with_image=True, title=None):
    return {
        'title': title or f'News {i}',
        'link': f'https://example.com/news/{i}',
        'summary': f'Summary {i}',
        'image': 'http://img' if with_image else None,
        'images': ['http://img'] if with_image else [],
        'video': None,
        'source': 'Test',
    }


class TestPush:
    async def test_push_three(self, queue):
        news = [make_news(i) for i in range(3)]
        added = await queue.push_many(news)
        assert added == 3
        assert await queue.peek_size() == 3

    async def test_push_dedupes(self, queue):
        news1 = [make_news(1), make_news(2)]
        await queue.push_many(news1)
        # Те же + новый
        news2 = [make_news(1), make_news(2), make_news(3)]
        added = await queue.push_many(news2)
        assert added == 1
        assert await queue.peek_size() == 3

    async def test_push_filters_no_image(self, queue, fake_settings):
        fake_settings.require_image = True
        news = [
            make_news(1, with_image=True),
            make_news(2, with_image=False),
            make_news(3, with_image=True),
        ]
        added = await queue.push_many(news)
        assert added == 2  # без картинки — отброшен

    async def test_push_allows_no_image_when_disabled(self, queue, fake_settings):
        fake_settings.require_image = False
        news = [make_news(i, with_image=False) for i in range(3)]
        added = await queue.push_many(news)
        assert added == 3

    async def test_push_overflow_drops_oldest(self, queue):
        news = [make_news(i) for i in range(QUEUE_MAX_SIZE + 5)]
        await queue.push_many(news)
        size = await queue.peek_size()
        assert size == QUEUE_MAX_SIZE
        # Должны быть самые новые — первый pop вернёт пост с большим индексом
        p = await queue.pop_next()
        assert p['link'] == 'https://example.com/news/5'


class TestPop:
    async def test_pop_fifo(self, queue):
        await queue.push_many([make_news(0), make_news(1), make_news(2)])
        p = await queue.pop_next()
        assert p['title'] == 'News 0'
        p = await queue.pop_next()
        assert p['title'] == 'News 1'

    async def test_pop_empty(self, queue):
        assert await queue.pop_next() is None

    async def test_pop_skips_no_image_posts(self, queue, tmp_json, fake_settings):
        # Кладём в очередь руками минуя фильтр push (моделируем старые данные)
        fake_settings.require_image = False
        await queue.push_many([
            make_news(1, with_image=False),
            make_news(2, with_image=False),
            make_news(3, with_image=True),
        ])
        # Теперь включаем фильтр и попап — первые два должны выбраться
        fake_settings.require_image = True
        p = await queue.pop_next()
        assert p['title'] == 'News 3'
        # Очередь теперь пуста
        assert await queue.peek_size() == 0


class TestTTL:
    async def test_expired_purged(self, queue, tmp_json, fake_settings, monkeypatch):
        # Положим запись с устаревшим queued_at вручную
        old_time = (datetime.now() - timedelta(hours=QUEUE_POST_TTL_HOURS + 1)).isoformat()
        fresh_time = datetime.now().isoformat()
        items = [
            {'news': make_news(1), 'queued_at': old_time},
            {'news': make_news(2), 'queued_at': fresh_time},
        ]
        tmp_json.write_text(json.dumps(items))
        new_queue = PostQueue(tmp_json)
        # peek_size триггерит purge
        size = await new_queue.peek_size()
        assert size == 1
        p = await new_queue.pop_next()
        assert p['title'] == 'News 2'


class TestClear:
    async def test_clear(self, queue):
        await queue.push_many([make_news(i) for i in range(5)])
        count = await queue.clear()
        assert count == 5
        assert await queue.peek_size() == 0


class TestPersistence:
    async def test_survives_restart(self, tmp_json, fake_settings):
        q1 = PostQueue(tmp_json)
        await q1.push_many([make_news(i) for i in range(3)])
        q2 = PostQueue(tmp_json)
        assert await q2.peek_size() == 3
        p = await q2.pop_next()
        assert p['title'] == 'News 0'
