"""Тесты оптимизаций: кэш скачанных картинок и корректная обрезка истории."""
import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image, ImageDraw

import anime_news_bot


def _pic(w=800, h=450):
    im = Image.new('RGB', (w, h), (40, 60, 100))
    ImageDraw.Draw(im).ellipse([50, 50, w - 50, h - 50], fill=(230, 200, 120))
    buf = io.BytesIO()
    im.save(buf, 'JPEG')
    return buf.getvalue()


class TestImageBytesCache:
    """Один файл нужен трижды: отпечаток, размер превью, отправка байтами."""

    @pytest.fixture(autouse=True)
    def clean(self):
        anime_news_bot._image_bytes_cache.clear()
        yield
        anime_news_bot._image_bytes_cache.clear()

    def test_downloads_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(anime_news_bot, '_download_image_bytes',
                            lambda u: calls.append(u) or _pic())
        for _ in range(4):
            anime_news_bot._cached_image_bytes('https://cdn/pic.jpg')
        assert len(calls) == 1

    def test_different_urls_separate(self, monkeypatch):
        calls = []
        monkeypatch.setattr(anime_news_bot, '_download_image_bytes',
                            lambda u: calls.append(u) or _pic())
        anime_news_bot._cached_image_bytes('https://cdn/a.jpg')
        anime_news_bot._cached_image_bytes('https://cdn/b.jpg')
        assert len(calls) == 2

    def test_failure_cached_too(self, monkeypatch):
        """Неудачу тоже помним — иначе будем долбить мёртвую ссылку."""
        calls = []
        monkeypatch.setattr(anime_news_bot, '_download_image_bytes',
                            lambda u: calls.append(u) or None)
        assert anime_news_bot._cached_image_bytes('https://cdn/dead.jpg') is None
        assert anime_news_bot._cached_image_bytes('https://cdn/dead.jpg') is None
        assert len(calls) == 1

    def test_empty_url(self):
        assert anime_news_bot._cached_image_bytes('') is None

    def test_bounded(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_download_image_bytes', lambda u: b'x')
        for i in range(anime_news_bot.IMAGE_BYTES_CACHE_MAX + 15):
            anime_news_bot._cached_image_bytes(f'https://cdn/{i}.jpg')
        assert len(anime_news_bot._image_bytes_cache) <= \
            anime_news_bot.IMAGE_BYTES_CACHE_MAX

    def test_pipeline_downloads_once(self, tmp_path, monkeypatch):
        downloads = []
        img = _pic(1280, 720)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(image_dedup=True, video_enabled=False,
                                      require_image=True, llm_enabled=False,
                                      dedup_final_text=False, llm_tags=False,
                                      translator_engine='google'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes',
                            anime_news_bot.ImageHashes(tmp_path / 'h.json'))
        monkeypatch.setattr(anime_news_bot, 'published_texts', None)
        monkeypatch.setattr(anime_news_bot, 'recent_subjects', None)
        monkeypatch.setattr(anime_news_bot, 'stats', MagicMock(record_skipped=AsyncMock()))
        monkeypatch.setattr(anime_news_bot, 'fetch_og_image', lambda u: None)

        class FakeTr:
            def translate(self, t, input_limit=None):
                return t
        monkeypatch.setattr(anime_news_bot, 'translator', FakeTr())
        monkeypatch.setattr(anime_news_bot, 'anilist', MagicMock(lookup=lambda q: None))
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda url, **kw: downloads.append(url) or MagicMock(
                                status_code=200, content=img,
                                headers={'Content-Type': 'image/jpeg'}))
        news = {'title': 'Тизер второго сезона', 'summary': 'x' * 300,
                'link': 'https://a/1', 'source': 'X', 'video': None,
                'images': ['https://cdn/pic.jpg'], '_thumb_only': True,
                'published_parsed': None, 'lang': 'ru'}
        asyncio.run(anime_news_bot._prepare_news_for_send(news, 'X'))
        assert len(downloads) == 1          # было 2


class TestSentLinksTrim:
    """При обрезке истории заголовки стирались ПОЛНОСТЬЮ — защита от дублей
    по названию обнулялась на несколько дней."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'SENT_LINKS_MAX', 100)
        monkeypatch.setattr(anime_news_bot, 'SENT_LINKS_TRIM_TO', 60)
        return anime_news_bot.SentLinksStore(tmp_path / 'links.json')

    def _fill(self, store, n):
        async def go():
            for i in range(n):
                await store.claim(f'https://s.com/a{i}', f'Заголовок номер {i}')
        asyncio.run(go())

    def test_titles_survive_trim(self, store):
        self._fill(store, 150)
        assert store._titles, 'заголовки не должны стираться подчистую'

    def test_newest_kept_oldest_dropped(self, store):
        self._fill(store, 150)
        assert store.has_title('Заголовок номер 149')
        assert not store.has_title('Заголовок номер 1')

    def test_counts_match(self, store):
        self._fill(store, 150)
        assert len(store._titles) == len(store._title_set)

    def test_survives_restart(self, store, tmp_path, monkeypatch):
        self._fill(store, 150)
        monkeypatch.setattr(anime_news_bot, 'SENT_LINKS_MAX', 100)
        again = anime_news_bot.SentLinksStore(store.path)
        assert again.has_title('Заголовок номер 149')
        assert len(again._titles) == len(store._titles)

    def test_release_removes_from_both(self, store):
        async def go():
            await store.claim('https://s.com/new', 'Свежий заголовок')
            assert store.has_title('Свежий заголовок')
            await store.release('https://s.com/new', 'Свежий заголовок')
        asyncio.run(go())
        assert not store.has_title('Свежий заголовок')
        assert all('свежии' not in t for t in store._titles)

    def test_old_format_migrates(self, tmp_path):
        import json
        p = tmp_path / 'links.json'
        p.write_text(json.dumps(['https://s.com/a', 'https://s.com/b']))
        store = anime_news_bot.SentLinksStore(p)
        assert len(store._urls) == 2
        assert store._titles == []
