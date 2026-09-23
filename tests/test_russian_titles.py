"""Официальные русские названия тайтлов в постах.

Бот узнаёт тайтл новости по Shikimori (см. test_work_identity.py), а там у
тайтла есть русское название. Русские каналы пишут «Необъятный океан»,
английские сайты — «Grand Blue», и пост в канале был то таким, то эдаким.
"""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


GRAND_BLUE = {'_work_key': 'shiki:37105', '_work_name': 'Grand Blue', '_work_russian': 'Необъятный океан'}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=True, llm_tags=True))


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=False, llm_tags=True))


def test_english_name_becomes_the_official_russian_one(on):
    title = bot._with_russian_work_name('Объявлен четвёртый сезон Grand Blue', GRAND_BLUE)
    assert title == 'Объявлен четвёртый сезон «Необъятный океан» (Grand Blue)'


def test_long_original_is_not_repeated_in_brackets(on):
    news = {'_work_name': 'From Old Country Bumpkin to Master Swordsman',
            '_work_russian': 'Старик из деревни становится Святым мечом'}
    title = bot._with_russian_work_name('Анонсирован 3 сезон From Old Country Bumpkin to Master Swordsman', news)
    assert title == 'Анонсирован 3 сезон «Старик из деревни становится Святым мечом»'


@pytest.mark.parametrize('title', [
    'Анонсирован 4 сезон аниме «Необъятный океан»',      # уже по-русски
    'Объявлен четвёртый сезон Гранд Блю',                 # имени в тексте нет — не гадаем
])
def test_title_without_the_english_name_stays(on, title):
    assert bot._with_russian_work_name(title, GRAND_BLUE) == title


def test_russian_source_names_are_left_to_the_channel(on):
    news = {'_work_name': 'Супер-полицейский-экстрасенс Тёдзо', '_work_russian': 'Суперэкстрасенс Тёдзё'}
    title = 'Трейлер к аниме "Супер-полицейский-экстрасенс Тёдзо!"'
    assert bot._with_russian_work_name(title, news) == title


def test_switched_off_keeps_the_original(off):
    assert bot._with_russian_work_name('Сезон Grand Blue', GRAND_BLUE) == 'Сезон Grand Blue'


@pytest.mark.parametrize(('russian', 'tag'), [
    ('Необъятный океан', '#НеобъятныйОкеан'),
    ('Киберпанк: Бегущие по краю', '#КиберпанкБегущиеПоКраю'),
    ('Старик из деревни становится Святым мечом', ''),      # длинно — тега нет
    ('86: Восемьдесят шесть', ''),                           # с цифры тег не начинается
    ('', ''),
])
def test_work_hashtag(russian, tag):
    assert bot._work_tag({'_work_russian': russian}) == tag


def test_work_tag_goes_first_and_model_tags_are_not_doubled(on):
    news = dict(GRAND_BLUE, _llm_tags='#аниме #НеобъятныйОкеан #сезон')
    assert bot._with_tags('Пост.', news) == 'Пост.\n\n#НеобъятныйОкеан #аниме #сезон'


def test_tags_switch_turns_off_the_work_tag_too(monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=True, llm_tags=False))
    assert bot._with_tags('Пост.', GRAND_BLUE) == 'Пост.'


def test_russian_titles_switch_turns_off_only_the_work_tag(off):
    news = dict(GRAND_BLUE, _llm_tags='#аниме')
    assert bot._with_tags('Пост.', news) == 'Пост.\n\n#аниме'


def test_model_gets_the_official_name(on):
    hint = bot._work_title_hint(GRAND_BLUE)
    assert hint == 'Русское название тайтла (Shikimori): «Необъятный океан»; в источнике — Grand Blue'
    payload = bot._llm_batch_payload([dict(GRAND_BLUE, title='Grand Blue Season 4 Announced')], ['text'])
    assert hint in payload
    assert payload.index(hint) < payload.index('<article_title>')     # подсказка — не данные статьи


def test_model_gets_no_hint_when_switched_off_or_unknown(off):
    assert bot._work_title_hint(GRAND_BLUE) == ''
    assert bot._work_title_hint({'_work_key': 'shiki:1'}) == ''


def test_prompt_explains_the_official_name():
    assert 'Русское название тайтла' in bot.LLM_SYSTEM_PROMPT
    assert '«Необъятный океан» (Grand Blue)' in bot.LLM_BATCH_SYSTEM_PROMPT


# ---------- кеш названий помнит русское имя ----------

@pytest.fixture
def resolver(monkeypatch, tmp_path):
    answer = [{'id': '37105', 'name': 'Grand Blue', 'russian': 'Необъятный океан',
               'english': 'Grand Blue Dreaming', 'japanese': 'ぐらんぶる', 'synonyms': []}]
    calls = []

    def post(url, json=None, **kwargs):
        calls.append(json['variables']['s'])
        return MagicMock(status_code=200, json=lambda: {'data': {'animes': answer}})
    monkeypatch.setattr(bot.requests, 'post', post)
    store = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
    store.MIN_INTERVAL = 0
    monkeypatch.setattr(bot, 'work_titles', store)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
    monkeypatch.setattr(bot, 'sent_links', None)
    return NS(store=store, calls=calls)


def test_news_learns_the_russian_name(resolver):
    items = [{'title': 'Grand Blue Season 4 Announced With Teaser Visual', 'link': 'https://ac/1'}]
    bot._annotate_work_keys(items)
    assert items[0]['_work_russian'] == 'Необъятный океан'


def test_old_cache_rows_without_russian_are_asked_again(resolver):
    """Записи, сделанные до русских названий, переспрашиваются один раз."""
    resolver.store._items['grandblue'] = {'key': 'shiki:37105', 'name': 'Grand Blue',
                                          'at': '2099-01-01T00:00:00+00:00'}
    assert resolver.store.cached('Grand Blue') is None
    items = [{'title': 'Grand Blue Season 4 Announced', 'link': 'https://ac/2'}]
    bot._annotate_work_keys(items)
    assert resolver.calls == ['Grand Blue'] and items[0]['_work_russian'] == 'Необъятный океан'


def test_no_model_post_uses_the_russian_name(resolver, monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=True, llm_tags=True))
    monkeypatch.setattr(bot, 'translate_text', lambda text, **k: 'Объявлен четвёртый сезон Grand Blue')
    news = dict(GRAND_BLUE, title='Grand Blue Season 4 Announced', summary='', link='https://ac/3')
    post = bot.format_news_short(news)
    assert post.startswith('Объявлен четвёртый сезон «Необъятный океан» (Grand Blue).')
    assert post.endswith('#НеобъятныйОкеан')


# ---------- одиночный запрос к модели и проверка её ответа ----------

@pytest.mark.asyncio
async def test_single_request_and_its_judge_both_see_the_official_name(monkeypatch):
    """Судья сверяет пост с источником: без подсказки в «источнике» он счёл бы
    «Необъятный океан» именем, которого в статье нет, и отклонил бы пост."""
    import json
    settings = NS(llm_read_article=False, llm_filter=True, llm_skip_filler=True,
                  llm_dedup_subject=False, llm_limit_repeats=False,
                  llm_rewrite=True, llm_tags=True, russian_titles=True)
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, 'entity_memory', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', True)
    seen = []

    async def call(messages, max_tokens=bot.LLM_MAX_TOKENS, **kwargs):
        seen.append(messages[-1]['content'])
        if len(seen) == 1:
            return json.dumps({'topic': 'аниме', 'kind': 'анонс', 'subject': 'Grand Blue',
                               'title': 'Анонсирован четвёртый сезон «Необъятного океана» (Grand Blue)',
                               'summary': '', 'tags': []})
        return json.dumps({'approved': True, 'reason': ''})
    monkeypatch.setattr(bot, '_llm_call', call)
    news = dict(GRAND_BLUE, title='Grand Blue Season 4 Announced With Teaser Visual',
                summary='The fourth season of Grand Blue was announced with a teaser visual.',
                link='https://animecorner.me/grand-blue-season-4', source='Anime Corner')
    assert await bot._llm_enrich(news) == 'ok'
    hint = 'Русское название тайтла (Shikimori): «Необъятный океан»'
    assert hint in seen[0] and seen[0].index(hint) < seen[0].index('<article_title>')
    assert len(seen) == 2 and hint in seen[1]            # судья видит его как факт источника
    assert '«Необъятного океана»' in news['_llm_text']
