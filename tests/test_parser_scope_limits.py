"""Тесты: изоляция постов в TG-парсере, лимит видео 5 минут,
потоковое скачивание и лимит действий гостей."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot


def _iso(hours_ago: float) -> str:
    """Метка времени N часов назад — чтобы тесты не «протухали» назавтра."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


FRESH = _iso(1)      # свежий пост
STALE = _iso(24 * 30)  # месячной давности


# Разметка из прода: соседний пост оказался ВНУТРИ старого (незакрытый div
# в обёртке альбома). Раньше старый альбом публиковался с сегодняшним текстом.
NESTED_HTML = '''
<div class="tgme_widget_message" data-post="ch/100">
  <div class="tgme_widget_message_grouped_wrap">
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn/old1.jpg')"></a>
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn/old2.jpg')"></a>
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn/old3.jpg')"></a>
    <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn/old4.jpg')"></a>
    <div class="tgme_widget_message" data-post="ch/200">
      <video src="https://cdn/trailer.mp4"></video>
      <time class="tgme_widget_message_video_duration">1:43</time>
      <div class="tgme_widget_message_text">Нарисовался трейлер к продолжению Джоджо Стил Болл Ран</div>
      <time datetime="FRESH_TS"></time>
    </div>
  <div class="tgme_widget_message_text">Почему СБР выходит кусками старый пост от 4 июля</div>
  <time datetime="STALE_TS"></time>
  </div>
</div>
'''.replace('FRESH_TS', FRESH).replace('STALE_TS', STALE)


def _parse(html, monkeypatch, max_age=24):
    monkeypatch.setattr(anime_news_bot, 'settings',
                        MagicMock(post_max_age_hours=max_age))
    monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                        lambda *a, **k: MagicMock(status_code=200, text=html))
    return anime_news_bot.get_telegram_channel('ch', 'TG: Test')


class TestPostIsolation:
    def test_no_cross_post_leak(self, monkeypatch):
        posts = _parse(NESTED_HTML, monkeypatch)
        assert len(posts) == 1                      # старый отсеян по возрасту
        p = posts[0]
        assert 'трейлер' in p['title']
        assert p['images'] == []                    # чужой альбом не прилип
        assert p['video'] == 'https://cdn/trailer.mp4'

    def test_old_post_not_resurrected_by_neighbour_date(self, monkeypatch):
        # Раньше старый пост брал дату соседа и обходил фильтр 24ч
        posts = _parse(NESTED_HTML, monkeypatch)
        assert all('кусками' not in p['title'] for p in posts)

    def test_normal_markup_unaffected(self, monkeypatch):
        html = ('<div class="tgme_widget_message" data-post="ch/300">'
                '<a class="tgme_widget_message_photo_wrap" '
                'style="background-image:url(\'https://cdn/a.jpg\')"></a>'
                '<a class="tgme_widget_message_photo_wrap" '
                'style="background-image:url(\'https://cdn/b.jpg\')"></a>'
                '<div class="tgme_widget_message_text">'
                'Обычный свежий пост с двумя картинками тут</div>'
                '<time datetime="' + FRESH + '"></time></div>')
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        posts = _parse(html, monkeypatch)
        assert len(posts) == 1 and len(posts[0]['images']) == 2

    def test_post_without_own_date_skipped(self, monkeypatch):
        html = ('<div class="tgme_widget_message" data-post="ch/400">'
                '<div class="tgme_widget_message_text">'
                'Пост без даты в разметке совсем никакой</div></div>')
        assert _parse(html, monkeypatch) == []

    def test_duplicate_post_id_taken_once(self, monkeypatch):
        one = ('<div class="tgme_widget_message" data-post="ch/500">'
               '<div class="tgme_widget_message_text">'
               'Один и тот же пост встретился дважды в разметке</div>'
               '<time datetime="' + FRESH + '"></time></div>')
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        assert len(_parse(one + one, monkeypatch)) == 1

    def test_msg_own_scopes_selection(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(NESTED_HTML, 'html.parser')
        outer = soup.select('div.tgme_widget_message')[0]
        own_text = anime_news_bot._msg_own_one(outer, 'div.tgme_widget_message_text')
        assert 'кусками' in own_text.get_text()      # свой текст, не соседа


class TestVideoDurationLimit:
    @pytest.mark.parametrize('dur,taken', [
        ('0:36', True), ('1:43', True), ('4:59', True),
        ('5:00', True), ('5:01', False), ('12:30', False),
    ])
    def test_five_minute_cap(self, dur, taken, monkeypatch):
        posts = _parse(NESTED_HTML.replace('1:43', dur), monkeypatch)
        assert bool(posts[0]['video']) is taken

    def test_constant_is_five_minutes(self):
        assert anime_news_bot.TG_VIDEO_MAX_SECONDS == 300


class _FakeStream:
    def __init__(self, content=b'', ctype='video/mp4', status=200, declared=None):
        self.content = content
        self.status_code = status
        self.headers = {'Content-Type': ctype,
                        'Content-Length': declared or str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class TestStreamingDownload:
    """5-минутное видео может весить сотни МБ — в память целиком читать нельзя."""

    @pytest.fixture(autouse=True)
    def _no_dns_dependency(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_is_public_http_url', lambda _url: True)

    def test_downloads_within_limit(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot.requests, 'get',
                            lambda *a, **k: _FakeStream(b'MP4'))
        assert anime_news_bot._download_media_bytes('http://x/v.mp4') == b'MP4'

    def test_rejects_by_content_length_without_downloading(self, monkeypatch):
        huge = _FakeStream(b'x', declared=str(200 * 1024 * 1024))
        pulled = []
        huge.iter_content = lambda chunk_size=1: pulled.append(1) or iter([])
        monkeypatch.setattr(anime_news_bot.requests, 'get', lambda *a, **k: huge)
        assert anime_news_bot._download_media_bytes('http://x/v.mp4', 48) is None
        assert pulled == []            # тело даже не начали читать

    def test_aborts_when_content_length_lies(self, monkeypatch):
        lying = _FakeStream(b'y' * (3 * 1024 * 1024), declared='10')
        monkeypatch.setattr(anime_news_bot.requests, 'get', lambda *a, **k: lying)
        assert anime_news_bot._download_media_bytes('http://x/v.mp4', 1) is None

    def test_wrong_content_type_rejected(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot.requests, 'get',
                            lambda *a, **k: _FakeStream(b'<html>', ctype='text/html'))
        assert anime_news_bot._download_media_bytes('http://x/v.mp4') is None

    def test_network_error_is_none(self, monkeypatch):
        def boom(*a, **k):
            raise OSError('down')
        monkeypatch.setattr(anime_news_bot.requests, 'get', boom)
        assert anime_news_bot._download_media_bytes('http://x/v.mp4') is None


class TestGuestRateLimit:
    def test_caps_actions_per_hour(self):
        anime_news_bot._guest_actions.clear()
        allowed = sum(1 for _ in range(15) if anime_news_bot._guest_rate_ok(777))
        assert allowed == anime_news_bot.GUEST_ACTIONS_PER_HOUR

    def test_limit_is_per_user(self):
        anime_news_bot._guest_actions.clear()
        for _ in range(anime_news_bot.GUEST_ACTIONS_PER_HOUR):
            anime_news_bot._guest_rate_ok(777)
        assert anime_news_bot._guest_rate_ok(777) is False
        assert anime_news_bot._guest_rate_ok(888) is True

    def test_old_hits_expire(self):
        anime_news_bot._guest_actions.clear()
        anime_news_bot._guest_actions[999] = [time.time() - 4000] * 50
        assert anime_news_bot._guest_rate_ok(999) is True


class TestConcurrentCollection:
    def test_sources_fetched_concurrently_in_order(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock
        order = []

        def make(name):
            def fn():
                time.sleep(0.05)
                order.append(name)
                return [{'title': name, 'link': f'http://x/{name}', 'images': ['i']}]
            return fn

        monkeypatch.setattr(anime_news_bot, 'SOURCES',
                            [(f'S{i}', make(f'S{i}')) for i in range(6)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(is_source_enabled=lambda n: True,
                                      require_image=False))
        monkeypatch.setattr(anime_news_bot, 'stats',
                            MagicMock(record_collected=AsyncMock(),
                                      record_skipped=AsyncMock(),
                                      record_source_error=AsyncMock()))
        t0 = time.perf_counter()
        news, _lines, errors = asyncio.run(anime_news_bot.collect_all_news())
        elapsed = time.perf_counter() - t0
        assert errors == []
        assert elapsed < 6 * 0.05          # быстрее последовательного
        # порядок результатов — как в SOURCES, независимо от порядка ответов
        assert [n['title'] for n in news] == [f'S{i}' for i in range(6)]

    def test_source_error_does_not_break_others(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock

        def boom():
            raise RuntimeError('источник упал')

        def ok():
            return [{'title': 'ok', 'link': 'http://x/ok', 'images': ['i']}]

        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('Bad', boom), ('Good', ok)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(is_source_enabled=lambda n: True,
                                      require_image=False))
        monkeypatch.setattr(anime_news_bot, 'stats',
                            MagicMock(record_collected=AsyncMock(),
                                      record_skipped=AsyncMock(),
                                      record_source_error=AsyncMock()))
        news, _lines, errors = asyncio.run(anime_news_bot.collect_all_news())
        assert len(news) == 1 and news[0]['title'] == 'ok'
        assert any('Bad' in e for e in errors)


class TestVideoExtractionFallback:
    """Telegram часто не отдаёт mp4 в ленте — пробуем страницу самого поста."""

    TXT = ('<div class="tgme_widget_message_text">'
           'Нарисовался свежий тизер к финальной части сериала</div>'
           '<time datetime="' + FRESH + '"></time>')
    LAZY = ('<a class="tgme_widget_message_video_thumb" '
            'style="background-image:url(\'https://cdn/frame.jpg\')"></a>')

    def _parse(self, html, monkeypatch, embed=(None, None, None)):
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_fetch_video_from_embed',
                            lambda pid: embed)
        return anime_news_bot.get_telegram_channel('ch', 'TG: T')

    def _msg(self, inner, post='ch/1'):
        return (f'<div class="tgme_widget_message" data-post="{post}">'
                f'{self.TXT}{inner}</div>')

    def test_direct_mp4_used(self, monkeypatch):
        posts = self._parse(self._msg(
            '<video src="https://cdn/t.mp4"></video>'
            '<time class="tgme_widget_message_video_duration">0:46</time>'), monkeypatch)
        assert posts[0]['video'] == 'https://cdn/t.mp4'
        assert '46' in posts[0]['_video_note']

    def test_embed_rescues_lazy_video(self, monkeypatch):
        posts = self._parse(
            self._msg(self.LAZY + '<time class="tgme_widget_message_video_duration">0:46</time>'),
            monkeypatch, embed=('https://cdn/embed.mp4', 46, None))
        assert posts[0]['video'] == 'https://cdn/embed.mp4'
        assert posts[0]['_thumb_only'] is False
        assert 'страницы поста' in posts[0]['_video_note']

    def test_embed_result_respects_duration_limit(self, monkeypatch):
        posts = self._parse(
            self._msg(self.LAZY), monkeypatch,
            embed=('https://cdn/long.mp4', anime_news_bot.TG_VIDEO_MAX_SECONDS + 60, None))
        assert posts[0]['video'] is None
        assert 'длиннее лимита' in posts[0]['_video_note']

    def test_falls_back_to_frame(self, monkeypatch):
        posts = self._parse(
            self._msg(self.LAZY + '<time class="tgme_widget_message_video_duration">0:46</time>'),
            monkeypatch, embed=(None, None, None))
        assert posts[0]['video'] is None
        assert posts[0]['images'] == ['https://cdn/frame.jpg']
        assert posts[0]['_thumb_only'] is True
        assert 'не отдал mp4' in posts[0]['_video_note']

    def test_long_video_keeps_frame(self, monkeypatch):
        posts = self._parse(self._msg(
            '<video src="https://cdn/long.mp4"></video>' + self.LAZY +
            '<time class="tgme_widget_message_video_duration">7:20</time>'), monkeypatch)
        assert posts[0]['video'] is None
        assert posts[0]['images'] == ['https://cdn/frame.jpg']
        assert 'длиннее лимита' in posts[0]['_video_note']

    def test_spare_frame_saved_even_with_video(self, monkeypatch):
        posts = self._parse(self._msg(
            '<video src="https://cdn/t.mp4"></video>' + self.LAZY +
            '<time class="tgme_widget_message_video_duration">0:46</time>'), monkeypatch)
        assert posts[0]['video'] == 'https://cdn/t.mp4'
        assert posts[0]['_video_thumb'] == 'https://cdn/frame.jpg'   # про запас
        assert posts[0]['images'] == []                              # но не в альбоме

    def test_embed_budget_limited(self, monkeypatch):
        """Лишние запросы не должны тормозить обход канала."""
        calls = []
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        html = ''.join(self._msg(self.LAZY, post=f'ch/{i}') for i in range(10))
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_fetch_video_from_embed',
                            lambda pid: calls.append(pid) or (None, None, None))
        anime_news_bot.get_telegram_channel('ch', 'TG: T')
        assert len(calls) == anime_news_bot.TG_EMBED_LOOKUPS_PER_RUN


class TestParseDuration:
    @pytest.mark.parametrize('text,secs', [
        ('0:46', 46), ('1:43', 103), ('7:20', 440), ('1:02:30', 3750), ('12', 12),
    ])
    def test_parses(self, text, secs):
        assert anime_news_bot._parse_duration(text) == secs

    @pytest.mark.parametrize('text', ['', 'abc', None, 'x:y'])
    def test_garbage(self, text):
        assert anime_news_bot._parse_duration(text) is None


class TestFetchVideoFromEmbed:
    def test_extracts_video_and_duration(self, monkeypatch):
        html = ('<div class="tgme_widget_message">'
                '<video src="https://cdn/embed.mp4"></video>'
                '<time class="tgme_widget_message_video_duration">0:46</time></div>')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        url, dur, _thumb = anime_news_bot._fetch_video_from_embed('ch/1')
        assert (url, dur) == ('https://cdn/embed.mp4', 46)

    def test_source_tag_variant(self, monkeypatch):
        html = '<video><source src="https://cdn/s.mp4"></video>'
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        assert anime_news_bot._fetch_video_from_embed('ch/1')[0] == 'https://cdn/s.mp4'

    def test_no_video_on_page(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text='<div></div>'))
        assert anime_news_bot._fetch_video_from_embed('ch/1')[:2] == (None, None)

    def test_http_error(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: None)
        assert anime_news_bot._fetch_video_from_embed('ch/1')[:2] == (None, None)

    def test_network_exception(self, monkeypatch):
        def boom(*a, **k):
            raise OSError('down')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', boom)
        assert anime_news_bot._fetch_video_from_embed('ch/1')[:2] == (None, None)


class TestSpareFrameOnVideoFailure:
    """Ролик не скачался → в посте должен быть кадр из него, а не случайная og:image."""

    def _bot(self):
        bot = MagicMock()
        for m in ('send_photo', 'send_video', 'send_media_group', 'send_message'):
            setattr(bot, m, AsyncMock())
        return bot

    def test_thread_uses_spare_frame(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'Тизер.')
        monkeypatch.setattr(anime_news_bot, '_resolve_video', AsyncMock(return_value=None))
        og_called = []
        monkeypatch.setattr(anime_news_bot, 'fetch_og_image',
                            lambda u: og_called.append(u) or 'https://site/og.jpg')
        bot = self._bot()
        news = {'title': 'Тизер', 'link': 'https://t.me/c/9', 'summary': '', 'lang': 'ru',
                'images': [], 'video': 'https://cdn4.cdn-telegram.org/file/v',
                '_video_thumb': 'https://cdn/frame.jpg', 'source': 'TG'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert bot.send_photo.await_args.kwargs['photo'] == 'https://cdn/frame.jpg'
        assert og_called == []          # og:image даже не понадобился

    def test_channel_uses_spare_frame(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'format_news_post', lambda n: 'Тизер.')
        monkeypatch.setattr(anime_news_bot, '_resolve_video', AsyncMock(return_value=None))
        monkeypatch.setattr(anime_news_bot, '_resolve_photos_for_album',
                            AsyncMock(side_effect=lambda p: p))
        bot = self._bot()
        news = {'title': 'Тизер', 'link': 'https://t.me/c/9', 'summary': '', 'lang': 'ru',
                'images': [], 'video': 'https://cdn4.cdn-telegram.org/file/v',
                '_video_thumb': 'https://cdn/frame.jpg', 'source': 'TG'}
        ok = asyncio.run(anime_news_bot._send_post(bot, news, -100500, None))
        assert ok is True
        assert bot.send_photo.await_args.kwargs['photo'] == 'https://cdn/frame.jpg'


class TestVideoUrlExtraction:
    """Три способа найти mp4 на странице поста."""

    MSG = ('<div class="tgme_widget_message">'
           '<div class="tgme_widget_message_text">пост</div>')
    DUR = '<time class="tgme_widget_message_video_duration">0:46</time>'

    def test_video_tag(self):
        url, dur, how, _thumb = anime_news_bot._extract_video_url(
            self.MSG + '<video src="https://cdn/a.mp4"></video>' + self.DUR + '</div>')
        assert url == 'https://cdn/a.mp4' and dur == 46 and how == 'тег video'

    def test_source_tag(self):
        url = anime_news_bot._extract_video_url(
            self.MSG + '<video><source src="https://cdn/b.mp4"></video></div>')[0]
        assert url == 'https://cdn/b.mp4'

    @pytest.mark.parametrize('prop', ['og:video', 'og:video:url', 'og:video:secure_url'])
    def test_og_video_meta(self, prop):
        url, _, how, _t = anime_news_bot._extract_video_url(
            self.MSG + f'<meta property="{prop}" content="https://cdn/c.mp4">' + '</div>')
        assert url == 'https://cdn/c.mp4' and 'мета' in how

    def test_twitter_player_meta(self):
        url = anime_news_bot._extract_video_url(
            self.MSG + '<meta name="twitter:player:stream" content="https://cdn/d.mp4"></div>')[0]
        assert url == 'https://cdn/d.mp4'

    def test_url_inside_script(self):
        url, _, how, _t = anime_news_bot._extract_video_url(
            self.MSG + '<script>var v="https://cdn7.cdn-telegram.org/file/e.mp4?t=1";</script></div>')
        assert 'e.mp4' in url
        assert how == 'ссылка в коде страницы'

    def test_nothing_found(self):
        url, _, how, _t = anime_news_bot._extract_video_url(self.MSG + '</div>')
        assert url is None and how == ''

    def test_no_false_positive_on_plain_page(self):
        url = anime_news_bot._extract_video_url(
            '<html><body>обычная страница без видео</body></html>')[0]
        assert url is None


class TestEmbedFetchDiagnostics:
    """Лог должен различать: страницы нет, пост закрыт, файла в HTML нет."""

    MSG = '<div class="tgme_widget_message">пост</div>'

    def _run(self, monkeypatch, responses):
        logs = []
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            MagicMock(side_effect=responses))
        monkeypatch.setattr(anime_news_bot.logger, 'info',
                            lambda m: logs.append(m))
        result = anime_news_bot._fetch_video_from_embed('ch/1')
        return result[:2], logs

    def test_second_url_rescues(self, monkeypatch):
        res, logs = self._run(monkeypatch, [
            MagicMock(status_code=200, text=self.MSG),
            MagicMock(status_code=200,
                      text=self.MSG + '<meta property="og:video" content="https://cdn/x.mp4">'),
        ])
        assert res[0] == 'https://cdn/x.mp4'
        assert any('нашёл mp4' in l for l in logs)

    def test_protected_post_reported(self, monkeypatch):
        res, logs = self._run(monkeypatch,
                              [MagicMock(status_code=200, text='<html>nope</html>')] * 2)
        assert res == (None, None)
        assert any('приватный/защищённый' in l for l in logs)

    def test_page_without_file_reported(self, monkeypatch):
        res, logs = self._run(monkeypatch,
                              [MagicMock(status_code=200, text=self.MSG)] * 2)
        assert res == (None, None)
        assert any('ссылки на файл в HTML не оказалось' in l for l in logs)

    def test_http_error_reported(self, monkeypatch):
        res, logs = self._run(monkeypatch,
                              [MagicMock(status_code=403, text=''), None])
        assert res == (None, None)
        assert any('HTTP 403' in l for l in logs)

    def test_exception_reported(self, monkeypatch):
        res, logs = self._run(monkeypatch, [OSError('down'), OSError('down')])
        assert res == (None, None)
        assert any('запрос не удался' in l for l in logs)

    def test_tries_both_urls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda url, **k: calls.append(url) or MagicMock(
                                status_code=200, text=self.MSG))
        anime_news_bot._fetch_video_from_embed('ch/1')
        assert len(calls) == 2
        assert 'embed=1' in calls[0] and 'embed' not in calls[1]


class TestVideoMarkupVariants:
    """У t.me/s/ разметка видео отличается от канала к каналу. Жёсткий список
    классов пропускал видео-посты — детект должен быть по признаку."""

    TXT = ('<div class="tgme_widget_message_text">'
           'Опубликован опенинг к третьему сезону сериала</div>'
           f'<time datetime="{FRESH}"></time>')

    def _parse(self, inner, monkeypatch):
        html = (f'<div class="tgme_widget_message" data-post="ch/1">'
                f'{self.TXT}{inner}</div>')
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_fetch_video_from_embed',
                            lambda pid: (None, None, None))
        return anime_news_bot.get_telegram_channel('ch', 'T')[0]

    @pytest.mark.parametrize('inner,has_mp4', [
        ('<div class="tgme_widget_message_grouped_wrap">'
         '<div class="tgme_widget_message_video_wrap">'
         '<video src="https://cdn/op.mp4"></video></div></div>', True),
        ('<div class="tgme_widget_message_video_wrap" '
         'style="background-image:url(\'https://cdn/frame.jpg\')"></div>', False),
        ('<div class="tgme_widget_message_roundvideo_wrap">'
         '<video src="https://cdn/round.mp4"></video></div>', True),
        ('<video src="https://cdn/plain.mp4"></video>', True),
        ('<div class="js-message_video_wrap" '
         'style="background-image:url(\'https://cdn/f.jpg\')"></div>'
         '<time class="tgme_widget_message_video_duration">1:30</time>', False),
    ])
    def test_detected_as_video_post(self, inner, has_mp4, monkeypatch):
        post = self._parse(inner, monkeypatch)
        assert post['_video_note'], 'пост с видео должен распознаваться'
        assert bool(post['video']) is has_mp4

    def test_thumb_from_any_video_wrapper(self, monkeypatch):
        post = self._parse(
            '<div class="tgme_widget_message_video_wrap" '
            'style="background-image:url(\'https://cdn/frame.jpg\')"></div>', monkeypatch)
        assert post['_video_thumb'] == 'https://cdn/frame.jpg'
        assert post['images'] == ['https://cdn/frame.jpg']
        assert post['_thumb_only'] is True

    def test_plain_photo_post_not_video(self, monkeypatch):
        post = self._parse(
            '<a class="tgme_widget_message_photo_wrap" '
            'style="background-image:url(\'https://cdn/p.jpg\')"></a>', monkeypatch)
        assert not post['_video_note']          # без ложных срабатываний
        assert post['images'] == ['https://cdn/p.jpg']

    def test_text_only_post_not_video(self, monkeypatch):
        post = self._parse('', monkeypatch)
        assert not post['_video_note']


class TestFullSizeThumbFromPostPage:
    """В ленте t.me/s/ превью видео — размытая заглушка. Полноразмерный кадр
    лежит на странице поста, и забирать его надо тем же запросом."""

    FEED = (f'<div class="tgme_widget_message" data-post="ch/22355">'
            '<div class="tgme_widget_message_text">Последний опенинг сериала '
            'выйдет уже сегодня вечером</div>'
            '<div class="tgme_widget_message_video_thumb" '
            'style="background-image:url(\'https://cdn/blurred.jpg\')"></div>'
            '<time class="tgme_widget_message_video_duration">1:30</time>'
            f'<time datetime="{FRESH}"></time></div>')

    def _parse(self, page_html, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        monkeypatch.setattr(
            anime_news_bot, 'http_get_public_with_retry',
            lambda url, **k: MagicMock(status_code=200,
                                       text=self.FEED if '/s/' in url else page_html))
        return anime_news_bot.get_telegram_channel('ch', 'T')[0]

    def test_og_image_replaces_blurred_thumb(self, monkeypatch):
        page = ('<div class="tgme_widget_message">'
                '<meta property="og:image" content="https://cdn/full.jpg"></div>')
        post = self._parse(page, monkeypatch)
        assert post['images'] == ['https://cdn/full.jpg']
        assert 'blurred' not in str(post['images'])
        assert post['_thumb_only'] is True

    def test_twitter_image_also_works(self, monkeypatch):
        page = ('<div class="tgme_widget_message">'
                '<meta name="twitter:image" content="https://cdn/tw.jpg"></div>')
        assert self._parse(page, monkeypatch)['images'] == ['https://cdn/tw.jpg']

    def test_background_image_fallback(self, monkeypatch):
        page = ('<div class="tgme_widget_message">'
                '<div class="tgme_widget_message_video_thumb" '
                'style="background-image:url(\'https://cdn/big.jpg\')"></div></div>')
        assert self._parse(page, monkeypatch)['images'] == ['https://cdn/big.jpg']

    def test_video_wins_over_thumb(self, monkeypatch):
        page = ('<div class="tgme_widget_message">'
                '<meta property="og:image" content="https://cdn/full.jpg">'
                '<video src="https://cdn/op.mp4"></video></div>')
        post = self._parse(page, monkeypatch)
        assert post['video'] == 'https://cdn/op.mp4'
        assert post['images'] == []          # кадр в альбом не идёт

    def test_keeps_feed_thumb_if_page_has_none(self, monkeypatch):
        post = self._parse('<div class="tgme_widget_message">пусто</div>', monkeypatch)
        assert post['images'] == ['https://cdn/blurred.jpg']   # хоть что-то

    def test_extract_returns_thumb(self):
        _url, _dur, _how, thumb = anime_news_bot._extract_video_url(
            '<meta property="og:image" content="https://cdn/x.jpg">')
        assert thumb == 'https://cdn/x.jpg'

    def test_telesco_pe_link_matched(self):
        html = ('<script>var v="https://cdn4.telesco.pe/file/abc.mp4?token=XYZ";</script>')
        url, _d, how, _t = anime_news_bot._extract_video_url(html)
        assert url.startswith('https://cdn4.telesco.pe/file/abc.mp4')
        assert how == 'ссылка в коде страницы'


class TestYtDlpFallback:
    """Последняя ступень добычи видео: yt-dlp с экстрактором telegram:embed.
    Он достаёт ссылку там, где её нет в HTML страницы."""

    def _feed(self, dur='1:30'):
        return ('<div class="tgme_widget_message" data-post="ch/1">'
                '<div class="tgme_widget_message_text">'
                'Опенинг сериала выйдет уже сегодня вечером</div>'
                '<div class="tgme_widget_message_video_thumb" '
                'style="background-image:url(\'https://cdn/blur.jpg\')"></div>'
                + (f'<time class="tgme_widget_message_video_duration">{dur}</time>'
                   if dur else '')
                + f'<time datetime="{FRESH}"></time></div>')

    def _parse(self, monkeypatch, ytdlp, dur='1:30'):
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)
        monkeypatch.setattr(
            anime_news_bot, 'http_get_public_with_retry',
            lambda url, **k: MagicMock(
                status_code=200,
                text=self._feed(dur) if '/s/' in url
                else '<div class="tgme_widget_message">пусто</div>'))
        monkeypatch.setattr(anime_news_bot, '_ytdlp_telegram_video', lambda pid: ytdlp)
        return anime_news_bot.get_telegram_channel('ch', 'T')[0]

    def test_rescues_video(self, monkeypatch):
        post = self._parse(monkeypatch,
                           ('https://cdn4.telesco.pe/file/op.mp4?token=X', 90, None))
        assert post['video'].startswith('https://cdn4.telesco.pe')

    def test_thumb_from_ytdlp(self, monkeypatch):
        post = self._parse(monkeypatch, (None, None, 'https://cdn/full.jpg'))
        assert post['images'] == ['https://cdn/full.jpg']

    def test_falls_back_to_frame(self, monkeypatch):
        post = self._parse(monkeypatch, (None, None, None))
        assert post['video'] is None
        assert post['images'] == ['https://cdn/blur.jpg']

    @pytest.mark.parametrize('yt_dur,feed_dur,taken', [
        (90, '1:30', True),
        (999, '1:30', False),        # yt-dlp знает, что ролик длинный
        (90, '20:00', False),        # лента знает
        (None, '', True),            # длительность неизвестна — берём
        (120, '', True),             # знает только yt-dlp
    ])
    def test_duration_limit_from_both_sources(self, yt_dur, feed_dur, taken, monkeypatch):
        post = self._parse(monkeypatch, ('https://cdn/a.mp4', yt_dur, None), feed_dur)
        assert bool(post['video']) is taken

    def test_silent_without_ytdlp(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', False)
        assert anime_news_bot._ytdlp_telegram_video('ch/1') == (None, None, None)

    def test_exception_is_safe(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)

        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, *a, **k):
                raise RuntimeError('yt-dlp упал')
        monkeypatch.setattr(anime_news_bot, 'yt_dlp',
                            MagicMock(YoutubeDL=lambda opts: Boom()))
        assert anime_news_bot._ytdlp_telegram_video('ch/1') == (None, None, None)

    def test_playlist_takes_first(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, *a, **k):
                return {'_type': 'playlist',
                        'entries': [{'url': 'https://cdn/first.mp4', 'duration': 40},
                                    {'url': 'https://cdn/second.mp4'}]}
        monkeypatch.setattr(anime_news_bot, 'yt_dlp',
                            MagicMock(YoutubeDL=lambda opts: Fake()))
        url, dur, _t = anime_news_bot._ytdlp_telegram_video('ch/1')
        assert url == 'https://cdn/first.mp4' and dur == 40

    def test_url_from_formats(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, *a, **k):
                return {'formats': [{'url': 'https://cdn/low.mp4'},
                                    {'url': 'https://cdn/best.mp4'}]}
        monkeypatch.setattr(anime_news_bot, 'yt_dlp',
                            MagicMock(YoutubeDL=lambda opts: Fake()))
        assert anime_news_bot._ytdlp_telegram_video('ch/1')[0] == 'https://cdn/best.mp4'
