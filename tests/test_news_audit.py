"""Аудит новостного конвейера 24.09.2026: живой прогон без модели.

Бот собрал 52 кандидата из всех лент и отрисовал посты так, как они ушли
бы в канал. Каждый тест ниже — дефект, который там воспроизвёлся:
подменённые AniList названия, английские посты в русском канале, по два-три
поста об одной новости, потерянные настоящие новости, не-новости и спойлеры.
Заголовки и тексты — реальные, иногда сокращённые.
"""
import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

import anime_news_bot as bot
import translation


# ---------- п.1. AniList больше не переписывает текст ----------

class _FuzzyAniList:
    """AniList отвечает на любой запрос: поиск нечёткий, пусто не бывает.

    Ответы — настоящие: «October» → «Saint October», «Character Design» →
    «Isekai Character Design ni Okeru Succubus no Seitai», «Mushoku Tensei» →
    сам «Mushoku Tensei: Isekai Ittara Honki Dasu».
    """
    KNOWN = {
        'mushoku tensei': {'romaji': 'Mushoku Tensei: Isekai Ittara Honki Dasu',
                           'english': 'Mushoku Tensei: Jobless Reincarnation',
                           'native': '無職転生 ～異世界行ったら本気だす～', 'synonyms': []},
        'isshiki-san wants to know about love': {
            'romaji': 'Isshiki-san wa Koi wo Shiritai.', 'english': None,
            'native': '一式さんは恋を知りたい。',
            'synonyms': ['Isshiki-san Wants to Know About Love.']},
        'phantom busters': {'romaji': 'Phantom Busters', 'english': None,
                            'native': 'ファントムバスターズ', 'synonyms': []},
    }
    WRONG = {'romaji': 'Shunkashuutou Daikousha: Haru no Mai', 'english': None,
             'native': '春夏秋冬代行者 春の舞', 'synonyms': []}

    def __init__(self):
        self.asked = []

    def lookup(self, query):
        self.asked.append(query)
        return self.KNOWN.get(query.casefold().strip(), self.WRONG)


@pytest.fixture
def fuzzy_anilist(monkeypatch):
    client = _FuzzyAniList()
    monkeypatch.setattr(bot, 'anilist', client)
    return client


@pytest.mark.parametrize('text', [
    'Rudy Takes Action in Mushoku Tensei Season 3 Episode 14 Preview',
    'The police rom-com series debuts in January 2027',
    'French / Japanese co-production begins broadcasting on October 2',
    'Isshiki-san Wants to Know About Love Anime Unveils Main Visual, More Cast',
    'Series Composition: Koudai Minami • Character Design: Masatoshi Tsuji.',
])
def test_anilist_never_writes_another_title_into_the_text(fuzzy_anilist, text):
    """«in October» → «in Saint October», «Preview» → «Shunkashuutou Daikousha».

    Защищать можно только то, что уже написано в тексте: после восстановления
    плейсхолдеров текст обязан совпасть с исходным.
    """
    protected, placeholders = bot.anilist_protect_titles(text, start_index=2000)
    assert all(value in text for value in placeholders.values()), placeholders
    assert bot.restore_terms(protected, placeholders) == text
    assert not any('Shunkashuutou' in value for value in placeholders.values())


def test_single_words_and_months_are_not_sent_to_anilist(fuzzy_anilist):
    bot.anilist_protect_titles('The anime premieres in October. Main Visual and New Cast revealed.')
    assert not any(len(q.split()) < 2 for q in fuzzy_anilist.asked), fuzzy_anilist.asked
    assert 'Main Visual' not in fuzzy_anilist.asked
    assert 'New Cast' not in fuzzy_anilist.asked


def test_real_title_is_protected_as_written(fuzzy_anilist):
    """«Mushoku Tensei Season 3…» — защищается «Mushoku Tensei», как в тексте."""
    text = 'Mushoku Tensei Season 3 revealed the Episode 14 preview images.'
    protected, placeholders = bot.anilist_protect_titles(text, start_index=2000)
    assert list(placeholders.values()) == ['Mushoku Tensei']
    assert protected.startswith('〖2000〗 Season 3')


def test_stale_cache_entry_is_not_trusted(tmp_path, monkeypatch):
    """Кеш AniList хранил «найдено» неделями: старая запись с чужим тайтлом
    не должна ни подменять текст, ни защищать обычные слова от перевода."""
    from datetime import datetime
    cache = tmp_path / 'anilist.json'
    cache.write_text(json.dumps({'character design': {
        'found': True, 'romaji': 'Isekai Character Design ni Okeru Succubus no Seitai',
        'english': None, 'native': None, 'checked_at': datetime.now().isoformat()}}),
        encoding='utf-8')
    client = bot.AniListClient(cache)
    monkeypatch.setattr(client, '_query_api', MagicMock(return_value=None))
    monkeypatch.setattr(bot, 'anilist', client)
    text = 'Staff list. Character Design: Masatoshi Tsuji.'
    protected, placeholders = bot.anilist_protect_titles(text, start_index=2000)
    assert placeholders == {}
    assert protected == text


def test_known_work_name_is_protected_without_anilist(monkeypatch):
    """Тайтл, уже опознанный по Shikimori, не уходит в переводчик."""
    monkeypatch.setattr(bot, 'anilist', None)
    protected, placeholders = bot.anilist_protect_titles(
        'Grand Blue Season 4 Announced With Teaser Visual', known_names=('Grand Blue',))
    assert list(placeholders.values()) == ['Grand Blue']
    assert 'Grand Blue' not in protected


# ---------- п.2–3. Непереведённый пост не уходит в канал ----------

class _TooManyRequests:
    def translate(self, text):
        raise RuntimeError('429 Client Error: Too Many Requests')


@pytest.fixture
def translator_down(monkeypatch):
    monkeypatch.setattr(bot, 'translator', _TooManyRequests())
    monkeypatch.setattr(bot, 'anilist', None)
    monkeypatch.setattr(bot, 'DEEPL_API_KEY', '')
    monkeypatch.setattr(bot, '_translation_cache', {})


def _grand_blue():
    # Опознанный тайтл: бот сам вставит «Необъятный океан» и хэштег — это
    # кириллица, которой у переводчика не было.
    return {'title': 'Grand Blue Season 4 Announced With Teaser Visual',
            'summary': 'The anime returns after the third season finale on October 22.',
            'link': 'https://animecorner.me/grand-blue-season-4/', 'source': 'Anime Corner',
            '_work_key': 'shiki:37105', '_work_name': 'Grand Blue',
            '_work_russian': 'Необъятный океан'}


def test_failed_translation_with_russian_title_is_detected(translator_down):
    assert bot._left_untranslated(_grand_blue()) is True


def _prepare_patched(monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(local_topic_filter=True, translator_engine='google'))
    for name in ('_improve_thumb', '_discover_article_video', '_optimize_news_media'):
        monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
    monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='off'))
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))


def test_translator_429_with_russian_title_defers_the_post(translator_down, monkeypatch):
    _prepare_patched(monkeypatch)
    result = asyncio.run(bot._prepare_news_for_send(_grand_blue(), 'Anime Corner',
                                                    apply_dedup=False))
    assert result == 'deferred'


def test_untranslated_post_is_not_deferred_forever(translator_down, monkeypatch, tmp_path):
    """Каждый заход заново качал медиа: без счётчика новость ждала вечно."""
    _prepare_patched(monkeypatch)
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bot, 'TRANSLATION_DEFER_MAX_ATTEMPTS', 2)
    results = [asyncio.run(bot._prepare_news_for_send(_grand_blue(), 'Anime Corner',
                                                      apply_dedup=False)) for _ in range(4)]
    assert results[:2] == ['deferred', 'deferred']
    assert results[2:] == ['skipped_filter', 'skipped_filter']


class _Response:
    def __init__(self, status, text):
        self.status_code, self.text = status, text
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Client Error', response=self)

    def close(self):
        self.closed = True


def test_dict_endpoint_translates_when_mobile_page_is_rate_limited(monkeypatch):
    """/m отвечал 429 весь прогон, словарный endpoint — 200 на тот же текст."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if 'clients5' in url:
            return _Response(200, '[["Аниме-адаптация Phantom Busters анонсирована на 2027 год","en"]]')
        return _Response(429, 'Too Many Requests')

    monkeypatch.setattr(requests, 'get', get)
    out = translation.GoogleTranslator(source='auto', target='ru').translate(
        'Phantom Busters Anime Adaptation Announced for 2027')
    assert out == 'Аниме-адаптация Phantom Busters анонсирована на 2027 год'
    assert len(calls) == 1


def test_rate_limited_endpoint_is_paused(monkeypatch):
    """После 429 endpoint не дёргается на каждый пост — это продлевает бан."""
    calls = []

    def get(url, **kwargs):
        calls.append('dict' if 'clients5' in url else 'mobile')
        if 'clients5' in url:
            return _Response(429, '')
        return _Response(200, '<div class="t0">перевод</div>')

    monkeypatch.setattr(requests, 'get', get)
    translator = translation.GoogleTranslator(source='auto', target='ru')
    assert translator.translate('first') == 'перевод'
    assert translator.translate('second') == 'перевод'
    assert calls == ['dict', 'mobile', 'mobile']


# ---------- п.4. Одна история — один пост ----------

# Настоящие записи Shikimori (поля сокращены) и соседи, которых нечёткий поиск
# возвращает вместе с ними.
SHIKIMORI = [
    {'id': '62808', 'name': 'Isshiki-san wa Koi wo Shiritai.', 'russian': 'Иссики хочет узнать о любви',
     'english': None, 'japanese': '一式さんは恋を知りたい。', 'synonyms': []},
    {'id': '37999', 'name': 'Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen',
     'russian': 'Госпожа Кагуя: в любви как на войне', 'english': 'Kaguya-sama: Love is War',
     'japanese': None, 'synonyms': []},
    {'id': '65009', 'name': 'Aoki Denshou Welsh & Shedar', 'russian': 'Лазурные сказания Уэлша и Шедара',
     'english': None, 'japanese': '蒼き伝承 ウェルシュ＆シェダー',
     'synonyms': ['Welsh & Shedar', 'The Azure Legend: Welsh & Shedar', 'Aoki Densho Welsh & Shedar']},
    {'id': '42310', 'name': 'Cyberpunk: Edgerunners', 'russian': 'Киберпанк: Бегущие по краю',
     'english': 'Cyberpunk: Edgerunners', 'japanese': 'サイバーパンク エッジランナーズ', 'synonyms': []},
    {'id': '34572', 'name': 'Black Clover', 'russian': 'Чёрный клевер', 'english': 'Black Clover',
     'japanese': 'ブラッククローバー', 'synonyms': []},
    {'id': '31564', 'name': 'Sansha Sanyou', 'russian': 'Трёхсторонний клевер', 'english': None,
     'japanese': None, 'synonyms': []},
    {'id': '59741', 'name': 'Tsuihou sareta Tensei Juukishi wa Game Chishiki de Musou suru',
     'russian': 'Изгнанный реинкарнированный тяжёлый рыцарь не имеет себе равных в знаниях игры',
     'english': 'The Exiled Heavy Knight Knows How to Game the System', 'japanese': None, 'synonyms': []},
    {'id': '39535', 'name': 'Mushoku Tensei: Isekai Ittara Honki Dasu',
     'russian': 'Реинкарнация безработного: История о приключениях в другом мире',
     'english': 'Mushoku Tensei: Jobless Reincarnation',
     'japanese': '無職転生 ～異世界行ったら本気だす～', 'synonyms': []},
    {'id': '59443', 'name': 'Reincarnation no Kaben', 'russian': 'Лепестки реинкарнации',
     'english': None, 'japanese': None, 'synonyms': []},
    {'id': '5682', 'name': 'Phantom: Requiem for the Phantom', 'russian': 'Фантом: Реквием по Призраку',
     'english': 'Phantom: Requiem for the Phantom', 'japanese': None, 'synonyms': []},
]


def _words(value):
    return set(re.findall(r'\w+', str(value or '').casefold()))


def _shikimori_post(url, json=None, **kwargs):
    """Нечёткий поиск, как у Shikimori: три записи с общими словами."""
    query = _words(json['variables']['s'])
    scored = []
    for row in SHIKIMORI:
        names = [row['name'], row['russian'], row['english'], row['japanese'], *row['synonyms']]
        score = len(query & set().union(*(_words(n) for n in names)))
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: -pair[0])
    response = MagicMock(status_code=200)
    response.json.return_value = {'data': {'animes': [row for _, row in scored[:3]]}}
    return response


# Заголовки и начало текста — как в ленте 24.09. Каждая история ушла бы в
# канал двумя-тремя постами.
STORIES = {
    'phantom': [
        ('MyAnimeList', None, "'Phantom Busters' TV Anime Announced For 2027",
         "A television anime adaptation of Shoco's Phantom Busters manga was announced on Thursday."),
        ('Shikimori', 'ru', 'Анонс и год премьеры аниме-экранизации манги «Phantom Busters»',
         'По манге «Phantom Busters» (Охотники за привидениями) анонсировали аниме-сериал. Премьера в 2027 году.'),
        ('Anime Corner', None, 'Phantom Busters Anime Adaptation Announced for 2027',
         "Neoshoco's Phantom Busters manga is officially getting a TV anime adaptation in 2027."),
        ('TG: CurrentAnime', 'ru', '😲 Анонсировано аниме по манге Phantom Busters (Охотники на призраков)',
         'Первоклассник Юджин Корекиши - отличник с нулевыми экстрасенсорными способностями.'),
    ],
    'isshiki': [
        ('MyAnimeList', None, "'Isshiki-san wa Koi wo Shiritai.' Announces Additional Cast",
         'The official website revealed additional cast and a key visual on Thursday.'),
        ('Shikimori', 'ru', 'Постер «Isshiki-san wa Koi wo Shiritai»',
         'Вышел постер аниме-сериала «Isshiki-san wa Koi wo Shiritai» (Иссики хочет узнать о любви).'),
        ('Crunchyroll', None, 'Isshiki-san Wants to Know About Love Anime Unveils Main Visual, More Cast',
         'The police rom-com series debuts in January 2027'),
    ],
    'welsh': [
        ('MyAnimeList', None, "'Aoki Denshou Welsh & Shedar' Reveals Additional Cast, Theme Songs, Main Promo",
         'The official website revealed additional cast, theme songs and the main promotional video.'),
        ('Crunchyroll', None, 'Aoki Densho Welsh & Shedar Anime Reveals Trailer, Additional Cast, Theme Song Info',
         'French / Japanese co-production begins broadcasting on October 2'),
        ('TG: VanitasNews', None, '📺 Main PV della serie animata original Welsh & Shedar, che inizierà '
         'ad essere trasmessa in Giappone dal 2 ottobre e sarà diretta da Naoki Horiuchi presso STUDIO MASSKET.',
         '❗Prodotta interamente in Giappone.'),
    ],
    'cyberpunk': [
        ('Shikimori', 'ru', 'Отрывок из аниме «Cyberpunk: Edgerunners 2»',
         'Компания Netflix опубликовала отрывок из предстоящего аниме. Премьера 20 октября 2026 года.'),
        ('TG: YtkaNews', 'ru', '⚡Тизер нового сезона аниме сериала «Киберпанк: Бегущие по краю»',
         'Премьера 20 октября! Производство студии TRIGGER.'),
        ('TG: Nexvlsz', 'ru', 'Отрывок к 1-й серии 2-го сезона "Киберпанк: Бегущие по краю".',
         'Снимает студия Trigger. Премьера 20-го октября.'),
    ],
    'clover': [
        ('TG: Advance', 'ru', '🍀 КЛЕВЕР ПОЙДЕТ ДО КОНЦА.',
         'Инсайдеры утверждают, что 2 сезон «Черного Клевера» станет финальным и охватит всю '
         'оставшуюся мангу. Причем за 26 серий сплит-куром.'),
        ('TG: animetarakans', 'ru', '🍀 Продолжение аниме «Черный клевер» полностью завершит экранизацию '
         'оригинальной манги.', 'Согласно инсайдам, долгожданный сиквел выйдет в формате двух раздельных куров.'),
        ('TG: VanitasNews', None, "📺⚠️ Stando ad un leak di SugoiLITE, la seconda stagione dell'anime di "
         "Black Clover adatterà tutto il materiale del manga in due cour non consecutivi, per un totale di "
         "26 episodi (ogni cour è attualmente previsto che abbia 13 episodi).", '⚠️ Sempre secondo il leak.'),
    ],
    'exiled': [
        ('Shikimori', 'ru', 'Постер и трейлер следующих серий «Tsuihou sareta Tensei Juukishi wa Game '
         'Chishiki de Musou suru»', 'Вышел постер и трейлер следующих серий.'),
        ('Anime Corner', None, 'The Exiled Heavy Knight Knows How to Game the System Part 2 Reveals Main '
         'Trailer, Visual, Theme Songs and New Cast', 'A new visual and main trailer for Part 2.'),
        ('TG: Advance', 'ru', '📛 Выдали постер ко 2 половине аниме «Изгнанный реинкарнированный тяжелый '
         'рыцарь не имеет себе равных в знаниях игры» (Tsuihou Sareta Tenshou Juu Kishi wa game Chishiki '
         'de Suru).', 'В сериале 26 эпизодов, так что он остается с нами на осень.'),
        ('TG: YtkaNews', 'ru', '⚔️Постер аниме «Изгнанный реинкарнированный тяжёлый рыцарь не имеет себе '
         'равных в знаниях игры»', 'Новые серии выходят по четвергам.'),
        ('TG: VanitasNews', None, "📺 Key visual e main PV della Parte 2 dell'anime (attualmente in corso) "
         'di The Exiled Heavy Knight Knows How to Game the System.', ''),
    ],
    'mushoku14': [
        ('Anime Corner', None, 'Rudy Takes Action in Mushoku Tensei Season 3 Episode 14 Preview',
         'Mushoku Tensei Season 3 revealed the Episode 14 preview images ahead of its September 27 premiere.'),
        ('TG: Advance', 'ru', 'Выдали кадры к 14 серии 3 сезона «Реинкарнации Безработного» (Mushoku '
         'Tensei), после которой сериал отправится на перерыв.', 'Прибудет эпизод 27 сентября.'),
        ('TG: CurrentAnime', 'ru', '😊Кадры к 14 серии 3 сезона аниме Реинкарнация безработного.',
         'Серия выйдет 27 сентября!'),
    ],
    # Тот же день, похожие слова — но другие новости: склеивать их нельзя.
    'other_clover': [
        ('TG: anilibria', 'ru', 'Клевер удачи или главный кошмар?',
         'Анонсировано аниме по манге "Ты — четырехлистный клевер" (Kimi wa Yotsuba no Clover). '
         'Подробности позже.'),
    ],
    'other_netflix': [
        ('ComicBook Anime', None, 'Netflix Locks Down First Major 2027 Anime Exclusive – Watch Trailer',
         'Netflix has done a remarkable job building up its anime catalogue, streaming Cyberpunk: Edgerunners.'),
    ],
    'other_mushoku13': [
        ('TG: YtkaNews', 'ru', 'Кадры к 13 серии 3 сезона аниме «Реинкарнация безработного»',
         'Серия выйдет 20 сентября.'),
    ],
}


@pytest.fixture
def audit_cycle(monkeypatch, tmp_path, fuzzy_anilist):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
    resolver = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
    resolver.MIN_INTERVAL = 0
    monkeypatch.setattr(bot, 'work_titles', resolver)
    monkeypatch.setattr(bot.requests, 'post', _shikimori_post)
    items = []
    for story, rows in STORIES.items():
        for idx, (source, lang, title, summary) in enumerate(rows):
            items.append({'title': title, 'summary': summary, 'source': source, 'lang': lang,
                          'link': f'https://example.test/{story}/{idx}', '_audit_story': story,
                          'images': [f'https://example.test/{story}/{idx}.jpg']})
    return items


def test_seven_stories_from_one_cycle_become_seven_posts(audit_cycle):
    bot._annotate_work_keys(audit_cycle, budget=200)
    clusters = bot._cluster_news(audit_cycle, persist_intelligence=False)
    story_of = {item['link']: item['_audit_story'] for item in audit_cycle}
    stories_per_cluster = [{story_of[link] for link in c['_story_links']} for c in clusters]
    clusters_per_story = {story: sum(story in found for found in stories_per_cluster)
                          for story in STORIES}
    assert clusters_per_story == {story: 1 for story in STORIES}
    # И ни одна история не проглотила чужую.
    assert all(len(found) == 1 for found in stories_per_cluster), stories_per_cluster


def test_number_on_one_side_only_is_not_a_different_story():
    from news_stories import same_work_event
    a = {'title': 'Отрывок из аниме «Cyberpunk: Edgerunners 2»', '_work_key': 'shiki:42310'}
    b = {'title': '⚡Тизер нового сезона аниме сериала «Киберпанк: Бегущие по краю»',
         '_work_key': 'shiki:42310'}
    c = {'title': 'Отрывок к 3-й серии 2-го сезона "Киберпанк: Бегущие по краю".',
         '_work_key': 'shiki:42310'}
    d = {'title': 'Отрывок к 4-й серии 2-го сезона "Киберпанк: Бегущие по краю".',
         '_work_key': 'shiki:42310'}
    assert same_work_event(a, b) and same_work_event(a, c)
    assert not same_work_event(c, d)        # номера у обоих, и они разные


def test_cluster_is_joined_through_any_member():
    """Первой в кластер попала новость без ключа тайтла (лимит запросов к
    базе кончился), второй — тот же заголовок с ключом. Английский пересказ
    с тем же ключом сравнивался только с первой и уходил отдельным постом."""
    title = ('⚔️Постер аниме «Изгнанный реинкарнированный тяжёлый рыцарь не имеет себе равных '
             'в знаниях игры»')
    first = {'title': title, 'source': 'TG: YtkaNews', 'link': 'https://a.test/1'}
    second = dict(first, source='TG: Advance', link='https://a.test/2', _work_key='shiki:59741')
    third = {'title': 'The Exiled Heavy Knight Knows How to Game the System Part 2 Reveals Main '
                      'Trailer, Visual', 'source': 'Anime Corner', 'link': 'https://a.test/3',
             '_work_key': 'shiki:59741'}
    clusters = bot._cluster_news([first, second, third], persist_intelligence=False)
    assert len(clusters) == 1


def test_shikimori_budget_goes_to_news_first(monkeypatch, tmp_path):
    """Первый цикл тратил все 40 запросов по порядку сбора — на подборки и LEGO."""
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
    monkeypatch.setattr(bot, 'settings', MagicMock(local_noise_filter=True, local_topic_filter=True))
    resolver = bot.WorkTitleResolver(tmp_path / 'wt.json')
    asked = []
    resolver.lookup = lambda name: asked.append(name) or ''
    monkeypatch.setattr(bot, 'work_titles', resolver)
    monkeypatch.setattr(bot, 'anilist', None)
    items = [
        {'title': '10 New Fall 2026 Anime to Watch This October', 'source': 'ComicBook Anime', 'summary': ''},
        {'title': 'Transformers Gives Optimus Prime a Blacked-Out Upgrade', 'source': 'Collider', 'summary': ''},
        {'title': 'Phantom Busters Anime Adaptation Announced for 2027', 'source': 'Anime Corner',
         'summary': ''},
    ]
    bot._annotate_work_keys(items, budget=1)
    assert asked == ['Phantom Busters']


@pytest.mark.parametrize(('title', 'name'), [
    ("'Phantom Busters' TV Anime Announced For 2027", 'Phantom Busters'),
    ('Rudy Takes Action in Mushoku Tensei Season 3 Episode 14 Preview', 'Mushoku Tensei'),
])
def test_work_name_from_single_quotes_and_title_tail(title, name):
    from news_stories import story_work_names
    assert name in story_work_names(title)
    # Апостроф внутри слова — не кавычка: «JoJo's …» не даёт имени «s …».
    assert not any(n.startswith('s ') for n in story_work_names("JoJo's Bizarre Adventure Opening"))


def test_slogan_title_takes_the_name_from_the_first_sentence():
    from news_stories import story_work_names
    news = {'title': '🍀 КЛЕВЕР ПОЙДЕТ ДО КОНЦА.',
            'summary': 'Инсайдеры утверждают, что 2 сезон «Черного Клевера» станет финальным. '
                       'А «Магическая битва» тут ни при чём.'}
    assert story_work_names(news) == ['Черного Клевера']


# ---------- п.5. Пересказ другим источником — не «Обновление:» ----------

@pytest.fixture
def story_history(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    return bot.PublishedStoryStore(tmp_path / 'stories.json')


def _published(store, title, summary, link):
    news = {'title': title, 'summary': summary, 'link': link, 'source': 'X'}
    news['_story_id'] = bot._story_id(news)
    store.record(news)


@pytest.mark.parametrize(('old', 'new'), [
    (("'Phantom Busters' TV Anime Announced For 2027",
      "A television anime adaptation of Shoco's Phantom Busters manga was announced on Thursday."),
     ('Phantom Busters Anime Adaptation Announced for 2027',
      "Neoshoco's Phantom Busters manga is officially getting a TV anime adaptation in 2027, "
      'as announced on September 24.')),
    (("'Shin Oishinbo'  Reveals Main Cast, Staff",
      "The official website for the anime adaptation of Tetsu Kariya's Oishinbo manga revealed "
      'the main cast, staff and a teaser visual on Thursday. The anime is scheduled to premiere in 2027.'),
     ('New Oishinbo TV Anime Reveals Main Cast, 2027 Release Date', 'The original series aired in 1988')),
    (("'Aoki Denshou Welsh & Shedar' Reveals Additional Cast, Theme Songs, Main Promo",
      'The official website revealed additional cast, theme songs and the main promotional video '
      'on Thursday. The anime is scheduled to premiere on October 2 at 9:25 p.m. on Tokyo MX.'),
     ('Aoki Densho Welsh & Shedar Anime Reveals Trailer, Additional Cast, Theme Song Info',
      'French / Japanese co-production begins broadcasting on October 2')),
])
def test_retelling_by_another_source_is_not_an_update(story_history, old, new):
    """Три новых слова давали «новизну» 0.5 — и пересказ обходил дедуп."""
    _published(story_history, *old, 'https://old.test/1')
    fresh = {'title': new[0], 'summary': new[1], 'link': 'https://new.test/2', 'source': 'Y'}
    fresh['_story_id'] = bot._story_id(fresh)
    assert story_history.classify_update(fresh) is None


def test_new_release_date_is_still_an_update(story_history):
    _published(story_history, 'Phantom Busters Anime Adaptation Announced for 2027',
               'The manga is getting a TV anime adaptation.', 'https://old.test/1')
    fresh = {'title': 'Phantom Busters Anime Reveals Teaser, Premiere Date',
             'summary': 'The anime will premiere on April 4, 2027.', 'link': 'https://new.test/2',
             'source': 'Y'}
    fresh['_story_id'] = bot._story_id(fresh)
    assert story_history.classify_update(fresh) is not None


# ---------- п.6. Ложные повторы теряли настоящие новости ----------

@pytest.mark.parametrize(('first', 'second'), [
    # Общая строка «announcedfor2027» — у двух разных тайтлов.
    ("'Phantom Busters' TV Anime Announced For 2027", 'Ace Attorney: Dual Destinies VR Announced for 2027'),
    # «Announces Additional Cast» — рубрика, а тайтлы разные.
    ("'Isshiki-san wa Koi wo Shiritai.' Announces Additional Cast",
     "'Hyouken no Majutsushi ga Sekai wo Suberu II' Announces Additional Cast Pair"),
    # Разные месяцы — разные подборки лицензий.
    ('Exciting New Licenses Coming in December 2026', 'Exciting New Licenses Coming in September 2026'),
    # «к 1-й серии 2-го сезона» — рубрика и номера, тайтлы разные.
    ('Кадры к 1-й серии 2-го сезона аниме "Чёрный клевер".',
     'Отрывок к 1-й серии 2-го сезона "Киберпанк: Бегущие по краю".'),
])
async def test_ledger_does_not_match_on_rubric_words(tmp_path, first, second):
    store = bot.SentLinksStore(tmp_path / 'sent.json')
    assert await store.claim('https://a.test/1', first)
    assert not store.has_similar_title(second)


async def test_ledger_still_matches_the_same_title(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'sent.json')
    assert await store.claim('https://a.test/1',
                             'From Old Country Bumpkin to Master Swordsman Season 3 Anime Announced')
    assert store.has_similar_title('From Old Country Bumpkin to Master Swordsman Season 3 Announced')


@pytest.mark.parametrize('second', [
    'Кадры к 14 серии 3 сезона аниме Реинкарнация безработного.',
    'Кадры к 1 серии аниме Я могу давать магическую силу в кредит!',
])
def test_final_text_does_not_match_on_rubric_words(tmp_path, second):
    """«Кадры 12 серии «Табакошка»» глушил «Кадры к 14 серии «Реинкарнации»»."""
    texts = bot.PublishedTexts(tmp_path / 'texts.json')
    texts.add('Кадры 12 серии аниме «Табакошка».')
    assert texts.find_similar(second) is None


def test_final_text_still_matches_the_same_news(tmp_path):
    texts = bot.PublishedTexts(tmp_path / 'texts.json')
    texts.add('Кадры 12 серии аниме «Табакошка».')
    assert texts.find_similar('🚬 Кадры к 12 серии аниме Табакошка.')


# ---------- п.12. Дата выхода, а не дата анонса ----------

@pytest.mark.parametrize(('text', 'date'), [
    ("Neoshoco's Phantom Busters manga is officially getting a TV anime adaptation in 2027, "
     'as announced on September 24.', ''),
    ('The anime was announced on September 20. It premieres October 4 on Netflix.', '4 октября'),
    ('The event on September 24 revealed a trailer; the anime airs October 4.', '4 октября'),
])
def test_release_date_prefers_release_context(text, date):
    assert bot.extract_release_date_from_text(text) == date
