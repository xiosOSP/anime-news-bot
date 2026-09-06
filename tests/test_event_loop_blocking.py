"""Регрессии на две проблемы, найденные при ревью Stage 3.

1. PostQueue залипала навсегда, если задача-владелец умирала, не вызвав
   ack_done: inflight висел в памяти и pop_next из любой другой задачи
   вечно возвращал None.
2. CPU-тяжёлые куски конвейера (Pillow-разбор кандидатов и генерация превью
   видео через ffprobe/ffmpeg) выполнялись прямо в корутине и подвешивали
   event loop на всё время подготовки поста.
"""
import asyncio
import json
import io
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

import anime_news_bot as bot


def _jpeg(size=(1280, 720)):
    buf = io.BytesIO()
    Image.new('RGB', size, (40, 90, 160)).save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _news(i):
    return {'title': f'News {i}', 'link': f'https://example.com/{i}',
            'summary': 's', 'images': ['http://img'], 'source': 'Test'}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(require_image=True))
    return bot.PostQueue(tmp_path / 'queue.json')


class TestQueueInflightReclaim:
    async def test_dead_owner_returns_post_to_head(self, queue):
        """Владелец умер без ack — пост возвращается в голову, а не теряется."""
        await queue.push_many([_news(1), _news(2)])

        async def owner():
            await queue.pop_next()
            await asyncio.sleep(30)          # тут задачу отменят

        task = asyncio.create_task(owner())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # FIFO не нарушен: первым снова идёт тот же пост.
        assert (await queue.pop_next())['link'] == 'https://example.com/1'
        assert (await queue.pop_next())['link'] == 'https://example.com/2'
        assert await queue.peek_size() == 0

    async def test_live_owner_still_blocks_second_consumer(self, queue):
        """Пока владелец жив, второй потребитель не должен получить пост."""
        await queue.push_many([_news(1), _news(2)])
        holding = asyncio.Event()
        release = asyncio.Event()

        async def owner():
            item = await queue.pop_next()
            holding.set()
            await release.wait()
            await queue.ack_done(item)

        task = asyncio.create_task(owner())
        await holding.wait()
        assert await queue.pop_next() is None
        release.set()
        await task
        assert (await queue.pop_next())['link'] == 'https://example.com/2'

    async def test_reclaim_is_written_to_disk(self, queue, tmp_path):
        """Возврат в голову должен попасть на диск, а не только в память."""
        await queue.push_many([_news(1), _news(2)])

        async def owner():
            await queue.pop_next()
            await asyncio.sleep(30)

        task = asyncio.create_task(owner())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        second = asyncio.create_task(queue.pop_next())
        assert (await second)['link'] == 'https://example.com/1'

        # На диске первый пост теперь снова обычный элемент очереди,
        # а inflight принадлежит уже новому владельцу.
        raw = json.loads((tmp_path / 'queue.json').read_text(encoding='utf-8'))
        assert raw['inflight']['news']['link'] == 'https://example.com/1'
        assert [i['news']['link'] for i in raw['items']] == ['https://example.com/2']


class TestPipelineDoesNotBlockLoop:
    async def test_image_scoring_runs_off_the_event_loop(self, monkeypatch):
        """Pillow-разбор кандидатов должен считаться в потоке, не в корутине."""
        threads = []
        original = bot._image_quality_info

        def spy(data, url):
            threads.append(threading.current_thread())
            return original(data, url)

        monkeypatch.setattr(bot, '_image_quality_info', spy)
        monkeypatch.setattr(bot, '_download_image_bytes', lambda _u: _jpeg())
        monkeypatch.setattr(bot, 'fetch_og_image', lambda _l: None)
        bot._image_bytes_cache.clear()

        news = {'title': 't', 'link': 'https://example.com/a', 'source': 'S',
                'images': [f'https://example.com/img{i}.jpg' for i in range(4)]}
        await bot._optimize_news_media(news)

        assert threads, 'scoring не вызывался'
        main = threading.main_thread()
        assert all(t is not main for t in threads)

    async def test_video_thumbnail_runs_off_the_event_loop(self, monkeypatch, tmp_path):
        """ffprobe/ffmpeg не должны крутиться в корутине отправки."""
        path = tmp_path / 'v.mp4'
        path.write_bytes(b'video')
        seen = []

        def fake_generate(p):
            seen.append(threading.current_thread())
            return b'jpeg-bytes'

        monkeypatch.setattr(bot, '_generate_video_thumbnail', fake_generate)
        kwargs = await bot._video_thumbnail_kwargs_async(path)

        assert kwargs == {'thumbnail': b'jpeg-bytes'}
        assert seen and seen[0] is not threading.main_thread()

    async def test_video_thumbnail_async_handles_missing_file(self):
        assert await bot._video_thumbnail_kwargs_async(None) == {}

    async def test_send_post_uses_async_thumbnail_path(self, monkeypatch, tmp_path):
        """_send_post обязан ходить через async-вариант, иначе блокировка вернётся."""
        path = tmp_path / 'v.mp4'
        path.write_bytes(b'video')
        seen = []

        def fake_generate(p):
            seen.append(threading.current_thread())
            return b'jpg'

        monkeypatch.setattr(bot, '_generate_video_thumbnail', fake_generate)
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(bot, 'format_news_post', lambda _n: 'Caption')
        tg = MagicMock()
        tg.send_video = AsyncMock()
        news = {'video': 'https://youtube.com/watch?v=x', 'images': [],
                'source': 'X', 'title': 'T'}

        assert await bot._send_post(tg, news, 1, path) is True
        assert tg.send_video.await_args.kwargs['thumbnail'] == b'jpg'
        assert seen and seen[0] is not threading.main_thread()

    async def test_thumbnail_generated_once_per_send(self, monkeypatch, tmp_path):
        """Три ветки отправки не должны трижды запускать ffmpeg."""
        path = tmp_path / 'v.mp4'
        path.write_bytes(b'video')
        calls = []
        monkeypatch.setattr(bot, '_generate_video_thumbnail',
                            lambda p: calls.append(p) or b'jpg')
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(bot, 'format_news_post', lambda _n: 'Caption')
        tg = MagicMock()
        tg.send_video = AsyncMock()
        news = {'video': 'https://youtube.com/watch?v=x', 'images': [],
                'source': 'X', 'title': 'T'}

        await bot._send_post(tg, news, 1, path)
        assert len(calls) == 1


class TestAnalyticsWriteIsOffLoop:
    """Запись аналитики переписывает весь файл: на потолке это ~100 мс.

    Раньше она шла прямо в корутине отправки, то есть бот замирал на каждой
    публикации. Теперь уходит в поток, а список событий защищён блокировкой.
    """

    @staticmethod
    def _filled(tmp_path, count=800, max_events=5000):
        # max_events снизу зажат сотней, поэтому для проверок без подрезки
        # потолок задаём явно и с запасом.
        store = bot.AnalyticsStore(tmp_path / 'analytics.json', max_events=max_events)
        store._data['events'] = [
            {'at': '2026-08-08T00:00:00+00:00', 'kind': 'delivery', 'story_id': f's{i}',
             'source': 'Test Source', 'result': 'sent'} for i in range(count)]
        return store

    def test_store_has_a_lock(self, tmp_path):
        store = bot.AnalyticsStore(tmp_path / 'analytics.json')
        assert hasattr(store, '_lock')

    async def test_parallel_records_do_not_lose_events(self, tmp_path):
        store = self._filled(tmp_path, count=10)
        await asyncio.gather(*(
            asyncio.to_thread(store.record, 'delivery', {'_story_id': f'new{i}'}, result='sent')
            for i in range(120)))
        ids = {e.get('story_id') for e in store._data['events']}
        assert len({i for i in ids if str(i).startswith('new')}) == 120
        on_disk = json.loads((tmp_path / 'analytics.json').read_text(encoding='utf-8'))
        assert len(on_disk['events']) == len(store._data['events'])

    async def test_send_news_does_not_write_analytics_inline(self, tmp_path, monkeypatch):
        """Запись должна происходить в другом потоке, а не в потоке цикла."""
        store = self._filled(tmp_path, count=10)
        seen = []
        original = store._save
        monkeypatch.setattr(store, '_save',
                            lambda: (seen.append(threading.current_thread()), original())[1])
        await asyncio.to_thread(store.record, 'delivery', {'_story_id': 'x'}, result='sent')
        assert seen and seen[0] is not threading.main_thread()

    def test_events_reader_takes_the_lock(self, tmp_path):
        """Чтение не должно видеть список в момент подрезки."""
        store = self._filled(tmp_path, count=50)
        assert len(store.events(kind='delivery')) == 50

    def test_trimming_keeps_the_newest_events(self, tmp_path):
        """Потолок должен срезать старое, а не новое."""
        store = bot.AnalyticsStore(tmp_path / 'trim.json', max_events=100)
        for i in range(150):
            store.record('delivery', {'_story_id': f's{i}'}, result='sent')
        ids = [e['story_id'] for e in store._data['events']]
        assert len(ids) == 100
        assert ids[0] == 's50' and ids[-1] == 's149'
