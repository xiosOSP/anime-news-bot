"""Аудит 10 ноября 2026: название тайтла уточняется перед публикацией в канал.

Пост попадает в ветку с тем названием, какое удалось узнать при сборе, и
лежит там часами. Наутро он вышел в канал с ромадзи, хотя Shikimori к тому
времени давно отвечал. Пример пересказан.
"""
import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


PEN = {'id': '63970', 'name': 'Pen to Wappa to Jijitsukon', 'russian': 'Перо, наручники и гражданский брак',
       'english': None, 'japanese': None, 'synonyms': []}
RUSSIAN = 'Перо, наручники и гражданский брак'


@pytest.fixture
def shikimori(monkeypatch, tmp_path):
    calls = []

    def post(url, json=None, **kwargs):
        calls.append(json['variables']['s'])
        return MagicMock(status_code=200, json=lambda: {'data': {'animes': [PEN]}})
    monkeypatch.setattr(bot.requests, 'post', post)
    resolver = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
    resolver.MIN_INTERVAL = 0
    monkeypatch.setattr(bot, 'work_titles', resolver)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
    monkeypatch.setattr(bot, 'settings', None)
    return calls


def _post():
    return {'title': 'Pen to Wappa anime announced', 'link': 'https://x/pen', 'source': 'TG: A',
            '_llm_text': 'В январе 2027 запустят аниме по манге «Pen to Wappa to Jijitsukon».',
            '_llm_subject': 'Pen to Wappa to Jijitsukon', '_llm_topic': 'аниме'}


def test_title_is_looked_up_again_right_before_publication(shikimori):
    news = _post()
    assert asyncio.run(bot._late_title_lookup(news)) is True
    assert news['_work_titles_ru'] == {'Pen to Wappa to Jijitsukon': RUSSIAN}
    assert f'«{RUSSIAN}»' in bot._russify_titles(news['_llm_text'], news)
    # Общий путь отправки зовёт проверку следом за автопостингом: второй раз
    # за минуту — ни сети, ни «нашлось».
    assert asyncio.run(bot._late_title_lookup(news)) is False
    assert len(shikimori) == 1


def test_silent_shikimori_is_not_asked_twice_in_a_row(shikimori, monkeypatch):
    """Автопостинг и общий путь отправки зовут проверку подряд: при молчащем
    Shikimori второй вызов — это ещё один таймаут перед самой публикацией."""
    import requests

    def down(url, json=None, **kwargs):
        shikimori.append(json['variables']['s'])
        raise requests.Timeout('slow')
    monkeypatch.setattr(bot.requests, 'post', down)
    news = _post()
    assert asyncio.run(bot._late_title_lookup(news)) is False
    assert asyncio.run(bot._late_title_lookup(news)) is False
    assert len(shikimori) == 1


def test_known_title_costs_no_request(shikimori):
    news = _post()
    asyncio.run(bot._late_title_lookup(news))
    again = dict(_post(), _work_titles_ru=dict(news['_work_titles_ru']))
    assert asyncio.run(bot._late_title_lookup(again)) is False
    assert len(shikimori) == 1


def test_post_left_in_the_thread_is_checked_again_later(shikimori):
    news = dict(_post(), _late_title_checked=time.time() - bot.LATE_TITLE_RECHECK_SEC - 5)
    assert asyncio.run(bot._late_title_lookup(news)) is True
    # Кривая отметка проверке не мешает; имя уже в кеше — второго запроса нет.
    assert asyncio.run(bot._late_title_lookup(dict(_post(), _late_title_checked='мусор'))) is True
    assert len(shikimori) == 1


@pytest.mark.parametrize('patch', [{'_edited_text': 'Текст админа'}, {'_llm_text': ''}])
def test_admin_text_and_posts_without_a_rewrite_are_left_alone(shikimori, patch):
    assert asyncio.run(bot._late_title_lookup(dict(_post(), **patch))) is False
    assert shikimori == []


def test_channel_send_looks_the_title_up_first(shikimori, monkeypatch):
    seen = {}

    async def send(_bot, news, video_file):
        seen['pairs'] = dict(news.get('_work_titles_ru') or {})
        return True
    monkeypatch.setattr(bot, '_send_channel_post', send)
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_mark_channel_post_now', lambda: None)
    assert asyncio.run(bot._prepare_and_send_channel_post(MagicMock(), _post())) is True
    assert seen['pairs'] == {'Pen to Wappa to Jijitsukon': RUSSIAN}


@pytest.fixture
def autopost(shikimori, tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'pending.json')
    key = store.add(_post())
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', NS(last_channel_post_at='', channel_interval_sec=3600,
                                            last_publish_at='', post_max_age_hours=72,
                                            russian_titles=True, save=lambda: None))
    for name in ('stats', 'source_yield', 'story_registry', 'story_history', 'work_aliases', 'anilist'):
        monkeypatch.setattr(bot, name, None)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    monkeypatch.setattr(bot, '_tg_account_media_waits', lambda news: False)
    monkeypatch.setattr(bot, '_update_moderation_done', AsyncMock())
    memory = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', memory)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_prepare_and_send_channel_post', sent)
    return NS(store=store, key=key, memory=memory, sent=sent)


def test_autopost_publishes_with_the_title_found_late(autopost):
    assert asyncio.run(bot._autopost_one_from_thread(NS())) == 'sent'
    assert autopost.sent.await_args.args[1]['_work_titles_ru'] == {'Pen to Wappa to Jijitsukon': RUSSIAN}


def test_post_that_turns_out_to_be_a_repeat_stays_in_the_thread(autopost):
    """Канал уже писал об этом тайтле под русским названием: с ромадзи пост
    повтором не считался, с найденным названием — считается."""
    autopost.memory.remember(['work:shiki:63970'], f'Анонсировано аниме «{RUSSIAN}»', 'bot',
                             events={'announce'}, numbers=())
    assert asyncio.run(bot._autopost_one_from_thread(NS())) is None
    autopost.sent.assert_not_awaited()
    stored = autopost.store.get(autopost.key)
    assert stored['_work_titles_ru'] == {'Pen to Wappa to Jijitsukon': RUSSIAN}   # название сохранено
    assert '_late_title_checked' not in stored
    # Следующий тик видит повтор сам, без Shikimori.
    assert autopost.store.next_for_autopost(skip=bot._autopost_skip_reason) is None


# ---------- строка «о чём» под текстом, который уже рассказал завязку ----------

def _about_news(about):
    return {'_about': about, '_llm_topic': 'аниме'}


@pytest.mark.parametrize('body', [
    'Аниме адаптирует мангу о старшекласснице, помогающей следователю в расследовании убийства.',
    'Сериал расскажет о суровом наёмнике, нанятом охотиться на ведьму.',
    'Студия EMT Squared снимет историю про школьницу и детектива.',
])
def test_about_line_is_not_added_when_the_body_already_tells_the_plot(monkeypatch, body):
    monkeypatch.setattr(bot, 'settings', None)
    text = f'В январе 2027 запустят аниме «Перо и наручники».\n\n{body}'
    about = 'Наблюдательная ученица помогает детективу раскрывать преступления и становится его напарницей.'
    assert bot._with_about(text, _about_news(about)) == text


@pytest.mark.parametrize('text', [
    # Завязки в тексте нет — строка «о чём» остаётся.
    'В январе 2027 запустят аниме «Перо и наручники».\n\nСериал снимает студия EMT Squared.',
    # «Аниме о …» в заголовке — тема, а не завязка.
    'Анонсировано аниме о Красной Шапочке.\n\nПремьера в январе 2027 года.',
])
def test_about_line_stays_when_the_body_has_no_plot(monkeypatch, text):
    monkeypatch.setattr(bot, 'settings', None)
    about = 'Наблюдательная ученица помогает детективу раскрывать преступления и становится его напарницей.'
    assert bot._with_about(text, _about_news(about)).endswith('\n\n' + about)


# ---------- чистка заголовка не виснет на длинной строке пробелов ----------

@pytest.mark.parametrize('text, expected', [
    ('[Интервью] Заголовок!', ' Заголовок.'),
    ('Заголовок [Новость]   !', 'Заголовок.'),
    ('Заголовок  【速報】 дальше', 'Заголовок дальше'),
    ('Заголовок   !!\nТекст!', 'Заголовок.\nТекст!'),
    ('[Тег]\nтекст', '[Тег]\nтекст'),
])
def test_title_tidy_keeps_its_behaviour(text, expected):
    assert bot._tidy_title_line(text) == expected


@pytest.mark.parametrize('line', ['\t \t' * 20000, ' ' * 60000 + 'x', '\t \t' * 20000 + '?'])
def test_title_tidy_is_fast_on_a_long_run_of_spaces(line):
    """«\\s*» с каждой позиции — квадратичный перебор: 60 тысяч пробелов и
    табуляций в заголовке источника останавливали бота на 20 секунд."""
    started = time.perf_counter()
    bot._tidy_title_line(line)
    assert time.perf_counter() - started < 1.0


# ---------- разбор чужого текста не виснет на длинных сериях знаков ----------

@pytest.mark.parametrize('line, expected', [
    ('Вышел трейлер. #аниме #трейлер', 'Вышел трейлер.'),
    ('«Ледяная стена» #аниме', '«Ледяная стена»'),
    ('Пост про #аниме и мангу', 'Пост про #аниме и мангу'),          # тег посреди фразы — часть текста
    ('#календарь', ''),
])
def test_trailing_hashtags_are_still_stripped(line, expected):
    import post_text
    assert post_text._tg_strip_trailing_tags(line) == expected


@pytest.mark.parametrize('text, expected', [
    ('Сериал выйдет в январе. Студия пока не названа и', 'Сериал выйдет в январе.'),
    ('Сериал выйдет...', ''),
    ('Премьера в январе.', 'Премьера в январе.'),
    ('Сериал снят человеком, чьё имя длинное и', ''),
])
def test_unfinished_tail_is_still_dropped(text, expected):
    import post_text
    assert post_text._drop_unfinished_tail(text) == expected


@pytest.mark.parametrize('name, text', [
    ('_tg_strip_trailing_tags', '"' * 60000),
    ('_tg_strip_trailing_tags', '. ' * 30000),
    ('_drop_unfinished_tail', '- ' * 30000 + ' z'),
    ('_drop_unfinished_tail', 'a' + ' ' * 60000 + 'z'),
    ('_drop_unfinished_tail', '..' * 30000 + ' z'),
])
def test_foreign_text_helpers_are_fast_on_long_runs(name, text):
    """Каждая из этих строк останавливала бота на десятки секунд: регулярка
    пробовала каждое место длинной серии знаков и каждый раз шла до конца."""
    import post_text
    started = time.perf_counter()
    getattr(post_text, name)(text)
    assert time.perf_counter() - started < 1.0


def test_source_hashtags_are_read_from_the_end_of_a_long_line():
    import post_text
    assert post_text.tg_source_hashtags('x' * 5000 + '. #арт #календарь') == ['арт', 'календарь']
