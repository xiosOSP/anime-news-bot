"""Тесты: XML-escape DeepL, санити-чек заголовка, японский, AnimateTimes, og при пустых."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import anime_news_bot
from anime_news_bot import (
    extract_release_date_from_text,
    _extract_first_sentence,
    get_animatetimes,
)

AT_HTML = '''<html><body>
<a href="/news/details.php?id=111"><img src="/upload/th1.jpg">news title one long enough</a>
<a href="/news/details.php?id=111">news title one long enough</a>
<a href="/news/details.php?id=222">second news title also long</a>
<a href="/news/details.php?id=333">短い</a>
</body></html>'''


class TestDeeplXmlEscape:
    def test_ampersand_and_angles_roundtrip(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        captured = {}
        def fake_post(url, data=None, headers=None, timeout=None):
            captured['text'] = data['text']
            m = MagicMock(status_code=200)
            m.json.return_value = {'translations': [{'text': 'A &amp; B &lt;t&gt; 〖0〗'}]}
            return m
        with patch('anime_news_bot.requests.post', side_effect=fake_post):
            out = anime_news_bot._deepl_translate('A & B <t> 〖0〗')
        assert '&amp;' in captured['text'] and '&lt;t&gt;' in captured['text']
        assert out == 'A & B <t> 〖0〗'


class TestTitleSanityCheck:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        class FakeAni:
            def lookup(self, q):
                return None
        monkeypatch.setattr(anime_news_bot, 'anilist', FakeAni())
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(translator_engine='google'))

    def test_truncated_translation_falls_back_to_original(self, monkeypatch):
        class Eater:
            def translate(self, text):
                return 'Netflix'
        monkeypatch.setattr(anime_news_bot, 'translator', Eater())
        news = {'title': 'ONE PIECE New Season Hits Netflix This Fall 2026',
                'summary': '', 'published_parsed': None}
        out = anime_news_bot.format_news_short(news)
        assert 'ONE PIECE New Season' in out

    def test_normal_short_title_kept(self, monkeypatch):
        class Ok:
            def translate(self, text):
                return 'Новый сезон'
        monkeypatch.setattr(anime_news_bot, 'translator', Ok())
        news = {'title': 'New Season Soon', 'summary': '', 'published_parsed': None}
        out = anime_news_bot.format_news_short(news)
        assert 'новый сезон' in out.lower()  # короткий оригинал — короткий перевод норм


class TestJapanese:
    def test_full_date(self):
        assert extract_release_date_from_text('2027年1月15日スタート') == '15 января 2027'

    def test_month_day(self):
        assert extract_release_date_from_text('『X』11月6日公開決定') == '6 ноября'

    def test_year_month(self):
        assert extract_release_date_from_text('2026年10月放送決定') == 'октябрь 2026'

    def test_sentence_boundary(self):
        s = _extract_first_sentence('『幻想』10月放送決定。追加声優発表も。')
        assert s == '『幻想』10月放送決定。'


class TestAnimateTimes:
    def test_parse_dedup_and_images(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=AT_HTML))
        posts = get_animatetimes()
        assert len(posts) == 2
        assert posts[0]['link'].endswith('id=111')
        assert posts[0]['images'] == ['https://www.animatetimes.com/upload/th1.jpg']
        assert posts[1]['images'] == []
        assert posts[0]['source'] == 'AnimateTimes(JP)'

    def test_http_fail_empty(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_with_retry', lambda *a, **k: None)
        assert get_animatetimes() == []

    def test_in_sources(self):
        assert any(n == 'AnimateTimes(JP)' for n, _ in anime_news_bot.SOURCES)


class TestOgFallbackForEmptyImages:
    def test_post_without_images_saved_by_og(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=False, require_image=True))
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'fetch_og_image',
                            lambda url: 'https://s.com/og.jpg')
        monkeypatch.setattr(anime_news_bot, 'format_news_text_long', lambda n: 'txt')
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        news = {'title': 'T', 'link': 'https://s.com/a', 'summary': '',
                'images': [], 'video': None, 'source': 'X'}
        ok = asyncio.run(anime_news_bot._send_post_thread_split(bot, news, None))
        assert ok is True
        assert bot.send_photo.await_args.kwargs['photo'] == 'https://s.com/og.jpg'
