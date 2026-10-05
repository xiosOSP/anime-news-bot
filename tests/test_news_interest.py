"""Интерес новостей: чужие названия в постах и вес тем при выборе.

На канале у бота 0.6 реакции на пост против 2.8 у админов. Две причины видны
по самим постам (примеры ниже — пересказ, не цитаты):

- в посты попадали чужие названия: «Game Tengoku: THE GAME PARADISE!» в
  новости о Professor Layton, «Major: Message» перед «Shonen Jump», «B Gata
  H Kei» в трейлере сериала — модель переносила их из соседней новости
  пачки или из памяти;
- 57% постов бота были про западное кино и сериалы (у админов — 16%), а
  кадры серий, арты и манга, которые у админов набирают больше всего, у
  бота весили наравне с кассовыми прогнозами.
"""
import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot as bot
import llm_protocol as llm

# ------------------------------------------------ чужие названия в ответе

LAYTON = 'Professor Layton and the New World of Steam gets December 10 release date in new trailer'


def test_title_from_another_story_is_rejected():
    assert llm._editorial_rejection(
        LAYTON, 'Профессор Лейтон выйдет 10 декабря RELEASE THE SPYCE',
        'Game Tengoku: THE GAME PARADISE!') == 'unsupported_names'


def test_single_foreign_name_in_english_story_is_enough():
    assert llm._editorial_rejection(
        'Popular Shonen Jump anime confirms second season with trailer',
        'Major: Message и Shonen Jump: второй сезон подтверждён', '') == 'unsupported_names'


def test_names_from_the_source_and_hint_pass():
    hint = 'Русское название тайтла (Shikimori): «Необъятный океан»; в источнике — Grand Blue'
    source = 'Grand Blue season 4 announced, Zero-G animates\n' + hint
    assert llm._editorial_rejection(
        source, 'Анонсирован 4 сезон «Необъятный океан» (Grand Blue)',
        'Анимацией займётся Zero-G, премьера на Netflix.') == ''


def test_abbreviation_possessive_and_accents_are_the_same_names():
    assert llm._editorial_rejection(
        "Sword Art Online's new film and Pokémon Legends dated", 'Новый фильм SAO и Pokemon Legends',
        '') == ''
    # «×» в источнике — не буква: «x» в ответе не новое имя, как и инициалы.
    assert llm._editorial_rejection(
        'SPY×FAMILY Code: White sequel', 'Продолжение Spy x Family: Code White', '') == ''


def test_japanese_source_may_get_official_latin_names():
    # Японское название латиницей можно написать только по памяти: ромадзи
    # или официальное английское. Отказ отправлял бы пост в машинный перевод.
    source = '「コードギアス」新作、東映アニメーション制作。TOHOシネマズで先行上映'
    assert llm._editorial_rejection(
        source, 'Анонсирован новый проект Code Geass',
        'Анимацией займётся Toei Animation, предпоказ в TOHO Cinemas.') == ''


def test_name_from_a_neighbour_in_the_batch_is_borrowed():
    own = '「コードギアス」新作が2027年に放送'
    neighbours = 'Witch Watch season 2 announced'
    assert llm._borrowed_names(own, neighbours, 'Новый Code Geass и Witch Watch') == ['Witch', 'Watch']
    # Имя из памяти, которого нет у соседей, — не перенос.
    assert llm._borrowed_names(own, neighbours, 'Новый проект Code Geass') == []


def _batch_news(title, summary, source):
    return {'title': title, 'summary': summary, 'source': source}


def test_batch_answer_with_a_neighbours_name_goes_to_a_single_call(enrich_env, monkeypatch, caplog):
    monkeypatch.setattr(bot, 'LLM_BATCH_SIZE', 4)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    japanese = _batch_news('「コードギアス」新作が2027年に放送', '詳細は後日発表。', 'Comic Natalie(JP)')
    english = _batch_news('Witch Watch season 2 announced', 'The sequel premieres in 2027.', 'Crunchyroll')
    items = [
        {'id': 1, 'topic': 'аниме', 'kind': 'анонс', 'subject': 'Code Geass',
         'title': 'Новый Code Geass и Witch Watch выйдут в 2027 году', 'summary': '', 'tags': []},
        {'id': 2, 'topic': 'аниме', 'kind': 'анонс', 'subject': 'Witch Watch',
         'title': 'Анонсирован 2 сезон Witch Watch', 'summary': 'Премьера в 2027 году.', 'tags': []},
    ]
    with caplog.at_level(logging.WARNING), \
            patch.object(bot.requests, 'post', return_value=_answer({'items': items})):
        assert asyncio.run(bot._llm_enrich_batch([japanese, english])) == 1
    assert bot._llm_editorial_cached(japanese) is None
    assert bot._llm_editorial_cached(english) is not None
    assert 'Witch' in caplog.text


def test_russian_source_does_not_get_new_latin_names():
    # Студии в источнике нет — модель взяла её из памяти.
    assert llm._editorial_rejection(
        'Анонсирован второй сезон «Волчицы и пряностей»', 'Анонсирован второй сезон',
        'Анимацией займётся Passione.') == 'unsupported_names'


def test_formats_and_platforms_are_not_names():
    assert llm._editorial_rejection(
        'Анонсирован второй сезон, премьера в 2027 году', 'Анонсирован второй сезон',
        'Выйдет на Netflix, OVA и Blu-ray позже.') == ''


@pytest.fixture
def enrich_env(tmp_path, monkeypatch):
    """Настроенная модель, как в tests/test_llm.py."""
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://x/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'fetch_article', lambda link: {'text': '', 'video': None})
    bot._pending_admin_alerts.clear()


def _answer(payload: dict):
    response = MagicMock(status_code=200)
    response.json.return_value = {'choices': [{'message': {'content': json.dumps(payload)}}]}
    return response


def test_rewrite_with_foreign_title_falls_back_and_logs_the_names(enrich_env, caplog):
    news = {'title': LAYTON, 'summary': 'The trailer shows new puzzles.', 'source': 'Polygon'}
    answer = {'topic': 'игры', 'kind': 'трейлер', 'subject': 'Professor Layton',
              'title': 'Профессор Лейтон выйдет 10 декабря',
              'summary': 'Game Tengoku: THE GAME PARADISE! показал новые головоломки.',
              'tags': ['#игры']}
    with caplog.at_level(logging.WARNING), \
            patch.object(bot.requests, 'post', return_value=_answer(answer)):
        asyncio.run(bot._llm_enrich(news))
    assert '_llm_text' not in news
    assert news['_editorial_rejection'] == 'unsupported_names'
    assert 'Tengoku' in caplog.text


# ----------------------------------------------------------- вес темы

def _news(title, source='MyAnimeList', summary='', **extra):
    return {'title': title, 'summary': summary, 'source': source, **extra}


def test_anime_beats_hollywood_box_office_at_equal_freshness():
    anime = _news('Frieren season 3 trailer reveals premiere date')
    hollywood = _news('Avengers: Doomsday presales reach $40 million ahead of premiere',
                      source='Variety')
    assert bot._news_priority_score(anime) > bot._news_priority_score(hollywood) + 5


def test_word_film_alone_no_longer_boosts():
    plain = _news('Studio hires new producer', source='Variety')
    film = _news('Studio hires new producer for the film', source='Variety')
    assert bot._news_priority_score(film) == bot._news_priority_score(plain)


def test_visuals_are_preferred_and_merch_is_not():
    frames = _news('New frames from episode 10 of the anime')
    merch = _news('Blu-ray release with bonus figures for the anime')
    neutral = _news('Anime gets new staff member')
    assert bot._topic_affinity(frames) > bot._topic_affinity(neutral) > bot._topic_affinity(merch)


def test_gossip_penalty_only_for_non_anime_news():
    rumor_film = _news('Rumor: actor in talks for a superhero sequel', source='Collider')
    film = _news('Actor joins a superhero sequel', source='Collider')
    rumor_anime = _news('Rumor: Chainsaw Man season 2 in production')
    anime = _news('Chainsaw Man season 2 in production')
    assert bot._topic_affinity(rumor_film) < bot._topic_affinity(film)
    assert bot._topic_affinity(rumor_anime) == bot._topic_affinity(anime)


def test_model_topic_decides_for_telegram_channels():
    tg = _news('Новый постер сериала', source='TG: Geek', _llm_topic='кино')
    tg_anime = _news('Новый постер сериала', source='TG: Geek', _llm_topic='аниме')
    assert bot._topic_affinity(tg_anime) - bot._topic_affinity(tg) == 5.0


# --------------------------------------- заголовок, оборванный переносом

@pytest.mark.parametrize('post, title', [
    # Запятая или союз в конце первой строки — фраза продолжается.
    ('Согласно ранним прогнозам,\nновый фильм соберёт 200 млн в первый уикенд.',
     'Согласно ранним прогнозам, новый фильм соберёт 200 млн в первый уикенд.'),
    ('Инсайдер пишет, что\nгерой не уйдёт на второй план.\nПодробности позже.',
     'Инсайдер пишет, что герой не уйдёт на второй план.'),
    # Название в кавычках на следующей строке — продолжение той же фразы.
    ('Новый кадр мультфильма\n«Барашек и чудище».', 'Новый кадр мультфильма «Барашек и чудище».'),
    # Предлог в конце строки.
    ('Свежий постер с героями из\n«Человека-бензопилы» к второму сезону.',
     'Свежий постер с героями из «Человека-бензопилы» к второму сезону.'),
    # Висящая запятая решает даже перед заглавной буквой.
    ('Согласно данным инсайдеров,\nNetflix готовит продолжение сериала.',
     'Согласно данным инсайдеров, Netflix готовит продолжение сериала.'),
    # Строчная буква — продолжение фразы и без висящего слова.
    ('Новый тизер второго сезона\nпоказал главного героя.',
     'Новый тизер второго сезона показал главного героя.'),
])
def test_headline_broken_by_a_line_break_is_joined(post, title):
    assert bot._tg_title_and_summary(post, 'ch', 'TG: Ch')[0] == title


@pytest.mark.parametrize('post, title', [
    # Законченный заголовок без точки и тело с заглавной — не склеиваются.
    ('Анонсировано аниме по манге «Икс»\nПремьера в 2027 году.', 'Анонсировано аниме по манге «Икс»'),
    # После двоеточия — список, он остаётся телом.
    ('Подробности второго сезона:\n• премьера 5 октября\n• 12 серий', 'Подробности второго сезона:'),
    # Пункт списка тире не приклеивается даже к оборванной строке.
    ('Что известно о сезоне,\n— 12 серий\n— осень', 'Что известно о сезоне,'),
    # Двоеточие — не обрыв: следом идёт пояснение, а не середина фразы.
    ('Подробности второго сезона:\nПремьера 5 октября.', 'Подробности второго сезона:'),
    # Фраза уже закончена точкой — строка со строчной её не продолжает.
    ('Анонсирован сезон «Икс».\nвыйдет осенью', 'Анонсирован сезон «Икс».'),
])
def test_finished_headline_and_lists_stay_apart(post, title):
    assert bot._tg_title_and_summary(post, 'ch', 'TG: Ch')[0] == title
