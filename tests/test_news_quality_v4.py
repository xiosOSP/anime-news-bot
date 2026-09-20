"""Regressions from September 20 screenshots: inline text, rumours and trailers."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import anime_news_bot as bot
from news_parser import clean_editorial_source, message_html_text


@pytest.mark.parametrize('markup,expected', [
    ('Анонсирован 2 сезон аниме по манге «<a href="https://example.test/title">'
     'Цугаи загробного мира</a>».<br>Дата выхода пока неизвестна.',
     'Анонсирован 2 сезон аниме по манге «Цугаи загробного мира».'),
    ('Зак Креггер рассказал, что когда-то действительно <b>предлагал сценарий '
     'для фильма DC</b>. Сюжет рассказывал о фармацевте.',
     'Зак Креггер рассказал, что когда-то действительно предлагал сценарий для фильма DC.'),
    ('Объявлены <b>14</b> новых актёров озвучки, в том числе '
     '<a href="https://example.test/cast">Дзюнко Такэути</a>, для нового аниме.',
     'Объявлены 14 новых актёров озвучки, в том числе Дзюнко Такэути, для нового аниме.'),
])
def test_real_telegram_parser_preserves_inline_sentences(markup, expected, monkeypatch, http_response):
    page = ('<div class="tgme_widget_message" data-post="news/1">'
            f'<div class="tgme_widget_message_text">{markup}</div>'
            '<time datetime="2026-09-20T10:00:00+00:00"></time></div>')
    monkeypatch.setattr(bot, '_is_too_old', lambda *_: False)
    monkeypatch.setattr(bot, 'http_get_public_with_retry',
                        lambda *a, **k: http_response(page.encode(), text=page))
    news = bot.get_telegram_channel('news', 'TG: News')[0]
    assert news['title'] == expected
    rendered = bot.format_news_short(news)
    assert rendered.startswith(expected)
    assert '«.' not in rendered
    assert 'действительно.' not in rendered


def test_inline_words_and_actual_paragraphs():
    assert message_html_text('Пре<b>мьера</b> <i>скоро</i>.<br><br>Студия Bones.') == \
        'Премьера скоро.\n\nСтудия Bones.'


def test_long_source_headline_is_not_cut_before_editor_sees_it():
    title = 'Анонсирован новый сезон «' + 'Очень длинное название ' * 12 + '»'
    head, body = bot._tg_title_and_summary(title, 'news', 'News')
    assert head == title and not body


def test_quoted_and_abbreviated_names_are_not_sentence_boundaries():
    head, tail = bot._tg_split_leading_sentence(
        'Новый сезон Dr. Stone получил трейлер. Премьера осенью.')
    assert head == 'Новый сезон Dr. Stone получил трейлер.'
    assert tail == 'Премьера осенью.'
    head, _ = bot._tg_split_leading_sentence('Вышел трейлер «Кто я? Новый мир». Премьера осенью.')
    assert head == 'Вышел трейлер «Кто я? Новый мир».'


def test_byline_does_not_become_release_date():
    source = 'Published on June 15, 2026 by Ingrid\nNew licenses arrive in December 2026.'
    assert bot.extract_release_date_from_text(source) == 'декабрь 2026'
    assert clean_editorial_source('Опубликовано 15 июня 2026 года Ингрид\nПремьера 4 октября.') == \
        'Премьера 4 октября.'
    assert clean_editorial_source('Студия опубликовала трейлер 15 июня.') == \
        'Студия опубликовала трейлер 15 июня.'
    assert '📅' not in bot._append_release_date('Релиз в декабре 2026 года.', 'декабрь 2026')
    assert '📅' in bot._append_release_date('Релиз в декабре 2027 года.', 'декабрь 2026')


@pytest.mark.parametrize('source,title,reason', [
    ('По словам инсайдеров, будет 3 сезон.', 'Анонсирован 3 сезон аниме', 'lost_uncertainty'),
    ('A sequel is reportedly in production.', 'Слух: аниме получит продолжение', ''),
    ('По словам инсайдеров, будет 3 сезон.', 'По данным инсайдеров, готовится 3 сезон', ''),
    ('Сезон официально анонсирован.', 'Официально анонсирован сезон', ''),
    ('Анонсирован сезон.', 'Анонсирован сезон «.', 'fragmented_headline'),
    ('Режиссёр предложил сценарий.', 'Режиссёр рассказал, что', 'fragmented_headline'),
])
def test_editorial_contract(source, title, reason):
    assert bot._editorial_rejection(source, title, '') == reason


@pytest.mark.asyncio
async def test_cached_batch_result_cannot_turn_rumour_into_announcement(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    bot.settings.llm_read_article = False
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, 'recent_subjects', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', False)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    news = {'title': 'По словам инсайдеров, аниме получит 3 сезон',
            'summary': '', 'lang': 'ru', '_llm_text': 'Старый анонс.'}
    bot._llm_editorial_remember(news, {'title': 'Аниме получит 3 сезон',
                                      'kind': 'анонс', 'topic': 'аниме'})
    request = AsyncMock()
    monkeypatch.setattr(bot, '_llm_call', request)
    assert await bot._llm_enrich(news, side_effects=False) == 'ok'
    assert '_llm_text' not in news
    assert news['_editorial_rejection'] == 'lost_uncertainty'
    assert 'инсайдеров' in bot.format_news_short(news)
    request.assert_not_awaited()


@pytest.mark.parametrize('url', [
    'https://twitter.com/yenpress', 'https://x.com/animatetimes',
    'https://youtube.com/@publisher', 'https://youtube.com/playlist?list=123',
    'https://vimeo.com/publisher', 'https://youtube.com.evil.test/watch?v=x',
])
def test_social_profiles_are_not_videos(url):
    assert not bot._is_playable_video_url(url)
    assert bot._find_video_in_html(f'<a href="{url}">Follow</a>') is None
    assert bot._add_video_link_to_text('Пост', url) == 'Пост'


@pytest.mark.parametrize('markup,url', [
    ('<iframe data-src="//www.youtube-nocookie.com/embed/roshidere2"></iframe>',
     'https://www.youtube-nocookie.com/embed/roshidere2'),
    ('<iframe data-lazy-src="https://youtu.be/roshidere2"></iframe>',
     'https://youtu.be/roshidere2'),
    ('<video><source src="../media/trailer.mp4"></video>',
     'https://news.example/media/trailer.mp4'),
    ('<lite-youtube videoid="roshidere2"></lite-youtube>',
     'https://www.youtube.com/watch?v=roshidere2'),
    ('<script type="application/ld+json">' + json.dumps({'@graph': [
        {'@type': ['Thing', 'VideoObject'], 'embedUrl': 'https://youtu.be/roshidere2'}]}) + '</script>',
     'https://youtu.be/roshidere2'),
    ('<a href="https://x.com/roshidere/status/123456/video/1">PV</a>',
     'https://x.com/roshidere/status/123456/video/1'),
])
def test_article_and_rss_share_video_discovery(markup, url):
    entry = SimpleNamespace(link='https://news.example/anime/story')
    assert bot._find_video_in_html(markup, entry.link) == url
    assert bot.extract_video_url(entry, markup) == url


def test_no_guessing_relative_video_without_base():
    assert bot._find_video_in_html('<video src="/clip.mp4"></video>') is None


def test_article_video_beats_sidebar_and_profile():
    markup = ('<aside><iframe src="https://youtu.be/other"></iframe></aside>'
              '<article><a href="https://twitter.com/yenpress">Follow</a>'
              '<iframe data-src="https://youtu.be/right"></iframe></article>')
    assert bot._find_video_in_html(markup) == 'https://youtu.be/right'


@pytest.mark.asyncio
async def test_japanese_trailer_replaces_false_profile_candidate(monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True))
    fetch = Mock(return_value={'video': 'https://youtu.be/roshidere2', 'text': ''})
    monkeypatch.setattr(bot, 'fetch_article', fetch)
    news = {'title': 'ロシデレ第2期ティザーPV公開', 'summary': '',
            'video': 'https://x.com/animatetimes', 'link': 'https://news.example/story'}
    await bot._discover_article_video(news)
    assert news['video'] == 'https://youtu.be/roshidere2'
    fetch.assert_called_once()


@pytest.mark.asyncio
async def test_discovered_trailer_is_sent_as_video_and_file_id_is_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True, require_image=False))
    monkeypatch.setattr(bot, 'pending_posts', None)
    monkeypatch.setattr(bot, 'format_news_text_long', lambda news: 'Вышел тизер «Рошидере».')
    monkeypatch.setattr(bot, 'format_news_post', lambda news: 'Вышел тизер «Рошидере».')
    monkeypatch.setattr(bot, 'fetch_article', Mock(return_value={
        'text': '', 'video': 'https://youtu.be/roshidere2'}))
    path = tmp_path / 'trailer.mp4'
    path.write_bytes(b'mocked video payload')
    download = Mock(return_value=path)
    monkeypatch.setattr(bot, 'download_video', download)
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)
    monkeypatch.setattr(bot, '_probe_video_file', lambda path: None)
    monkeypatch.setattr(bot, '_normalize_video_file', lambda path, info: path)
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs_async', AsyncMock(return_value={}))
    news = {'title': 'Рошидере: опубликован тизер', 'source': 'Example',
            'link': 'https://news.example/story', 'images': []}
    await bot._discover_article_video(news)
    video = await bot._prepare_video_file(news)
    tg = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(
        video=SimpleNamespace(file_id='telegram-trailer-id'), message_id=1, chat_id=-100)))
    assert await bot._send_post_thread_split(tg, news, video)
    download.assert_called_once()
    assert tg.send_video.call_args.kwargs['supports_streaming']
    assert news['_telegram_video_file_id'] == 'telegram-trailer-id'
    tg.send_video.reset_mock()
    assert await bot._send_post(tg, news, bot.CHANNEL_ID, None)
    assert tg.send_video.call_args.kwargs['video'] == 'telegram-trailer-id'


def test_missing_video_notice_only_for_moderators(monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True))
    monkeypatch.setattr(bot, 'CHANNEL_ID', -100)
    news = {'title': 'Опубликован трейлер', 'link': 'https://news.example/story'}
    assert 'Видео не прикреплено' in bot._video_moderation_notice('Пост', news, -200)
    assert bot._video_moderation_notice('Пост', news, -100) == 'Пост'


def test_malformed_video_candidate_does_not_hide_later_embed():
    markup = ('<iframe src="https://[broken"></iframe>'
              '<iframe src="https://youtu.be/valid"></iframe>')
    assert bot._find_video_in_html(markup) == 'https://youtu.be/valid'


def test_editorial_cache_includes_late_source_facts():
    news = {'title': 'New season', 'summary': 'word ' * 350 + 'Premiere in October.'}
    changed = dict(news, summary='word ' * 350 + 'Premiere in December.')
    assert bot._llm_content_key(news) != bot._llm_content_key(changed)


def test_telegram_external_trailer_link_is_extracted(monkeypatch, http_response):
    page = ('<div class="tgme_widget_message" data-post="news/2">'
            '<div class="tgme_widget_message_text">Вышел тизер Рошидере '
            '<a href="https://youtu.be/roshidere2">Смотреть</a></div>'
            '<time datetime="2026-09-20T10:00:00+00:00"></time></div>')
    monkeypatch.setattr(bot, '_is_too_old', lambda *_: False)
    monkeypatch.setattr(bot, 'http_get_public_with_retry',
                        lambda *a, **k: http_response(page.encode(), text=page))
    assert bot.get_telegram_channel('news', 'TG: News')[0]['video'] == \
        'https://youtu.be/roshidere2'


@pytest.mark.asyncio
async def test_telegram_rejecting_video_does_not_silently_publish_cover(monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True, require_image=False))
    monkeypatch.setattr(bot, 'CHANNEL_ID', -100)
    tg = SimpleNamespace(send_video=AsyncMock(side_effect=bot.BadRequest('invalid video')),
                         send_message=AsyncMock())
    news = {'title': 'Опубликован трейлер', 'source': 'Example',
            'link': 'https://news.example/story'}
    assert await bot._send_thread_media_then_text(
        tg, news, [], True, None, 'https://cdn.example/trailer.mp4',
        'Вышел трейлер.', None, {}, -200)
    assert 'Видео не прикреплено' in tg.send_message.call_args.kwargs['text']
