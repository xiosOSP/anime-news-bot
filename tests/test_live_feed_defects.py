"""Дефекты, найденные прогоном живых лент через бота без модели (22.09.2026).

Разметка здесь — настоящая, урезанная до нужного: t.me/s/, описание RSS
ComicBook, карточки Yen Press и главная AnimateTimes. Каждый тест назван по
тому, что уходило в канал до исправления.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot
from news_parser import clean_editorial_source, clean_html_fragment, parse_listing_html
from post_text import _tg_title_and_summary, tg_source_hashtags


def _tg_page(*messages: str, title: str = 'Гиковский Вестник') -> str:
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    body = ''.join(m.replace('{stamp}', stamp) for m in messages)
    return (f'<html><body><div class="tgme_channel_info_header_title"><span dir="auto">'
            f'{title}</span></div>{body}</body></html>')


def _tg_message(post: str, text_html: str, extra_class: str = '') -> str:
    return (f'<div class="tgme_widget_message {extra_class} js-widget_message" data-post="{post}">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>'
            f'<time datetime="{{stamp}}"></time></div>')


@pytest.fixture
def tg_page(monkeypatch):
    def serve(html_text: str):
        fake = MagicMock(status_code=200, text=html_text)
        monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: fake)
        monkeypatch.setattr(bot, '_read_limited_text', lambda response: response.text)
    return serve


class TestTelegramChannel:
    def test_pinned_photo_service_message_is_not_a_post(self, tg_page):
        """В канал уходил пост «Vanitas: News 🌀 pinned a photo.»."""
        tg_page(_tg_page(
            _tg_message('VanitasNews/22928',
                        '<a class="tgme_widget_message_author_name" href="https://t.me/VanitasNews">'
                        '<span dir="auto">Vanitas: News 🌀</span></a> pinned a photo',
                        extra_class='text_not_supported_wrap service_message'),
            # Служебное событие, в котором нет имени канала: его не отсеет и
            # сверка с подписью.
            _tg_message('VanitasNews/22930', 'Channel photo updated by the admins',
                        extra_class='service_message'),
            _tg_message('VanitasNews/22929',
                        'Nuovo PV dell\'adattamento anime del manga Super Psychic Policeman Chojo.'),
            title='Vanitas: News 🌀'))
        posts = bot.get_telegram_channel('VanitasNews', 'TG: VanitasNews')
        assert [p['link'] for p in posts] == ['https://t.me/VanitasNews/22929']

    def test_signature_with_display_name_is_cut(self, tg_page):
        """@QewbsNews подписывает посты «📰 Гиковский Вестник» — это не текст новости."""
        tg_page(_tg_page(_tg_message(
            'QewbsNews/1',
            'Райан Гослинг сыграет главную роль в новом фильме Рефна.<br/><br/>'
            'Таким образом, это будет их третья совместная работа.<br/><br/><br/><br/>'
            '<i class="emoji"><b>📰</b></i> Гиковский Вестник')))
        post = bot.get_telegram_channel('QewbsNews', 'TG: QewbsNews')[0]
        assert post['summary'] == 'Таким образом, это будет их третья совместная работа.'

    def test_rubric_hashtags_leave_the_text_and_mark_the_genre(self, tg_page):
        """«#арт | #kusuriya» уезжал в пост, а фан-арт проходил как новость."""
        tg_page(_tg_page(_tg_message(
            'Advance/9', '🐸 В ночь с Женькой и Маомао от CL_opium.<br/>#арт | #kusuriya')))
        post = bot.get_telegram_channel('advance_emp', 'TG: Advance')[0]
        assert '#' not in post['title'] + post['summary']
        assert post['_source_tags'] == ['арт', 'kusuriya']
        assert bot.noise_reason(post) == 'фан-контент'

    def test_news_tag_after_the_sentence_is_dropped(self):
        title, summary = _tg_title_and_summary(
            'Трейлер к аниме «Тёдзо!».\nСнимает студия Arvo animation. Премьера 6-го октября. #новость',
            'nexvlsz', 'TG: Nexvlsz')
        assert summary == 'Снимает студия Arvo animation. Премьера 6-го октября.'

    @pytest.mark.parametrize('line', [
        'Трейлер нового сезона #OnePiece',        # тег служит названием
        "L'episodio #12 (finale) uscirà il 25 settembre",   # номер, а не тег
    ])
    def test_hashtag_inside_the_phrase_stays(self, line):
        assert _tg_title_and_summary(line, 'ch', 'Канал')[0] == line
        assert tg_source_hashtags(line) == []


class TestSourceTextJunk:
    def test_photo_credit_does_not_glue_to_the_first_sentence(self):
        """ComicBook: «Courtesy of Netflix Cyberpunk: Edgerunners is coming back…»."""
        description = ('<figcaption class="wp-caption-text">Courtesy of Netflix</figcaption>'
                       '<figure class="post-thumbnail"><img src="https://comicbook.com/a.jpg"/>'
                       '</figure><p>Cyberpunk: Edgerunners is coming back this Fall.</p>')
        assert clean_html_fragment(description) == 'Cyberpunk: Edgerunners is coming back this Fall.'

    def test_press_release_buttons_are_removed(self):
        """GKIDS: описание начиналось с «VIEW TRAILER HERETICKETS ON SALE NOW»."""
        text = ('VIEW TRAILER HERETICKETS ON SALE NOW Tickets are now on sale for '
                'SHAUN THE SHEEP: THE BEAST OF MOSSY BOTTOM.')
        assert clean_editorial_source(text) == (
            'Tickets are now on sale for SHAUN THE SHEEP: THE BEAST OF MOSSY BOTTOM.')

    def test_ticket_fact_in_plain_case_is_kept(self):
        text = 'Билеты уже в продаже. Tickets are on sale now at participating theaters.'
        assert clean_editorial_source(text) == text


YEN_CARD = '''<div class="news-list-grid"><div class="inline_block col-d-33">
<div class="news-list-img prel"><img alt="{title}" src="https://yenpress.com/uploads/a.jpg"/></div>
<div class="news-list-txt">
<p class="paragraph fs-12 grey upper">Posted {posted} by Ingrid  Lorenzi</p>
<h3 class="heading subtitle-25">{title}</h3>
<p class="paragraph fs-16">Here's a quick recap of the titles joining our lineup!</p>
<a class="view-link" href="/news/{slug}"><span>Read more</span></a>
</div></div></div>'''


def _listing(markup, pattern=r'yenpress\.com/news/[^/?#]+/?$', base='https://yenpress.com/'):
    return parse_listing_html(markup, source_name='Yen Press', base_url=base,
                              href_pattern=pattern, limit=5,
                              normalize_image=lambda value, link: value,
                              normalize_url=lambda link: link)


class TestListingCards:
    def test_byline_is_not_the_summary(self):
        """Описанием Yen Press была подпись «Posted Aug 24, 2026 by Ingrid Lorenzi»."""
        row = _listing(YEN_CARD.format(title='New Licenses Coming in December 2026',
                                       posted='Aug 24, 2026', slug='new-licenses'))[0]
        assert row['summary'] == "Here's a quick recap of the titles joining our lineup!"

    def test_date_is_read_from_the_byline(self):
        row = _listing(YEN_CARD.format(title='New Licenses Coming in December 2026',
                                       posted='Apr 06, 2026', slug='new-licenses'))[0]
        assert tuple(row['published_parsed'][:3]) == (2026, 4, 6)

    def test_date_is_read_from_the_address(self):
        markup = ('<article><h3>TOHO and GKIDS Reveal New Trailer</h3>'
                  '<a href="https://gkids.com/2026/09/19/toho-trailer/">Read more</a></article>')
        row = _listing(markup, pattern=r'gkids\.com/20\d\d/\d\d/\d\d/[^/?#]+/?$',
                       base='https://gkids.com/')[0]
        assert tuple(row['published_parsed'][:3]) == (2026, 9, 19)

    def test_time_tag_wins_and_is_converted_to_utc(self):
        markup = ('<article><h3>Seven Seas licenses a new manga</h3>'
                  '<time datetime="2026-09-20T01:30:00+03:00"></time>'
                  '<a href="https://sevenseas.example/2026/09/19/manga/">Read more</a></article>')
        row = _listing(markup, pattern=r'/20\d\d/\d\d/\d\d/[^/?#]+/?$',
                       base='https://sevenseas.example/')[0]
        assert tuple(row['published_parsed'][:4]) == (2026, 9, 19, 22)

    def test_undated_card_stays_undated(self):
        markup = '<article><h3>Official announcement title</h3><a href="/news/x">Read more</a></article>'
        row = _listing(markup, pattern=r'/news/\w+$', base='https://official.example/')[0]
        assert row['published_parsed'] is None

    def test_old_cards_do_not_leave_the_source(self, monkeypatch):
        """Апрельский «Sakura Con 2026 Wrap Up!» собирался как свежая новость."""
        fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%b %d, %Y')
        page = (YEN_CARD.format(title='Exciting New Licenses Coming Soon', posted=fresh,
                                slug='fresh-news')
                + YEN_CARD.format(title='Sakura Con 2026 Wrap Up!', posted='Apr 06, 2026',
                                  slug='sakura-con'))
        response = MagicMock(status_code=200, text=page)
        monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)
        monkeypatch.setattr(bot, '_read_limited_text', lambda r: r.text)
        monkeypatch.setattr(bot, 'settings', MagicMock(post_max_age_hours=48))
        rows = bot.get_yenpress_news()
        assert [row['title'] for row in rows] == ['Exciting New Licenses Coming Soon']

    def test_convention_wrap_up_is_a_digest(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings', MagicMock(local_noise_filter=True))
        monkeypatch.setattr(bot, 'KEYWORDS', [])
        assert bot.matches_keywords({'title': 'Anime NYC 2026 Wrap Up!', 'summary': ''}) is False
        assert bot.matches_keywords({'title': 'Yen Press wraps up its manga license for 2026',
                                     'summary': ''}) is True


ANIMATETIMES = '''<html><body>
<a class="c-item-link" href="https://www.animatetimes.com/news/details.php?id=1789528784" id="top_pickup1">
<div class="c-favtag c-favtag-pickUp">PICK UP</div>
<div class="c-item-img"><img alt="夏アニメOP／ED主題歌・アニソン特集！" src="https://img2.animatetimes.com/a.jpg"/></div>
<div class="c-item-ttl"><div class="c-item-ttl__heading">夏アニメOP／ED主題歌・アニソン特集！</div></div></a>
<a class="c-item-link" href="https://www.animatetimes.com/news/details.php?id=1790000001" id="top_news">
<div class="c-favtag">NEWS</div>
<div class="c-item-ttl"><div class="c-item-ttl__heading">『ぐらんぶる』Season 4制作決定＆ビジュアル解禁！</div></div></a>
<a href="https://www.animatetimes.com/news/details.php?id=1400000000">【2024年】おすすめアニメまとめ特集ページはこちら</a>
<a class="c-headline-link" href="https://www.animatetimes.com/news/details.php?id=1500000000" id="times_ranking">
第 5 位 『ハイキュー!!』キャラクター一覧 2024-03-11 16:00</a>
</body></html>'''


class TestAnimateTimes:
    @pytest.fixture
    def page(self, monkeypatch):
        def serve(markup):
            response = MagicMock(status_code=200, text=markup)
            monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)
            monkeypatch.setattr(bot, '_read_limited_text', lambda r: r.text)
        return serve

    def test_news_feed_not_the_showcase(self, page):
        """Первые пять ссылок главной были витриной «PICK UP», а не новостями."""
        page(ANIMATETIMES)
        titles = [row['title'] for row in bot.get_animatetimes()]
        assert titles == ['『ぐらんぶる』Season 4制作決定＆ビジュアル解禁！']

    def test_without_news_feed_showcase_and_ranking_are_still_skipped(self, page):
        page(ANIMATETIMES.replace('id="top_news"', 'id="other"'))
        titles = [row['title'] for row in bot.get_animatetimes()]
        assert titles == ['『ぐらんぶる』Season 4制作決定＆ビジュアル解禁！',
                          '【2024年】おすすめアニメまとめ特集ページはこちら']

    def test_label_does_not_stick_to_the_title(self, page):
        page(ANIMATETIMES.replace('id="top_pickup1"', 'id="top_news"'))
        assert not any(row['title'].startswith('PICK UP') for row in bot.get_animatetimes())


class TestUntranslatedPost:
    @pytest.fixture
    def broken_translator(self, monkeypatch):
        class TooManyRequests:
            def translate(self, text):
                raise RuntimeError('429 Client Error')
        monkeypatch.setattr(bot, 'translator', TooManyRequests())
        monkeypatch.setattr(bot, 'anilist', MagicMock(lookup=lambda q: None))
        monkeypatch.setattr(bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(bot, '_translation_cache', {})

    def _english(self):
        return {'title': 'Grand Blue Season 4 Announced With Teaser Visual',
                'summary': 'Grand Blue Season 4 anime was officially announced after the '
                           'third season finale on October 22.',
                'link': 'https://animecorner.me/grand-blue-season-4/', 'source': 'Anime Corner'}

    def test_failed_translation_is_detected(self, broken_translator):
        assert bot._left_untranslated(self._english()) is True

    def test_translated_post_passes(self, monkeypatch):
        class Works:
            def translate(self, text):
                return 'Анонсирован четвёртый сезон аниме Grand Blue с тизер-постером'
        monkeypatch.setattr(bot, 'translator', Works())
        monkeypatch.setattr(bot, 'anilist', MagicMock(lookup=lambda q: None))
        monkeypatch.setattr(bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(bot, '_translation_cache', {})
        assert bot._left_untranslated(self._english()) is False

    @pytest.mark.parametrize('extra', [
        {'lang': 'ru'},
        # Пересказ модели и правка админа могут быть почти целиком латиницей —
        # трогать их всё равно нельзя.
        {'_llm_text': 'Grand Blue Season 4: teaser visual и дата'},
        {'_edited_text': 'Grand Blue Season 4 announced!'},
    ])
    def test_russian_model_and_admin_texts_are_not_checked(self, broken_translator, extra):
        assert bot._left_untranslated({**self._english(), **extra}) is False

    def test_russian_post_full_of_latin_names_is_russian(self):
        """Самый «латинский» русский пост живых лент — эпизод с английским названием."""
        news = {'title': 'Kakao Entertainment закроет Tapas и Kakao Webtoon',
                'summary': 'Контент переедет на KakaoPage до конца года.', 'lang': 'ru'}
        text = bot.format_news_short(news)
        letters = [c for c in text if c.isalpha()]
        cyrillic = sum(1 for c in letters if 'а' <= c.lower() <= 'я' or c in 'ёЁ')
        assert cyrillic / len(letters) > bot.UNTRANSLATED_CYRILLIC_SHARE * 1.5
        assert bot._left_untranslated({**news, 'lang': None}) is False

    def test_pipeline_defers_instead_of_publishing(self, broken_translator, monkeypatch):
        monkeypatch.setattr(bot, 'settings', MagicMock(local_topic_filter=True))
        for name in ('_improve_thumb', '_discover_article_video', '_optimize_news_media'):
            monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
        monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
        monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='off'))
        monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
        result = asyncio.run(bot._prepare_news_for_send(self._english(), 'Anime Corner',
                                                        apply_dedup=False))
        assert result == 'deferred'


class TestGeneralTopicTelegram:
    def test_geek_channel_without_model_keeps_only_anime(self):
        """«Гиковский Вестник» без модели шёл в канал целиком: Sony, Гослинг, Торонто."""
        gosling = {'source': 'TG: QewbsNews', 'lang': 'ru',
                   'title': 'Райан Гослинг сыграет главную роль в новом фильме Рефна.',
                   'summary': 'Таким образом, это будет их третья совместная работа.'}
        anime = {**gosling, 'title': 'Аниме «Магическая битва» получит третий сезон.'}
        assert bot.off_topic_without_llm(gosling) is True
        assert bot.off_topic_without_llm(anime) is False


def test_age_filter_really_uses_the_card_date():
    """Страховка от вакуума: дата карточки должна быть той, что понимает фильтр."""
    old = _listing(YEN_CARD.format(title='Old announcement from April', posted='Apr 06, 2026',
                                   slug='old'))[0]['published_parsed']
    assert isinstance(old, time.struct_time)
    assert bot._is_too_old(old, max_age_hours=48) is True
