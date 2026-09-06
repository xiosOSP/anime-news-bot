"""Тесты догрузки полного текста статьи со страницы."""
import pytest

import anime_news_bot
from anime_news_bot import fetch_full_article_text, enrich_summary_from_page


FAKE_HTML = """
<html>
<head><meta property="og:description" content="Short desc"></head>
<body>
  <nav>Menu Home About Contact</nav>
  <article>
    <h1>Article Title</h1>
    <p>The continuation of the Seido vs Inashiro match is finally here after a long wait.</p>
    <p>The new visual showcases the second-year students with detailed artwork and personality.</p>
    <p>Source: Official Twitter</p>
    <figure><figcaption>Image credit: Production IG</figcaption></figure>
  </article>
  <footer>Copyright 2026 All rights reserved</footer>
</body>
</html>
"""


class FakeResp:
    status_code = 200
    text = FAKE_HTML


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(anime_news_bot, '_article_cache', {})
    monkeypatch.setattr(anime_news_bot, '_article_text_cache', {})


class TestFetchArticle:
    def test_extracts_paragraphs(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda url, **kw: FakeResp())
        text = fetch_full_article_text('https://example.com/1')
        assert text is not None
        assert 'Seido' in text
        assert 'second-year' in text

    def test_filters_junk(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda url, **kw: FakeResp())
        text = fetch_full_article_text('https://example.com/1')
        assert 'Source:' not in text
        assert 'Image credit' not in text
        assert 'Menu' not in text
        assert 'Copyright' not in text

    def test_returns_none_on_http_fail(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda url, **kw: None)
        assert fetch_full_article_text('https://example.com/1') is None

    def test_empty_url(self):
        assert fetch_full_article_text('') is None

    def test_caches_result(self, monkeypatch):
        calls = [0]
        def counting(url, **kw):
            calls[0] += 1
            return FakeResp()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', counting)
        fetch_full_article_text('https://example.com/cached')
        fetch_full_article_text('https://example.com/cached')
        assert calls[0] == 1  # второй раз из кеша


class TestEnrichSummary:
    def test_short_summary_enriched(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'fetch_full_article_text',
                            lambda url: 'Long full article text ' * 10)
        news = {'summary': 'Short...', 'link': 'https://example.com/1'}
        enrich_summary_from_page(news)
        assert len(news['summary']) > 100

    def test_long_summary_not_touched(self, monkeypatch):
        # Если RSS уже длинный — на страницу не лезем
        called = [False]
        def should_not_call(url):
            called[0] = True
            return 'x'
        monkeypatch.setattr(anime_news_bot, 'fetch_full_article_text', should_not_call)
        long_text = 'This RSS already has plenty of content here. ' * 20
        news = {'summary': long_text, 'link': 'https://example.com/2'}
        enrich_summary_from_page(news)
        assert called[0] is False
        assert news['summary'] == long_text

    def test_no_link_no_crash(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'fetch_full_article_text', lambda url: 'x')
        news = {'summary': 'short', 'link': None}
        enrich_summary_from_page(news)  # не должно падать

    def test_fetch_returns_none_keeps_original(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'fetch_full_article_text', lambda url: None)
        news = {'summary': 'short', 'link': 'https://example.com/3'}
        enrich_summary_from_page(news)
        assert news['summary'] == 'short'  # осталось как было
