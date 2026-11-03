"""Неточные названия тайтлов (разбор постов канала, октябрь 2026).

Пересказ: в канал выходили ромадзи вместо официальных русских названий
(«Sousou no Frieren», «Kaijuu 8-gou», «BLUE BOX»), один тайтл под тремя
именами («WOUND & BANDAGE», «Kizuguchi to hōtai», «Раны и бинты»), а
апостроф ломал подстановку: «…» (Frieren: Beyond Journey)s End».
"""
import pytest

import anime_news_bot as bot

FRIEREN = {'key': 'shiki:52991', 'name': 'Sousou no Frieren',
           'english': "Frieren: Beyond Journey's End",
           'russian': 'Провожающая в последний путь Фрирен', 'rule': bot.WORK_MATCH_RULE, 'at': bot.datetime.now(bot.timezone.utc).isoformat()}
WOUND = {'key': 'shiki:64600', 'name': 'Kizuguchi to Houtai', 'english': 'Wound & Bandage',
         'russian': 'Рана и бинт', 'rule': bot.WORK_MATCH_RULE, 'at': bot.datetime.now(bot.timezone.utc).isoformat()}


@pytest.fixture
def titles(monkeypatch, tmp_path):
    resolver = bot.WorkTitleResolver(tmp_path / 'wt.json')

    def seed(row, *names):
        for name in names:
            resolver._items[resolver._norm(name)] = dict(row)
        resolver._index(row)
    seed(FRIEREN, 'Sousou no Frieren', "Frieren: Beyond Journey's End")
    seed(WOUND, 'Wound & Bandage', 'Kizuguchi to Houtai')
    monkeypatch.setattr(bot, 'work_titles', resolver)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _k: True)
    monkeypatch.setattr(bot, '_russian_titles_on', lambda: True)
    monkeypatch.setattr(resolver, 'lookup', lambda _n: pytest.fail('сеть не нужна'))
    return resolver


def test_apostrophe_is_not_a_closing_quote(titles):
    news = {'_work_titles_ru': {'Frieren: Beyond Journey': 'Провожающая в последний путь Фрирен'}}
    text = "3 сезон «Frieren: Beyond Journey's End» выйдет в октябре."
    assert ")s End" not in bot._russify_titles(text, news)


def test_other_spelling_of_a_known_title_is_replaced(titles):
    """Источник писал «Frieren», модель — ромадзи «Sousou no Frieren»."""
    news = {'_work_key': 'shiki:52991', '_work_name': "Frieren: Beyond Journey's End",
            '_work_russian': 'Провожающая в последний путь Фрирен'}
    out = bot._russify_titles('3 сезон «Sousou no Frieren» выйдет в октябре.', news)
    assert '«Провожающая в последний путь Фрирен»' in out and 'Sousou no Frieren)' in out


def test_titles_named_by_the_model_are_russified(titles):
    news = {'_llm_subject': 'Wound & Bandage', '_llm_topic': 'аниме',
            '_llm_text': 'Вышел новый тизер аниме Kizuguchi to Houtai.'}
    bot._annotate_llm_titles(news)
    assert news['_work_key'] == 'shiki:64600'
    assert '«Рана и бинт»' in bot._russify_titles(news['_llm_text'], news)


def test_macrons_match_the_database_spelling():
    assert bot.WorkTitleResolver._norm('Kizuguchi to Hōtai') == bot.WorkTitleResolver._norm('Kizuguchi to Hotai')
    assert bot.WorkTitleResolver.names_match('Kizuguchi to hōtai', ['Kizuguchi to Houtai'])


def test_network_lookup_only_for_anime_and_manga(titles, monkeypatch):
    asked = []
    monkeypatch.setattr(titles, 'lookup', lambda name: asked.append(name) or '')
    bot._annotate_llm_titles({'_llm_subject': 'Avengers: Doomsday', '_llm_topic': 'кино',
                              '_llm_text': 'Вышел тизер.'})
    assert asked == []
    bot._annotate_llm_titles({'_llm_subject': 'Some New Anime', '_llm_topic': 'аниме',
                              '_llm_text': 'Вышел тизер.'})
    assert asked == ['Some New Anime']


def test_lookup_budget_per_news(titles, monkeypatch):
    asked = []
    monkeypatch.setattr(titles, 'lookup', lambda name: asked.append(name) or '')
    bot._annotate_llm_titles({'_llm_subject': 'Alpha Title', '_llm_topic': 'аниме',
                              '_llm_text': 'Аниме «Beta Title», «Gamma Title» и «Delta Title», «Omega Title».'})
    assert len(asked) == bot.LLM_TITLE_LOOKUPS


def test_name_fallback_key_yields_to_the_database(titles):
    news = {'_work_key': 'name:раныибинты', '_llm_subject': 'Wound & Bandage', '_llm_topic': 'аниме',
            '_llm_text': 'Вышел трейлер аниме «Раны и бинты».'}
    bot._annotate_llm_titles(news)
    assert news['_work_key'] == 'shiki:64600'


def _wound_news(text):
    return {'_work_key': 'shiki:64600', '_work_name': 'Wound & Bandage',
            '_work_russian': 'Рана и бинт', '_llm_subject': 'Wound & Bandage', '_llm_text': text}


def test_channel_translation_becomes_the_official_name(titles):
    out = bot._russify_titles('Вышел трейлер аниме «Раны и бинты».', _wound_news(''))
    assert out == 'Вышел трейлер аниме «Рана и бинт».'


def test_declined_official_name_is_kept(titles):
    """Склонённое официальное название — не чужой перевод."""
    news = {'_work_key': 'shiki:52991', '_work_name': 'Sousou no Frieren',
            '_work_russian': 'Провожающая в последний путь Фрирен', '_llm_subject': 'Sousou no Frieren'}
    text = 'Кадры аниме «Провожающей в последний путь Фрирен».'
    assert bot._official_russian_name(text, news) == text


def test_other_work_named_in_the_post_is_not_replaced(titles):
    news = dict(_wound_news(''), _llm_subject='Some Director Project')
    text = 'Режиссёр снимет аниме «Новый мир».'
    assert bot._russify_titles(text, news) == text


def test_two_named_works_are_left_alone(titles):
    text = 'Аниме «Раны и бинты» и аниме «Другой тайтл» покажут вместе.'
    assert bot._official_russian_name(text, _wound_news('')) == text


@pytest.mark.parametrize('head, expected', [
    ('​ постер ко 2 сезону', 'Постер ко 2 сезону'),
    ('официальный арт', 'Официальный арт'),
    ('iPhone получил аниме-тему', 'iPhone получил аниме-тему'),
    ('Постер вышел', 'Постер вышел'),
])
def test_head_is_cleaned_and_capitalised(head, expected):
    assert bot._clean_head(head) == expected
