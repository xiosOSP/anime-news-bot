"""Тесты Telegram-каналов как источников (t.me/s/ парсер) и lang='ru'."""
from unittest.mock import MagicMock

import pytest

import anime_news_bot
from anime_news_bot import get_telegram_channel

TG_HTML = '''<html><body>
<div class="tgme_widget_message" data-post="testch/101">
  <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.tg.org/a.jpg')"></a>
  <div class="tgme_widget_message_text">Заголовок первой новости
Описание новости с деталями и подробностями события.</div>
  <time datetime="2026-07-03T10:00:00+00:00"></time>
</div>
<div class="tgme_widget_message" data-post="testch/102">
  <div class="tgme_widget_message_text">Вторая новость без фото и с одной строкой текста тут.</div>
  <time datetime="2026-07-03T11:00:00+00:00"></time>
</div>
<div class="tgme_widget_message" data-post="testch/103">
  <div class="tgme_widget_message_text">ок</div>
</div>
</body></html>'''


@pytest.fixture
def tg_response(monkeypatch):
    fake = MagicMock(status_code=200, text=TG_HTML)
    monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: fake)
    monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)


class TestTelegramSource:
    def test_parses_posts(self, tg_response):
        posts = get_telegram_channel('testch', 'TG: Test')
        assert len(posts) == 2  # третий слишком короткий

    def test_fields(self, tg_response):
        p = get_telegram_channel('testch', 'TG: Test')[0]
        assert p['link'] == 'https://t.me/testch/101'
        assert p['title'] == 'Заголовок первой новости'
        assert 'Описание новости' in p['summary']
        assert p['images'] == ['https://cdn.tg.org/a.jpg']
        assert p['lang'] == 'ru'
        assert p['source'] == 'TG: Test'
        assert p['published_parsed'] is not None

    def test_short_posts_skipped(self, tg_response):
        posts = get_telegram_channel('testch', 'TG: Test')
        assert all(len(p['title']) >= 2 for p in posts)
        assert not any(p['link'].endswith('/103') for p in posts)

    def test_http_fail_empty(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: None)
        assert get_telegram_channel('x', 'TG: X') == []

    def test_channels_in_sources(self):
        labels = [s[0] for s in anime_news_bot.SOURCES]
        assert any(lb.startswith('TG:') for lb in labels)


class TestRussianLangSkipsTranslation:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.calls = []
        outer = self
        class SpyTr:
            def translate(self, text):
                outer.calls.append(text)
                return 'X:' + text
        class FakeAni:
            def lookup(self, q):
                return None
        monkeypatch.setattr(anime_news_bot, 'translator', SpyTr())
        monkeypatch.setattr(anime_news_bot, 'anilist', FakeAni())
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')

    def test_ru_not_translated(self):
        news = {'title': 'Русский заголовок', 'summary': 'Русское описание события.',
                'published_parsed': None, 'lang': 'ru'}
        out = anime_news_bot.format_news_short(news)
        assert 'X:' not in out and not self.calls
        assert 'Русский заголовок.' in out

    def test_en_still_translated(self):
        news = {'title': 'English title', 'summary': 'Something happened.',
                'published_parsed': None}
        out = anime_news_bot.format_news_short(news)
        assert 'X:' in out
