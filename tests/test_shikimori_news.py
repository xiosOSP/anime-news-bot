"""Раздел «Новости» Shikimori как источник.

Русские новости только про аниме и мангу, бесплатный API без ключа:
https://shikimori.io/api/topics?forum=news. Разметка ниже повторяет
настоящий ответ API от 23 сентября 2026 года, тексты сокращены.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot

SITE = 'https://shikimori.io'
POSTER = f'{SITE}/system/user_images_h/original/6dd7/483a.jpg'


def _anime_link(name: str, russian: str, text: str) -> str:
    return (f'<a href="{SITE}/animes/62542-grand-blue-season-3" title="{name}" class="bubbled b-link" '
            f'data-attrs="{{&quot;id&quot;:62542,&quot;russian&quot;:&quot;{russian}&quot;}}">{text}</a>')


def _topic(**overrides):
    topic = {
        'id': 645257,
        'topic_title': 'Анонс 4-го сезона «Grand Blue»',
        'html_body': ('Анонсировали 4-й сезон '
                      + _anime_link('Grand Blue Season 3', 'Необъятный океан 3',
                                    '«Grand Blue» (Необъятный океан)')
                      + '<br class="br"><br class="br">Дату выхода сообщат позже.'),
        'html_footer': (f'<div class="b-shiki_wall to-process"><a href="{POSTER}" class="b-image unprocessed">'
                        f'<img src="{SITE}/system/user_images_h/preview/6dd7/483a.jpg"></a></div>'),
        'created_at': '2026-09-23T18:27:29.185+03:00',
        'type': 'Topics::NewsTopic',
        'linked_id': 62542, 'linked_type': 'Anime',
    }
    topic.update(overrides)
    return topic


def test_topic_becomes_a_russian_news_item():
    news = bot._shikimori_topic(_topic(), SITE)
    published = news.pop('published_parsed')
    assert news == {
        'title': 'Анонс 4-го сезона «Grand Blue»',
        'link': f'{SITE}/forum/news/645257',
        'summary': 'Анонсировали 4-й сезон «Grand Blue» (Необъятный океан). Дату выхода сообщат позже.',
        'image': POSTER, 'images': [POSTER], 'video': None, '_video_thumb': None,
        'source': 'Shikimori', 'lang': 'ru',
        '_work_name': 'Grand Blue', '_work_russian': 'Необъятный океан',
    }
    assert tuple(published[:5]) == (2026, 9, 23, 15, 27)          # время в UTC


def test_post_names_the_work_in_russian(monkeypatch):
    """Пара автора новости — «Grand Blue» (Необъятный океан) — точнее поиска по
    базе: там у тайтла третьего сезона русское имя с номером."""
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=True, llm_tags=True))
    post = bot.format_news_short(bot._shikimori_topic(_topic(), SITE))
    assert post.startswith('Анонс 4-го сезона «Необъятный океан» (Grand Blue).')
    assert post.endswith('#НеобъятныйОкеан')


def test_spoilers_never_reach_the_post():
    body = ('Вышла 12-я серия.<div class="b-spoiler_block to-process"><span>спойлер</span>'
            '<div>Главный герой погибает.</div></div> Финал через неделю. '
            '<span class="b-spoiler_inline to-process"><span>злодей — брат героя</span></span>')
    summary = bot._shikimori_topic(_topic(html_body=body), SITE)['summary']
    assert 'погибает' not in summary and 'брат' not in summary
    assert summary.startswith('Вышла 12-я серия.') and 'Финал через неделю.' in summary


def test_linked_name_keeps_only_the_russian_one():
    """Ссылку без своего текста Shikimori рисует двумя именами подряд."""
    body = ('Перед премьерой «<a class="bubbled b-link" href="/animes/62856">'
            '<span class="name-en">Nijusseiki Denki Mokuroku</span>'
            '<span class="name-ru">История электричества в двадцатом веке</span></a>» '
            'издание поговорило с режиссёром.')
    summary = bot._shikimori_topic(_topic(html_body=body), SITE)['summary']
    assert summary == ('Перед премьерой «История электричества в двадцатом веке» '
                       'издание поговорило с режиссёром.')


def test_line_break_ends_a_sentence_without_doubling_punctuation():
    text = 'Строка без точки\nВопрос?\n«Цитата!»\nСписок:\nКонец.'
    assert bot._shikimori_sentences(text) == 'Строка без точки. Вопрос? «Цитата!» Список: Конец.'


def test_trailer_without_pictures_keeps_its_frame():
    footer = ('<div class="b-shiki_wall to-process"><div class="b-video unprocessed youtube">'
              '<a class="video-link" data-href="https://youtube.com/embed/rYpXoCdTNQI" '
              'href="https://youtu.be/rYpXoCdTNQI"><img src="//img.youtube.com/vi/rYpXoCdTNQI/hqdefault.jpg">'
              '</a><span class="marker">youtube</span></div></div>')
    news = bot._shikimori_topic(_topic(html_footer=footer), SITE)
    assert news['video'] == 'https://youtu.be/rYpXoCdTNQI'
    assert news['images'] == [] and news['_video_thumb'] == 'https://img.youtube.com/vi/rYpXoCdTNQI/hqdefault.jpg'
    with_poster = bot._shikimori_topic(_topic(html_footer=footer + _topic()['html_footer']), SITE)
    assert with_poster['images'] == [POSTER] and with_poster['_video_thumb'] is None


def test_pictures_are_originals_and_at_most_four():
    links = ''.join(f'<a href="{SITE}/system/user_images_h/original/a/{i}.jpg" class="b-image unprocessed">'
                    f'<img src="{SITE}/system/user_images_h/thumbnail/a/{i}.jpg"></a>' for i in range(6))
    news = bot._shikimori_topic(_topic(html_footer='', html_body='Текст.' + links), SITE)
    assert news['images'] == [f'{SITE}/system/user_images_h/original/a/{i}.jpg' for i in range(4)]
    assert news['summary'] == 'Текст.'


@pytest.mark.parametrize(('title', 'expected'), [
    # Рубрика без названия ничего не сообщает — заголовком становится первая фраза.
    ('Hinode Japan 2026', 'С 4 по 6 октября в Москве пройдёт выставка японской культуры.'),
    ('Постер, трейлер и дата премьеры', 'С 4 по 6 октября в Москве пройдёт выставка японской культуры.'),
    ('', 'С 4 по 6 октября в Москве пройдёт выставка японской культуры.'),
    # Короткий заголовок с названием — уже заголовок.
    ('Трейлер «Dark Machine»', 'Трейлер «Dark Machine»'),
    ('Crunchyroll ищет руководителя по борьбе с пиратством',
     'Crunchyroll ищет руководителя по борьбе с пиратством'),
])
def test_rubric_title_gives_way_to_the_first_sentence(title, expected):
    body = 'С 4 по 6 октября в Москве пройдёт выставка японской культуры. Билеты уже в продаже.'
    assert bot._shikimori_topic(_topic(topic_title=title, html_body=body), SITE)['title'] == expected


def test_long_first_sentence_does_not_become_a_title():
    body = 'Очень ' + 'длинное ' * 40 + 'предложение. Второе.'
    assert bot._shikimori_topic(_topic(topic_title='Hinode Japan 2026', html_body=body), SITE)['title'] \
        == 'Hinode Japan 2026'


@pytest.mark.parametrize('body', [
    'Похожий тайтл: «Grand Blue Dreaming» (Необъятный океан).',   # в заголовке не он
    'Трейлер «Пираты» (Чёрная лагуна).',                         # обе части русские
    'Анонс «Grand Blue» (Grand Blue Dreaming).',                   # русского имени нет
])
def test_work_pair_needs_the_titles_name_and_a_russian_translation(body):
    news = bot._shikimori_topic(_topic(topic_title='Анонс «Grand Blue» и «Пираты»', html_body=body), SITE)
    assert '_work_russian' not in news


def test_topic_without_id_or_text_is_skipped():
    assert bot._shikimori_topic(_topic(id=None), SITE) is None
    assert bot._shikimori_topic(_topic(topic_title='', html_body=''), SITE) is None
    assert bot._shikimori_topic('не словарь', SITE) is None


# ---------- запрос к API ----------

def _stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def api(monkeypatch):
    state = NS(status=200, payload=[], urls=[])

    def get(url, **kwargs):
        state.urls.append(url)
        return NS(status_code=state.status, text=json.dumps(state.payload), close=lambda: None)
    monkeypatch.setattr(bot, 'http_get_with_retry', get)
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    return state


def test_fresh_news_up_to_the_per_source_limit(api, monkeypatch):
    monkeypatch.setattr(bot, 'NEWS_PER_SOURCE', 2)
    api.payload = [_topic(id=i, created_at=_stamp(i)) for i in (1, 2, 3)]
    assert [n['link'] for n in bot.get_shikimori_news()] == [f'{SITE}/forum/news/1', f'{SITE}/forum/news/2']
    assert api.urls == [bot.SHIKIMORI_NEWS_URL] and 'forum=news' in bot.SHIKIMORI_NEWS_URL


def test_old_news_are_skipped(api):
    api.payload = [_topic(id=1, created_at=_stamp(100)), _topic(id=2, created_at=_stamp(1)),
                   'мусор', {'id': 'x'}]
    assert [n['link'] for n in bot.get_shikimori_news()] == [f'{SITE}/forum/news/2']


def test_link_follows_the_configured_site(api, monkeypatch):
    """Shikimori уже переезжал с домена на домен: адрес берём из настройки."""
    monkeypatch.setattr(bot, 'SHIKIMORI_NEWS_URL', 'https://shikimori.one/api/topics?forum=news')
    api.payload = [_topic(created_at=_stamp(1))]
    assert bot.get_shikimori_news()[0]['link'] == 'https://shikimori.one/forum/news/645257'


@pytest.mark.parametrize(('status', 'payload', 'reason'), [
    (503, [], 'API HTTP 503'),
    (200, {'error': 'rate limit'}, 'API: в ответе нет списка новостей'),
])
def test_api_failure_is_reported_not_silent(api, status, payload, reason):
    api.status, api.payload = status, payload
    result = bot.get_shikimori_news()
    assert isinstance(result, bot.SourceFetchFailure) and result.reason == reason


def test_broken_json_is_reported(monkeypatch):
    monkeypatch.setattr(bot, 'http_get_with_retry',
                        lambda url, **kwargs: NS(status_code=200, text='<html>', close=lambda: None))
    result = bot.get_shikimori_news()
    assert isinstance(result, bot.SourceFetchFailure) and result.reason.startswith('API: ответ не разобрался')


def test_source_is_registered_and_trusted_as_anime_only():
    assert ('Shikimori', bot.get_shikimori_news) in bot.SOURCES
    assert 'Shikimori' not in bot.GENERAL_TOPIC_SOURCES


def test_published_time_is_utc_struct():
    news = bot._shikimori_topic(_topic(created_at='2026-01-02T01:30:00+03:00'), SITE)
    assert time.strftime('%Y-%m-%d %H:%M', news['published_parsed']) == '2026-01-01 22:30'


def test_lookup_keeps_the_sources_own_russian_name(monkeypatch, tmp_path):
    """База отдаёт тайтл третьего сезона с номером в имени, автор новости —
    название франшизы. Ключ тайтла берём из базы, название — у автора."""
    answer = [{'id': '62542', 'name': 'Grand Blue', 'russian': 'Необъятный океан 3',
               'english': '', 'japanese': '', 'synonyms': []}]
    monkeypatch.setattr(bot.requests, 'post', lambda url, json=None, **kwargs: MagicMock(
        status_code=200, json=lambda: {'data': {'animes': answer}}))
    store = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
    store.MIN_INTERVAL = 0
    monkeypatch.setattr(bot, 'work_titles', store)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
    monkeypatch.setattr(bot, 'sent_links', None)
    shikimori = bot._shikimori_topic(_topic(), SITE)
    english = {'title': 'Grand Blue Season 4 Announced', 'link': 'https://ac/1'}
    bot._annotate_work_keys([shikimori, english])
    assert shikimori['_work_key'] == english['_work_key'] == 'shiki:62542'
    assert shikimori['_work_russian'] == 'Необъятный океан'
    assert english['_work_russian'] == 'Необъятный океан 3'


# ---------- где смотреть, какие источники мертвы ----------

@pytest.mark.asyncio
@pytest.mark.parametrize('custom', [[], [{'type': 'tg', 'label': 'TG: marvel_dc_f', 'value': 'marvel_dc_f'}]])
async def test_sources_command_points_to_health(monkeypatch, custom):
    """/sources показывает только добавленные вручную источники; админ прислал
    его вывод, чтобы показать мёртвые источники, — а они видны в /health."""
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, 'custom_sources', NS(all=lambda: custom))
    reply = AsyncMock()
    update = NS(message=NS(reply_text=reply), effective_chat=NS(id=42), effective_user=NS(id=1))
    await bot.sources_command(update, NS(args=[]))
    assert '/health' in reply.await_args.args[0]
