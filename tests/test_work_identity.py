"""Одна новость на разных языках — одна история.

Живой прогон 23.09.2026: анонс 4-го сезона «Необъятного океана» пришёл из
семи источников на трёх языках, и бот не склеил ни одного — без модели канал
получил бы его семь раз. Сравнение заголовков тут бессильно: в «Необъятном
океане» и «Grand Blue» нет общих букв. Склеивает тайтл, опознанный по базе
Shikimori, где у него есть все эти имена.

Заголовки ниже — формы, встреченные в живых лентах.
"""
import json
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot
from news_stories import (same_work_event, story_event_classes, story_work_names,
                          story_work_numbers)


# ---------- разбор заголовка ----------

@pytest.mark.parametrize(('title', 'name'), [
    ('Постер к 4-му сезону аниме "Необъятный океан".', 'Необъятный океан'),
    ('🌊Анонсирован 4 сезона аниме Необъятный океан.', 'Необъятный океан'),
    ('🌊 Инсайды: Аниме «Необъятный океан» продлили на 4 сезон.', 'Необъятный океан'),
    ('🚔Новый трейлер аниме по манге Super Psychic Policeman Chojo (Полицейский-экстрасенс Тёдзо!)',
     'Super Psychic Policeman Chojo'),
    ("📺 Annunciata la quarta stagione dell'anime di Grand Blue Dreaming, che inizierà prossimamente.",
     'Grand Blue Dreaming'),
    ("📺 Annunciato il secondo cour dell'anime di Though I Am an Inept Villainess",
     'Though I Am an Inept Villainess'),
    ('Grand Blue Season 4 Announced With Teaser Visual', 'Grand Blue'),
    ('Cyberpunk: Edgerunners 2 New "Cyberpsycho" Sneak Peek Trailer', 'Cyberpunk: Edgerunners'),
    ('アニメ『さよならララ』谷紫織描き下ろしのメモリアルビジュアル解禁', 'さよならララ'),
])
def test_work_name_is_found_in_any_language(title, name):
    assert name in story_work_names(title)


@pytest.mark.parametrize('title', [
    'Опенинг аниме «Гонка» — песня «SPIN» от Кэйти',
    "📺 Opening dell'anime Steel Ball Run (Parte 7): \"SPIN\" di Kroi",
])
def test_song_in_quotes_is_not_a_work(title):
    """«SPIN» — песня. Как тайтл она нашлась бы в базе и склеила чужие новости."""
    assert 'SPIN' not in story_work_names(title)


@pytest.mark.parametrize('title', [
    "📺 Climax visual dell'anime (attualmente in corso) DIGIMON BEATBREAK.",
    "📺 Opening (pre-rilascio) dell'anime Steel Ball Run",
])
def test_lowercase_parenthesis_is_a_remark_not_a_title(title):
    assert not any(name.startswith(('attualmente', 'pre-rilascio')) for name in story_work_names(title))


@pytest.mark.parametrize(('title', 'classes'), [
    ('Grand Blue Season 4 Announced With Teaser Visual', {'announce', 'visual'}),  # не трейлер
    ('Тизер-постер аниме «Икс»', {'visual'}),
    ('Постер и трейлер к аниме "Цветочный круг мастера барьеров".', {'visual', 'trailer'}),
    ('🏥 Автор «Монолога фармацевта» Нацу Хюга попала в больницу.', {'health'}),
    ('The Apothecary Diaries Author Natsu Hyuuga Gets Hospitalized', {'health'}),
    ('⚔️ В ресторанах «Бургер Кинг» стартовала коллаборация с «Атакой титанов».', {'collab'}),
])
def test_event_type_is_read_in_any_language(title, classes):
    assert set(story_event_classes(title)) == classes


@pytest.mark.parametrize(('title', 'numbers'), [
    ('Cyberpunk: Edgerunners 2 First Look Sneak Peek', {'2'}),          # «First Look» — не номер
    ('Solo Leveling Reveals First Look Ahead of October 22 Release', set()),  # дата — не номер
    ('Премьера аниме «Икс» состоится в январе 2027 года', set()),        # год — не номер
    ('Кадры к 1-й серии 2-го сезона аниме "Чёрный клевер".', {'1', '2'}),
    ('Black Clover Season 2 Episode 1 Preview and Synopsis Revealed', {'1', '2'}),
    ("📺 Annunciata la quarta stagione dell'anime di Grand Blue Dreaming", {'4'}),
    ('Katekyo Hitman Reborn! Anime Reveals Fourteenth Ending', {'14'}),
    ('Katekyo Hitman Reborn! Anime Reveals Seventh Opening', {'7'}),
])
def test_season_and_episode_numbers(title, numbers):
    assert set(story_work_numbers(title)) == numbers


# ---------- одна ли это новость ----------

def _item(title, key='shiki:37105'):
    return {'title': title, '_work_key': key}


def test_same_announcement_in_three_languages_is_one_event():
    ru = _item('Постер к 4-му сезону аниме "Необъятный океан".')
    en = _item('Grand Blue Season 4 Announced With Teaser Visual')
    it = _item("📺 Annunciata la quarta stagione dell'anime di Grand Blue Dreaming")
    assert same_work_event(ru, en) and same_work_event(en, it) and same_work_event(ru, it)


@pytest.mark.parametrize(('a', 'b'), [
    # Разный номер: сезоны 3 и 4.
    (_item('Grand Blue Season 4 Announced'), _item('Grand Blue Season 3 Announced')),
    # Разные песни одного тайтла.
    (_item('Reborn! Reveals Fourteenth Ending', 'shiki:1604'),
     _item('Reborn! Reveals Seventh Opening', 'shiki:1604')),
    # Трейлер и анонс — разные новости.
    (_item('Grand Blue Season 4 New Trailer'), _item('Grand Blue Season 4 Announced')),
    # Аниме и манга ушли на перерыв — разные новости.
    (_item('Аниме «Ван-Пис» уходит на перерыв', 'shiki:21'),
     _item('Il manga ONE PIECE sarà in pausa', 'shiki:21')),
    # Тип события не распознан — у большой франшизы таких новостей несколько в день.
    (_item('⚔️ «Бургер Кинг» и «Атака титанов»: что внутри', 'shiki:16498'),
     _item('Attack on Titan Comeback at MAPPA', 'shiki:16498')),
    # Другой тайтл.
    (_item('Grand Blue Season 4 Announced'), _item('Black Clover Season 4 Announced', 'shiki:34572')),
])
def test_different_news_of_one_work_stay_apart(a, b):
    assert not same_work_event(a, b)


def test_without_a_key_the_titles_decide_as_before():
    assert not same_work_event(_item('Grand Blue Season 4 Announced', None),
                               _item('Grand Blue Season 4 Announced', None))


# ---------- сверка с базой тайтлов ----------

GRAND_BLUE = {'id': '37105', 'name': 'Grand Blue', 'russian': 'Необъятный океан',
              'english': 'Grand Blue Dreaming', 'japanese': 'ぐらんぶる', 'synonyms': []}


@pytest.fixture
def shikimori(monkeypatch, tmp_path):
    """Подставной Shikimori: отвечает заданным списком и считает вызовы."""
    calls = []

    def serve(answers):
        def post(url, json=None, **kwargs):
            calls.append(json['variables']['s'])
            answer = answers(json['variables']['s']) if callable(answers) else answers
            if isinstance(answer, Exception):
                raise answer
            return MagicMock(status_code=200, json=lambda: {'data': {'animes': answer}})
        monkeypatch.setattr(bot.requests, 'post', post)
        resolver = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
        resolver.MIN_INTERVAL = 0
        monkeypatch.setattr(bot, 'work_titles', resolver)
        monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
        monkeypatch.setattr(bot, 'sent_links', None)
        return resolver
    serve.calls = calls
    return serve


@pytest.mark.parametrize('name', ['Необъятный океан', 'Grand Blue', 'Grand Blue Dreaming', 'ぐらんぶる'])
def test_every_name_of_the_work_gives_one_key(shikimori, name):
    resolver = shikimori([GRAND_BLUE])
    assert resolver.lookup(name) == 'shiki:37105'


def test_a_lookalike_answer_is_not_accepted(shikimori):
    """Поиск Shikimori всегда что-нибудь находит: «CrosSing» он выдал за «Fumikiri Jikan»."""
    resolver = shikimori([{'id': '37188', 'name': 'Fumikiri Jikan', 'russian': 'Время у переезда',
                           'english': None, 'japanese': '踏切時間', 'synonyms': []}])
    assert resolver.lookup('CrosSing') == ''


def test_a_short_name_must_match_exactly(shikimori):
    resolver = shikimori([{'id': '1', 'name': 'Spine', 'russian': None, 'english': None,
                           'japanese': None, 'synonyms': []}])
    assert resolver.lookup('Spin') == ''


def test_known_names_do_not_go_to_the_network_again(shikimori, tmp_path):
    shikimori([GRAND_BLUE])
    items = [{'title': 'Постер к 4-му сезону аниме "Необъятный океан".', 'link': 'https://t.me/a/1'}]
    assert bot._annotate_work_keys(items) == 1
    again = [{'title': '🌊Анонсирован 4 сезона аниме Необъятный океан.', 'link': 'https://t.me/b/2'}]
    assert bot._annotate_work_keys(again) == 1
    assert shikimori.calls == ['Необъятный океан']
    # Кеш переживает перезапуск.
    fresh = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
    assert fresh.cached('Необъятный океан') == 'shiki:37105'


def test_a_miss_is_remembered_too(shikimori):
    resolver = shikimori([])
    assert resolver.lookup('Ничего похожего') == ''
    assert resolver.cached('Ничего похожего') == ''


NAMES = ['Альфа центавра', 'Бета версия', 'Гамма лучи', 'Дельта реки', 'Эпсилон окрестность',
         'Дзета функция', 'Эта звезда', 'Тета ритм', 'Йота капли', 'Каппа водяной']


def test_network_failure_is_not_remembered_and_stops_the_cycle(shikimori):
    """Сеть не ответила — ключа нет, новость склеится по-старому. Три отказа — хватит."""
    import requests
    resolver = shikimori(requests.ConnectionError('down'))
    items = [{'title': f'Постер к аниме «{name}»', 'link': f'https://x/{i}'} for i, name in enumerate(NAMES)]
    assert bot._annotate_work_keys(items) == 0
    assert len(shikimori.calls) == 3
    assert resolver.cached(NAMES[0]) is None


def test_lookup_budget_per_cycle(shikimori):
    shikimori([])
    items = [{'title': f'Постер к аниме «{name}»', 'link': f'https://x/{i}'} for i, name in enumerate(NAMES)]
    bot._annotate_work_keys(items, budget=4)
    assert len(shikimori.calls) == 4


def test_already_sent_links_are_not_looked_up(shikimori, monkeypatch):
    shikimori([GRAND_BLUE])
    monkeypatch.setattr(bot, 'sent_links', {'https://t.me/a/1'})
    items = [{'title': 'Постер к 4-му сезону аниме "Необъятный океан".', 'link': 'https://t.me/a/1'}]
    assert bot._annotate_work_keys(items) == 0 and shikimori.calls == []


def test_switched_off_means_no_network(shikimori, monkeypatch):
    shikimori([GRAND_BLUE])
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', False)
    items = [{'title': 'Постер к 4-му сезону аниме "Необъятный океан".', 'link': 'https://t.me/a/1'}]
    assert bot._annotate_work_keys(items) == 0 and shikimori.calls == []


# ---------- склейка в пачке и между циклами ----------

LIVE_GRAND_BLUE = [
    ('TG: Nexvlsz', 'Постер к 4-му сезону аниме "Необъятный океан".'),
    ('TG: CurrentAnime', '🌊Анонсирован 4 сезона аниме Необъятный океан.'),
    ('TG: animetarakans', '🌊 Инсайды: Аниме «Необъятный океан» продлили на 4 сезон.'),
    ('Anime Corner', 'Grand Blue Season 4 Announced With Teaser Visual'),
    ('TG: VanitasNews', "📺 Annunciata la quarta stagione dell'anime di Grand Blue Dreaming"),
]


def test_seven_sources_become_one_story(shikimori, monkeypatch):
    shikimori([GRAND_BLUE])
    monkeypatch.setattr(bot, 'story_registry', None)
    items = [{'title': title, 'source': source, 'link': f'https://x/{i}', 'summary': ''}
             for i, (source, title) in enumerate(LIVE_GRAND_BLUE)]
    bot._annotate_work_keys(items)
    stories = bot._cluster_news(items, persist_intelligence=False)
    assert len(stories) == 1
    assert set(stories[0]['_story_sources']) == {source for source, _ in LIVE_GRAND_BLUE}


def test_without_titles_they_were_five_posts(monkeypatch):
    """Страховка от вакуума: без ключа тайтла эти же заголовки не склеиваются."""
    monkeypatch.setattr(bot, 'story_registry', None)
    items = [{'title': title, 'source': source, 'link': f'https://x/{i}', 'summary': ''}
             for i, (source, title) in enumerate(LIVE_GRAND_BLUE)]
    assert len(bot._cluster_news(items, persist_intelligence=False)) == 5


def test_story_delivered_yesterday_is_a_duplicate_today(tmp_path):
    """Русские каналы часто пишут позже английских: склейка нужна и между циклами."""
    registry = bot.StoryRegistry(tmp_path / 'stories.json')
    en = {'title': 'Grand Blue Season 4 Announced With Teaser Visual', 'link': 'https://ac/1',
          '_work_key': 'shiki:37105'}
    memory = registry.observe(en, ['Anime Corner'], [en['link']])
    registry.mark_delivery({**en, '_story_registry_id': memory['registry_id']}, published=True)
    ru = {'title': '🌊 Инсайды: Аниме «Необъятный океан» продлили на 4 сезон.',
          'link': 'https://t.me/tar/1', '_work_key': 'shiki:37105'}
    assert registry.observe(ru, ['TG: animetarakans'], [ru['link']])['delivery_duplicate'] is True


def test_other_news_of_the_same_work_is_not_a_duplicate(tmp_path):
    registry = bot.StoryRegistry(tmp_path / 'stories.json')
    en = {'title': 'Grand Blue Season 4 Announced With Teaser Visual', 'link': 'https://ac/1',
          '_work_key': 'shiki:37105'}
    memory = registry.observe(en, ['Anime Corner'], [en['link']])
    registry.mark_delivery({**en, '_story_registry_id': memory['registry_id']}, published=True)
    trailer = {'title': 'Трейлер 4 сезона аниме «Необъятный океан»', 'link': 'https://t.me/n/2',
               '_work_key': 'shiki:37105'}
    assert registry.observe(trailer, ['TG: Nexvlsz'], [trailer['link']])['delivery_duplicate'] is False


def test_cache_file_is_plain_json(shikimori, tmp_path):
    shikimori([GRAND_BLUE]).lookup('Необъятный океан')
    bot.work_titles.flush()
    data = json.loads((tmp_path / 'work_titles.json').read_text(encoding='utf-8'))
    assert data['titles']['необъятныйокеан']['key'] == 'shiki:37105'


@pytest.mark.parametrize('title', [
    "'Katainaka no Ossan, Kensei ni Naru' Gets Third Season",     # MyAnimeList
    'Аниме «Необъятный океан» получит четвёртый сезон',
])
def test_gets_a_season_is_an_announcement(title):
    assert 'announce' in story_event_classes(title)


def test_lookups_stop_at_the_time_limit(shikimori, monkeypatch):
    """Пустой кеш не должен растягивать цикл сбора: что не успели — в следующий раз."""
    shikimori([])
    monkeypatch.setattr(bot, 'WORK_LOOKUP_WALL_SEC', -1)
    items = [{'title': f'Постер к аниме «{name}»', 'link': f'https://x/{i}'} for i, name in enumerate(NAMES)]
    bot._annotate_work_keys(items)
    assert shikimori.calls == []
