"""Тесты: байтовый фолбэк TG-картинок + источник Filmix."""
import asyncio
from unittest.mock import AsyncMock, MagicMock


from conftest import FakeHTTPResponse
from telegram.error import TelegramError

import anime_news_bot
from anime_news_bot import _download_image_bytes, get_filmix

FILMIX_HTML = '''<html><body>
<a href="/mnews/185592-v-seti.html"><img src="/uploads/post_185592_thumb.jpg"></a>
<h2><a href="/mnews/185592-v-seti.html">В сети появился трейлер нового сезона</a></h2>
<div class="meta">Вчера, 13:16</div>
<p>Студия показала первый полноценный трейлер продолжения культового сериала, назвав дату.</p>
<h2><a href="/mnews/185593-vtoraya.html">Вторая новость про другой фильм тут</a></h2>
</body></html>'''


class TestDownloadImageBytes:
    def test_ok_image(self, monkeypatch):
        resp = FakeHTTPResponse(b'JPG', headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: resp)
        assert _download_image_bytes('http://x/a.jpg') == b'JPG'

    def test_non_image_rejected(self, monkeypatch):
        resp = FakeHTTPResponse(b'<html>', headers={'Content-Type': 'text/html'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: resp)
        assert _download_image_bytes('http://x/a.jpg') is None

    def test_oversize_rejected(self, monkeypatch):
        resp = FakeHTTPResponse(b'x' * (10 * 1024 * 1024),
                                headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: resp)
        assert _download_image_bytes('http://x/a.jpg') is None

    def test_http_fail(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: None)
        assert _download_image_bytes('http://x/a.jpg') is None


class TestBytesFallbackInThreadSend:
    def test_url_rejected_then_bytes_sent(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=False, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'txt')
        resp = FakeHTTPResponse(b'IMGBYTES', headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: resp)
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=[TelegramError('Wrong type'), None])
        bot.send_message = AsyncMock()
        news = {'title': 'T', 'link': 'https://t.me/ch/1', 'summary': '',
                'images': ['https://cdn4.telegram-cdn.org/file/a.jpg'],
                'video': None, 'source': 'TG: X', 'lang': 'ru'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert bot.send_photo.await_count == 2
        assert bot.send_photo.await_args.kwargs['photo'] == b'IMGBYTES'


class TestFilmix:
    def test_parse(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_with_retry',
                            lambda *a, **k: FakeHTTPResponse(text=FILMIX_HTML))
        posts = get_filmix()
        assert len(posts) == 2
        p = posts[0]
        assert p['link'] == 'https://filmix.gg/mnews/185592-v-seti.html'
        assert p['lang'] == 'ru'
        assert p['images'] == ['https://filmix.gg/uploads/post_185592_thumb.jpg']
        assert 'трейлер' in p['summary']

    def test_http_fail_empty(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_with_retry', lambda *a, **k: None)
        assert get_filmix() == []

    def test_in_sources(self):
        assert any(n == 'Filmix' for n, _ in anime_news_bot.SOURCES)
