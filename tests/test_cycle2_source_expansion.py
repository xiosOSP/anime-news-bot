from types import SimpleNamespace

import anime_news_bot as bot


def _settings(enabled=None):
    enabled = set(enabled or [])
    return SimpleNamespace(is_source_enabled=lambda name: name in enabled)


def test_red_production_sources_are_disabled_on_fresh_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'ADMIN_ID', 123)
    s = bot.BotSettings(tmp_path / 'settings.json')
    for name in (
        'CBR Anime', 'MyAnimeList', 'ANN Newsroom', 'ANN Industry',
        'Anime Corner', 'Anime Herald', 'Crunchyroll', "Honey's Anime",
        'AnimeHunch', 'AnimateTimes(JP)', 'Collider', '/Film', 'Variety',
        'ComingSoon', 'Filmix', 'TG: CurrentAnime',
    ):
        assert s.is_source_enabled(name) is False
    assert s.is_source_enabled('ComicBook Anime') is True
    assert s.is_source_enabled('AnimeNewsNetwork') is True
    assert s.is_source_enabled('Polygon') is True


def test_second_cycle_sources_are_present():
    names = {name for name, _ in bot.SOURCES}
    assert {
        'Anime Trending', 'Otaku USA', 'Comic Natalie(JP)', 'Anime Limited',
        'Seven Seas', 'Yen Press', 'GKIDS',
    } <= names


def test_listing_parser_extracts_distinct_news_cards():
    html = '''
    <main>
      <article><h2><a href="/news/2026/08/08/example-one/">Example Anime Gets New Trailer</a></h2>
        <img src="/img/a.jpg"><p>New trailer and premiere date were revealed.</p></article>
      <article><a href="https://anitrendz.net/news/2026/08/08/example-two/">Second Anime Announces Cast</a></article>
      <a href="/about/">About us</a>
    </main>'''
    rows = bot._parse_listing_html(
        html,
        source_name='Anime Trending',
        base_url='https://anitrendz.net/',
        href_pattern=r'/news/20\d\d/\d\d/\d\d/[^/?#]+/?$',
    )
    assert [r['title'] for r in rows] == [
        'Example Anime Gets New Trailer', 'Second Anime Announces Cast'
    ]
    assert rows[0]['images'] == ['https://anitrendz.net/img/a.jpg']
    assert rows[0]['summary'].startswith('New trailer')


def test_listing_parser_handles_japanese_source_and_dedupes_links():
    html = '''
      <article><a href="/comic/news/680001">新作アニメ「テスト」2027年放送決定</a></article>
      <div><a href="https://natalie.mu/comic/news/680001?utm_source=x">新作アニメ「テスト」2027年放送決定</a></div>
    '''
    rows = bot._parse_listing_html(
        html,
        source_name='Comic Natalie(JP)',
        base_url='https://natalie.mu/',
        href_pattern=r'natalie\.mu/comic/news/\d+/?$',
        lang='ja',
    )
    assert len(rows) == 1
    assert rows[0]['lang'] == 'ja'
    assert rows[0]['link'] == 'https://natalie.mu/comic/news/680001'


def test_fetch_listing_source_closes_response(monkeypatch):
    class Resp:
        status_code = 200
        text = '<article><a href="/2026/08/08/new-title/">New Official Anime Release Announced</a></article>'
        content = text.encode()
        headers = {}
        closed = False
        def close(self): self.closed = True
    r = Resp()
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: r)
    monkeypatch.setattr(bot, '_read_limited_text', lambda response: response.text)
    rows = bot._fetch_listing_source(
        'https://gkids.com/author/gkids/', 'GKIDS',
        base_url='https://gkids.com/',
        href_pattern=r'gkids\.com/20\d\d/\d\d/\d\d/[^/?#]+/?$',
    )
    assert len(rows) == 1
    assert r.closed is True


def test_new_rss_sources_use_expected_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, '_parse_rss_with_fallback',
                        lambda url, name, **kw: calls.append((url, name, kw)) or [])
    bot.get_otakuusa()
    bot.get_anime_limited()
    assert calls[0][0] == 'https://otakuusamagazine.com/feed/'
    assert calls[0][1] == 'Otaku USA'
    assert calls[1][0] == 'https://blog.alltheanime.com/feed/'
    assert calls[1][1] == 'Anime Limited'


def test_source_menu_is_paginated_and_callbacks_stay_short(monkeypatch):
    fake_sources = [(f'Source {i}', lambda: []) for i in range(23)]
    monkeypatch.setattr(bot, 'SOURCES', fake_sources)
    monkeypatch.setattr(bot, 'settings', _settings({'Source 0', 'Source 10'}))
    first = bot.build_sources_menu(0)
    second = bot.build_sources_menu(1)
    # 10 source rows + navigation + back
    assert len(first.inline_keyboard) == 12
    assert first.inline_keyboard[-2][0].text == '1/3'
    assert second.inline_keyboard[-2][1].text == '2/3'
    for markup in (first, second):
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    assert len(button.callback_data.encode('utf-8')) <= 64


def test_source_callback_resolver_accepts_paged_payload(monkeypatch):
    name = 'Very Long Source Name'
    monkeypatch.setattr(bot, 'SOURCES', [(name, lambda: [])])
    sid = bot._source_callback_id(name)
    assert bot._source_name_from_callback(f'{sid}:2') == name


def test_current_green_telegram_sources_are_built_in():
    names = {name for name, _ in bot.SOURCES}
    assert {
        'TG: Nexvlsz', 'TG: YtkaNews', 'TG: Advance', 'TG: QewbsNews',
        'TG: animetarakans', 'TG: anilibria', 'TG: VanitasNews',
    } <= names


def test_custom_source_does_not_duplicate_builtin_label_case_insensitive(monkeypatch):
    monkeypatch.setattr(bot, 'SOURCES', [('TG: QewbsNews', lambda: [])])
    bot._attach_custom_source({'type': 'tg', 'value': 'QewbsNews', 'label': 'tg: qewbsnews'})
    assert len(bot.SOURCES) == 1
