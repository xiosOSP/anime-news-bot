"""Тесты цельной отправки в ветку: медиа+текст одним сообщением, все фото, видео из TG."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InputMediaVideo

import anime_news_bot


@pytest.fixture(autouse=True)
def no_buttons(monkeypatch):
    monkeypatch.setattr(anime_news_bot, 'pending_posts', None)


class _FakeStream:
    """Ответ requests.get(stream=True) для тестов скачивания медиа."""

    def __init__(self, content=b'', ctype='video/mp4', status=200):
        self.content = content
        self.status_code = status
        self.headers = {'Content-Type': ctype, 'Content-Length': str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


def _stream_patch(monkeypatch, mapping):
    """mapping: функция url -> _FakeStream | None (None = сетевой сбой)."""
    def fake_get(url, **kw):
        resp = mapping(url)
        if resp is None:
            raise OSError('network down')
        return resp
    monkeypatch.setattr(anime_news_bot.requests, 'get', fake_get)
    # Unit-тесты не должны зависеть от DNS окружения; SSRF-валидация тестируется отдельно.
    monkeypatch.setattr(anime_news_bot, '_is_public_http_url', lambda _url: True)


class TestDirectVideoDetection:
    def test_cdn_telegram_is_direct(self):
        assert anime_news_bot._is_direct_video('https://cdn4.cdn-telegram.org/file/x') is True

    def test_mp4_is_direct(self):
        assert anime_news_bot._is_direct_video('https://s.com/v.mp4') is True

    def test_youtube_not_direct(self):
        assert anime_news_bot._is_direct_video('https://youtube.com/watch?v=x') is False


class TestCohesivePosts:
    def _bot(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_media_group = AsyncMock()
        bot.send_message = AsyncMock()
        return bot

    def test_single_photo_has_caption(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long',
                            lambda n: 'Заголовок.\n\nТекст.')
        bot = self._bot()
        news = {'title': 'T', 'link': 'https://s/a', 'summary': '',
                'images': ['https://cdn.x/1.jpg'], 'video': None, 'source': 'X', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert bot.send_photo.await_count == 1
        assert 'Заголовок' in bot.send_photo.await_args.kwargs['caption']
        assert bot.send_message.await_count == 0  # НЕ отдельным текстом

    def test_four_photos_all_kept_in_album(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Заголовок.')
        bot = self._bot()
        photos = [f'https://cdn.x/{i}.jpg' for i in range(4)]
        news = {'title': 'T', 'link': 'https://s/a', 'summary': '', 'images': photos,
                'video': None, 'source': 'X', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        media = bot.send_media_group.await_args.kwargs['media']
        assert len(media) == 4  # ВСЕ 4, не одна
        assert 'Заголовок' in media[0].caption

    def test_video_plus_photos_cohesive_album(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Тизер.')
        monkeypatch.setattr(anime_news_bot, '_resolve_photos_for_album',
                            AsyncMock(return_value=['https://cdn.x/1.jpg', 'https://cdn.x/2.jpg']))
        monkeypatch.setattr(anime_news_bot, '_resolve_video',
                            AsyncMock(side_effect=lambda u: u))
        bot = self._bot()
        news = {'title': 'Тизер', 'link': 'https://t.me/c/1', 'summary': '',
                'images': ['https://cdn.x/1.jpg', 'https://cdn.x/2.jpg'],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        media = bot.send_media_group.await_args.kwargs['media']
        assert isinstance(media[0], InputMediaVideo)
        assert 'Тизер' in media[0].caption
        assert len(media) == 3

    def test_video_only_has_caption(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Видео.')
        monkeypatch.setattr(anime_news_bot, 'fetch_og_image', lambda u: None)
        monkeypatch.setattr(anime_news_bot, '_resolve_video',
                            AsyncMock(side_effect=lambda u: u))
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/2', 'summary': '', 'images': [],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert 'Видео' in bot.send_video.await_args.kwargs['caption']

    def test_long_text_falls_back_to_split(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Слово ' * 250)
        bot = self._bot()
        news = {'title': 'T', 'link': 'https://s/a', 'summary': '',
                'images': ['https://cdn.x/1.jpg'], 'video': None, 'source': 'X', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        # длинный текст → медиа отдельно + текст отдельно
        assert bot.send_photo.await_count == 1
        assert bot.send_message.await_count == 1


class TestTelegramVideoExtraction:
    def test_short_video_extracted(self, monkeypatch):
        html = '''<div class="tgme_widget_message" data-post="ch/1">
          <div class="tgme_widget_message_text">Тизер к второй серии выходит уже сегодня</div>
          <video src="https://cdn4.cdn-telegram.org/file/vid.mp4"></video>
          <time class="tgme_widget_message_video_duration">0:36</time>
          <time datetime="2026-07-03T10:00:00+00:00"></time>
        </div>'''
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        posts = anime_news_bot.get_telegram_channel('ch', 'TG: Test')
        assert len(posts) == 1
        assert posts[0]['video'] == 'https://cdn4.cdn-telegram.org/file/vid.mp4'

    def test_long_video_skipped(self, monkeypatch):
        html = '''<div class="tgme_widget_message" data-post="ch/1">
          <div class="tgme_widget_message_text">Длинное видео тут смотрите полную версию новости</div>
          <video src="https://cdn4.cdn-telegram.org/file/vid.mp4"></video>
          <time class="tgme_widget_message_video_duration">5:20</time>
          <time datetime="2026-07-03T10:00:00+00:00"></time>
        </div>'''
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        posts = anime_news_bot.get_telegram_channel('ch', 'TG: Test')
        assert posts[0]['video'] is None  # >60с — не берём


class TestTelegramVideoMarkupForms:
    """Разные формы разметки видео в t.me/s/ (реальные вариации)."""
    TXT = ('<div class="tgme_widget_message_text">'
           'текст новости достаточно длинный для прохода фильтра</div>'
           '<time datetime="2026-07-24T12:00:00+00:00"></time>')

    def _parse(self, html, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        return anime_news_bot.get_telegram_channel('c', 'TG: T')

    def test_video_player_wrapper(self, monkeypatch):
        html = (f'<div class="tgme_widget_message" data-post="c/1">{self.TXT}'
                '<a class="tgme_widget_message_video_player">'
                '<video class="tgme_widget_message_video" src="https://cdn.tg/v.mp4"></video>'
                '<time class="tgme_widget_message_video_duration">0:45</time></a></div>')
        posts = self._parse(html, monkeypatch)
        assert posts[0]['video'] == 'https://cdn.tg/v.mp4'

    def test_lazy_video_saves_thumb(self, monkeypatch):
        # Ленивая загрузка: прямого mp4 нет, но превью-кадр должен попасть в images
        html = (f'<div class="tgme_widget_message" data-post="c/1">{self.TXT}'
                '<a class="tgme_widget_message_video_thumb" '
                'style="background-image:url(\'https://cdn.tg/thumb.jpg\')"></a>'
                '<time class="tgme_widget_message_video_duration">0:36</time></div>')
        posts = self._parse(html, monkeypatch)
        assert posts[0]['video'] is None
        assert 'https://cdn.tg/thumb.jpg' in posts[0]['images']

    def test_source_tag_variant(self, monkeypatch):
        html = (f'<div class="tgme_widget_message" data-post="c/1">{self.TXT}'
                '<video><source src="https://cdn.tg/s.mp4" type="video/mp4"></video>'
                '<time class="tgme_widget_message_video_duration">0:20</time></div>')
        posts = self._parse(html, monkeypatch)
        assert posts[0]['video'] == 'https://cdn.tg/s.mp4'


class TestVideoResolver:
    """cdn-telegram видео Bot API по URL не принимает (как и фото) —
    _resolve_video качает их байтами; недоступные → пост без видео."""

    def test_cdn_downloaded_as_bytes(self, monkeypatch):
        _stream_patch(monkeypatch, lambda u: _FakeStream(b'MP4DATA'))
        out = asyncio.run(anime_news_bot._resolve_video(
            'https://cdn4.cdn-telegram.org/file/v.mp4'))
        assert out == b'MP4DATA'

    def test_oversize_video_rejected(self, monkeypatch):
        # Заявленный размер больше лимита — не качаем вовсе
        big = _FakeStream(b'x')
        big.headers['Content-Length'] = str((anime_news_bot.TG_VIDEO_MAX_MB + 5) * 1024 * 1024)
        _stream_patch(monkeypatch, lambda u: big)
        assert asyncio.run(anime_news_bot._resolve_video(
            'https://cdn4.cdn-telegram.org/file/v.mp4')) is None

    def test_stream_aborted_when_limit_exceeded(self, monkeypatch):
        # Content-Length соврал: обрыв должен произойти на лету
        lying = _FakeStream(b'y' * (3 * 1024 * 1024))
        lying.headers['Content-Length'] = '10'
        monkeypatch.setattr(anime_news_bot, 'TG_VIDEO_MAX_MB', 1)
        _stream_patch(monkeypatch, lambda u: lying)
        assert anime_news_bot._download_media_bytes(
            'https://cdn4.cdn-telegram.org/file/v.mp4', 1) is None

    def test_cdn_unavailable_returns_none(self, monkeypatch):
        _stream_patch(monkeypatch, lambda u: None)
        out = asyncio.run(anime_news_bot._resolve_video(
            'https://cdn4.cdn-telegram.org/file/v.mp4'))
        assert out is None

    def test_regular_url_passes_through(self, monkeypatch):
        called = []
        monkeypatch.setattr(anime_news_bot.requests, 'get',
                            lambda *a, **k: called.append(1))
        out = asyncio.run(anime_news_bot._resolve_video('https://site.com/v.mp4'))
        assert out == 'https://site.com/v.mp4'
        assert called == []          # обычные URL не качаем — Telegram сам умеет

    def test_none_in_none_out(self):
        assert asyncio.run(anime_news_bot._resolve_video(None)) is None


class TestVideoBytesInSending:
    def _bot(self):
        bot = MagicMock()
        for m in ('send_photo', 'send_video', 'send_media_group', 'send_message'):
            setattr(bot, m, AsyncMock())
        return bot

    def _settings(self, monkeypatch, video=True):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=video, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Тизер.')
        monkeypatch.setattr(anime_news_bot, 'format_news_post', lambda n: 'Тизер.')

    def test_thread_video_only_sends_bytes(self, monkeypatch):
        self._settings(monkeypatch)
        _stream_patch(monkeypatch, lambda u: _FakeStream(b'MP4DATA'))
        monkeypatch.setattr(anime_news_bot, 'fetch_og_image', lambda u: None)
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/2', 'summary': '', 'images': [],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert bot.send_video.await_args.kwargs['video'] == b'MP4DATA'

    def test_thread_album_video_bytes(self, monkeypatch):
        self._settings(monkeypatch)
        _stream_patch(monkeypatch, lambda u: _FakeStream(b'MP4DATA'))
        monkeypatch.setattr(anime_news_bot, '_cached_image_bytes', lambda _url: b'JPG')
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/2', 'summary': '',
                'images': ['https://cdn4.cdn-telegram.org/file/p1.jpg',
                           'https://cdn4.cdn-telegram.org/file/p2.jpg'],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        from telegram import InputMediaVideo
        media = bot.send_media_group.await_args.kwargs['media']
        assert isinstance(media[0], InputMediaVideo)
        assert not isinstance(media[0].media, str)   # байты (InputFile), не URL
        assert len(media) == 3

    def test_channel_video_bytes(self, monkeypatch):
        self._settings(monkeypatch)
        _stream_patch(monkeypatch, lambda u: _FakeStream(b'MP4DATA'))
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/3', 'summary': '', 'images': [],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post(bot, news, -100500, None))
        assert ok is True
        assert bot.send_video.await_args.kwargs['video'] == b'MP4DATA'

    def test_unavailable_video_no_cdn_link_in_text(self, monkeypatch):
        # Видео скачать не удалось → пост уходит без него и БЕЗ уродливой cdn-ссылки
        self._settings(monkeypatch)
        _stream_patch(monkeypatch, lambda u: None)
        monkeypatch.setattr(anime_news_bot, '_cached_image_bytes', lambda _url: b'JPG')
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/4', 'summary': '',
                'images': ['https://cdn4.cdn-telegram.org/file/p1.jpg'],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        caption = bot.send_photo.await_args.kwargs['caption']
        assert 'cdn-telegram' not in caption and 'Смотреть' not in caption

    def test_video_disabled_no_cdn_link(self, monkeypatch):
        self._settings(monkeypatch, video=False)
        img = MagicMock(status_code=200, content=b'JPG',
                        headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: img)
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://t.me/c/5', 'summary': '',
                'images': ['https://cdn4.cdn-telegram.org/file/p1.jpg'],
                'video': 'https://cdn4.cdn-telegram.org/file/v', 'source': 'TG', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        caption = bot.send_photo.await_args.kwargs['caption']
        assert 'cdn-telegram' not in caption

    def test_video_disabled_youtube_link_kept(self, monkeypatch):
        # А человеческие ссылки (YouTube) при выключенном видео — остаются
        self._settings(monkeypatch, video=False)
        img = MagicMock(status_code=200, content=b'JPG',
                        headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: img)
        bot = self._bot()
        news = {'title': 'V', 'link': 'https://site.com/a', 'summary': '',
                'images': ['https://site.com/p.jpg'],
                'video': 'https://youtube.com/watch?v=abc', 'source': 'X', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        caption = bot.send_photo.await_args.kwargs['caption']
        assert 'youtube.com/watch?v=abc' in caption
