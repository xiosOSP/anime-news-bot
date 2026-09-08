"""News extraction regressions: source boundaries, text structure and card order."""
import re

import pytest

import anime_news_bot as bot
from news_parser import clean_html_fragment, extract_article_text, parse_listing_html


@pytest.mark.parametrize('raw, expected', [
    ('<p>Первый сезон.</p><p>Второй сезон.</p>', 'Первый сезон. Второй сезон.'),
    ('Дата:<br>12 октября<ul><li>Студия: Bones</li><li>Режиссёр: Ито</li></ul>',
     'Дата: 12 октября Студия: Bones Режиссёр: Ито'),
    ('<b>Fri</b>eren &amp; <i>friends</i>', 'Frieren & friends'),
    ('テス<strong>ト</strong>作品。<p>公開決定。</p>', 'テスト作品。 公開決定。'),
    ('<p>Анонс</p><!-- invisible --><script>evil()</script><p>сезона</p>', 'Анонс сезона'),
    ('Текст<template><div>не новость</div></template> после', 'Текст после'),
    ('Новость<script>unterminated evil()', 'Новость'),
    ('3 &lt; 4; &laquo;Тест&raquo;&#160;готов', '3 < 4; «Тест» готов'),
    ('<p>Новость</p><div><p>без закрывающего тега', 'Новость без закрывающего тега'),
])
def test_html_text_keeps_boundaries_without_changing_inline_words(raw, expected):
    assert clean_html_fragment(raw) == expected


def article(value, maximum=3500):
    return extract_article_text(value, max_chars=maximum, junk_pattern=bot._ARTICLE_JUNK)


def test_article_does_not_choose_long_page_wrapper_over_body():
    story = 'Студия Bones представила новый трейлер аниме и объявила актёрский состав.'
    other = 'Это текст другой новости, который не должен попадать в текущую публикацию. '
    page = f'<main><article><p>{story}</p></article><section><p>{other * 20}</p></section></main>'
    assert article(page) == story


def test_article_body_beats_repeated_related_news_and_keeps_facts_in_order():
    page = '''<article>
      <div itemprop="articleBody">
        <p>Премьера — 12 октября.</p><p>Анимация: Bones.</p>
        <ul><li>Музыка: Юки Кадзиура.</li><li>Режиссёр: Синъитиро Ватанабэ.</li></ul>
        <p>Премьера — 12 октября.</p>
      </div>
      <section class="related-posts"><p>Новая большая подборка других сериалов и фильмов.</p></section>
      <div class="comments"><p>Это пользовательский комментарий, а не факты.</p></div>
    </article>'''
    assert article(page) == (
        'Премьера — 12 октября. Анимация: Bones. Музыка: Юки Кадзиура. '
        'Режиссёр: Синъитиро Ватанабэ.'
    )


def test_article_excludes_link_navigation_and_duplicate_nested_paragraphs():
    page = '''<article>
      <p><a href="/other">Читайте новую большую подборку других популярных сериалов</a></p>
      <ul><li><p>Анимация: Studio Trigger.</p></li></ul>
      <p>Source: Studio Trigger official website and press release.</p>
    </article>'''
    assert article(page) == 'Анимация: Studio Trigger.'


def test_article_empty_explicit_container_falls_back_and_caps_output():
    text = 'Подтверждённая дата премьеры и актёрский состав. ' * 30
    page = f'<article></article><main><p>{text}</p></main>'
    assert article(page, maximum=120) == text[:120].strip()
    assert article(page, maximum=0) == ''


def listing(value, **options):
    return parse_listing_html(
        value, source_name='Example Anime', base_url='https://example.com/',
        href_pattern=r'/news/\d+/?$', limit=options.pop('limit', 5),
        normalize_image=bot._normalize_image_url, normalize_url=bot.normalize_url,
        **options,
    )


def test_listing_accepts_only_link_with_tracking_query_and_keeps_functional_query():
    rows = listing('''
      <article><a href="/news/1?utm_source=rss#headline">Anime Gets a New Trailer</a></article>
      <article><a href="/news/2?edition=jp">Second Anime Announces Cast</a></article>
    ''')
    assert [row['title'] for row in rows] == ['Anime Gets a New Trailer', 'Second Anime Announces Cast']
    assert rows[0]['link'] == 'https://example.com/news/1?utm_source=rss'
    assert rows[1]['link'] == 'https://example.com/news/2?edition=jp'


@pytest.mark.parametrize('href', [
    'https://other.example/news/1',
    'https://example.com.evil.test/news/1',
    'https://example.com@evil.test/news/1',
    'https://name:password@example.com/news/1',
    'https://example.com:8443/news/1',
    'https://example.com:invalid/news/1',
    'ftp://example.com/news/1',
    'javascript:alert(1)',
])
def test_listing_cannot_attribute_foreign_or_malformed_links_to_source(href):
    assert listing(f'<a href="{href}">New Official Anime Trailer</a>') == []


def test_listing_dedupes_before_applying_limit_and_preserves_discovery_order():
    rows = listing('''
      <a href="/news/2?utm_source=x">Second Anime Announces Cast</a>
      <a href="https://www.example.com/news/2/">Second Anime Announces Cast Again</a>
      <a href="/news/2#title">Second Anime Announces Cast Once Again</a>
      <a href="/news/1">First Anime Gets New Trailer</a>
      <a href="/news/3">Third Anime Announces Movie</a>
    ''', limit=2)
    assert [bot.normalize_url(row['link']) for row in rows] == [
        'https://example.com/news/2', 'https://example.com/news/1',
    ]


def test_listing_retains_distinct_functional_queries():
    rows = listing('''
      <a href="/news/1?edition=jp">Japanese Anime Announcement</a>
      <a href="/news/1?edition=en">English Anime Announcement</a>
    ''')
    assert len(rows) == 2


def test_listing_finds_lazy_image_in_same_card_and_falls_back_to_src():
    rows = listing('''
      <article><a href="/news/1"><img src="data:image/gif;base64,AA==" data-src="/real.jpg"></a>
        <h2>First Anime Announces Cast</h2><p>Actual first summary.</p></article>
      <article><a href="/news/2">Second Anime Announces Cast</a>
        <img data-src="data:image/gif;base64,AA==" src="/second.jpg"><p>Second summary.</p></article>
    ''')
    assert rows[0]['images'] == ['https://example.com/real.jpg']
    assert rows[1]['images'] == ['https://example.com/second.jpg']
    assert rows[0]['summary'] == 'Actual first summary.'


def test_listing_filters_before_consuming_limit_and_keeps_lang():
    rows = listing('''
      <a href="/news/1">Unrelated Cinema Announcement</a>
      <a href="/news/2">Actual Anime Announcement</a>
    ''', title_keywords=('anime',), limit=1, lang='ja')
    assert len(rows) == 1 and rows[0]['lang'] == 'ja'
    assert rows[0]['link'].endswith('/news/2')
    assert listing('<a href="/news/1">Actual Anime Announcement</a>', limit=0) == []


def test_invalid_listing_pattern_is_nonfatal():
    assert parse_listing_html(
        '<p>Broken</p>', source_name='Example', base_url='https://example.com/',
        href_pattern='[', limit=5, normalize_image=lambda *_: None,
        normalize_url=lambda value: value,
    ) == []


def test_article_respects_custom_junk_filter():
    assert extract_article_text(
        '<article><p>Ignore this promoted post.</p><p>The anime premieres in October.</p></article>',
        max_chars=200, junk_pattern=re.compile('promoted'),
    ) == 'The anime premieres in October.'


def test_full_article_fallback_uses_cleaned_description_only_when_more_complete():
    page = '''<head><meta property="og:description"
      content="The anime premieres in October. &amp; New cast members were announced."></head>
      <article><p>Anime confirmed.</p></article>'''
    assert extract_article_text(
        page, max_chars=200, junk_pattern=bot._ARTICLE_JUNK, description_fallback_at=400,
    ) == 'The anime premieres in October. & New cast members were announced.'
    assert article(page) == 'Anime confirmed.'


def test_full_article_fallback_never_replaces_more_complete_article():
    page = '''<meta property="og:description" content="Short summary.">
      <article><p>The anime premieres in October with a new cast and director.</p></article>'''
    assert extract_article_text(
        page, max_chars=200, junk_pattern=bot._ARTICLE_JUNK, description_fallback_at=400,
    ) == 'The anime premieres in October with a new cast and director.'
