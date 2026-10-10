"""Строка «о чём» в посте, названия без капса.

У владельца канала под анонсом стоит цитата с завязкой сюжета, а пост бота
был заголовком и одной фразой: «Анонсировано аниме „Жри или сдохни“. Премьера
в 2027 году». Читатель не узнавал, про что тайтл. Теперь бот берёт справку
о тайтле с Shikimori и просит модель одну фразу «о чём».
"""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


# ---------------------------------------------------------------- справка Shikimori

def _graphql(description, genres=()):
    r = MagicMock(status_code=200)
    r.json.return_value = {'data': {'animes': [{
        'id': '1', 'description': description,
        'genres': [{'russian': g, 'kind': 'genre'} for g in genres]}]}}
    return r


@pytest.fixture
def titles(tmp_path, monkeypatch):
    resolver = bot.WorkTitleResolver(tmp_path / 'titles.json')
    resolver.MIN_INTERVAL = 0
    monkeypatch.setattr(bot, 'work_titles', resolver)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    return resolver


def test_description_is_cleaned_of_markup_and_spoilers():
    text = bot._shikimori_plain('[character=7]Сэнку[/character] просыпается в мире, где [[люди]] '
                                'окаменели. [spoiler]Он погибнет.[/spoiler] Наука спасёт всех. Третья.')
    assert text == 'Сэнку просыпается в мире, где люди окаменели. Наука спасёт всех.'


def test_about_is_fetched_once_and_cached(titles, monkeypatch):
    calls = []

    def post(*a, **k):
        calls.append(k['json'])
        return _graphql('Школьник находит тетрадь смерти.', ['Триллер', 'Мистика'])
    monkeypatch.setattr(bot.requests, 'post', post)
    first = titles.about('shiki:1535')
    second = titles.about('shiki:1535')
    assert first == {'text': 'Школьник находит тетрадь смерти.', 'genres': ['Триллер', 'Мистика']}
    assert second == first and len(calls) == 1
    assert calls[0]['variables'] == {'ids': '1535'}


def test_about_survives_restart(titles, monkeypatch, tmp_path):
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: _graphql('Описание тайтла здесь.'))
    titles.about('shiki:5')
    titles.flush()
    again = bot.WorkTitleResolver(tmp_path / 'titles.json')
    assert again.cached_about('shiki:5')['text'] == 'Описание тайтла здесь.'


def test_network_failure_is_not_cached(titles, monkeypatch):
    def broken(*a, **k):
        raise bot.requests.ConnectionError('нет сети')
    monkeypatch.setattr(bot.requests, 'post', broken)
    assert titles.about('shiki:9') == {}
    assert titles.cached_about('shiki:9') is None


def test_foreign_keys_are_not_looked_up(titles, monkeypatch):
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: pytest.fail('запроса быть не должно'))
    assert titles.about('name:somethingelse') == {}


def test_hint_for_the_model_carries_the_synopsis(titles):
    titles._about['shiki:3'] = {'text': 'Девушка-ниндзя служит в полиции.', 'genres': ['Экшен'],
                                'at': datetime.now(timezone.utc).isoformat()}
    hint = bot._work_title_hint({'_work_key': 'shiki:3'})
    assert 'Справка о тайтле' in hint and 'Девушка-ниндзя' in hint and 'Экшен' in hint


@pytest.mark.asyncio
async def test_attach_fetches_only_missing_and_within_budget(titles, monkeypatch):
    asked = []
    monkeypatch.setattr(titles, 'about', lambda key: asked.append(key) or {})
    titles._about['shiki:1'] = {'text': 'Есть.', 'genres': [], 'at': datetime.now(timezone.utc).isoformat()}
    items = [{'_work_key': 'shiki:1'}] + [{'_work_key': f'shiki:{n}'} for n in range(10, 20)]
    await bot._attach_work_about(items)
    assert 'shiki:1' not in asked and len(asked) == 4


# ---------------------------------------------------------------- фраза модели

SOURCE = ('Анонсировано аниме по манге. Премьера в 2027 году.\n'
          'Справка о тайтле (Shikimori, без спойлеров): Школьница становится помощницей бога смерти.')
BODY = 'Анонсировано аниме по манге.\n\nПремьера в 2027 году.'


def test_good_about_passes():
    about = bot._checked_about('Школьница помогает богу смерти искать потерянные души', SOURCE, BODY)
    assert about.endswith('.') and 'Школьница' in about


@pytest.mark.parametrize('about', [
    'Школьница 17 лет помогает богу смерти.',               # число, которого нет в источнике
    'Анонсировано аниме по манге, премьера в 2027 году.',   # повтор текста
    'Школьница помогает богу смерти и ищет души. ' * 6,     # простыня
    'Short English phrase only.',                           # не по-русски
])
def test_bad_about_is_dropped(about):
    assert bot._checked_about(about, SOURCE, BODY) == ''


def _reply(payload):
    return MagicMock(status_code=200, text='', headers={},
                     json=lambda: {'choices': [{'message': {
                         'content': json.dumps(payload, ensure_ascii=False)}}]})


@pytest.fixture
def llm(tmp_path, monkeypatch, titles):
    for name, value in (('LLM_API_KEY', 'k'), ('LLM_BASE_URL', 'https://x/v1'), ('LLM_MODEL', 'm'),
                        ('LLM_MIN_INTERVAL', 0), ('LLM_DAILY_LIMIT', 100),
                        ('_llm_disabled_runtime', False), ('_llm_fail_streak', 0)):
        monkeypatch.setattr(bot, name, value)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(titles, 'about', lambda key: {})
    return bot


def test_model_about_reaches_the_post(llm, monkeypatch):
    news = {'title': 'Eat or Die anime announced', 'summary': 'White Fox will animate. Premiere in 2027. '
            'The story follows a cook who must win food battles to survive.', 'source': 'X', 'lang': 'en'}
    answer = {'relevant': True, 'topic': 'аниме', 'kind': 'анонс',
              'title': 'Анонсировано аниме «Жри или сдохни»',
              'summary': 'Премьера в 2027 году, анимацией займётся White Fox.',
              'about': 'Повар должен побеждать в кулинарных битвах, чтобы выжить'}
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: _reply(answer))
    assert asyncio.run(bot._llm_enrich(news, use_cache=False)) == 'ok'
    assert news['_about'].startswith('Повар')
    text = bot.format_news_short(news)
    assert text.endswith('выжить.')
    rendered = bot._post_html(text, 1024, news)
    assert '<blockquote>Повар должен побеждать в кулинарных битвах, чтобы выжить.</blockquote>' in rendered


def test_without_model_shikimori_sentence_is_used(titles, monkeypatch):
    titles._about['shiki:3'] = {'text': 'Девушка-ниндзя служит в полиции. Вторая фраза.', 'genres': [],
                                'at': datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(bot, 'settings', MagicMock(russian_titles=False, llm_tags=False))
    news = {'title': 'Анонсировано аниме «Шиноби под прикрытием»', 'summary': '', 'lang': 'ru',
            '_work_key': 'shiki:3'}
    text = bot.format_news_short(news)
    assert text.rstrip().endswith('Девушка-ниндзя служит в полиции.')
    assert 'Вторая фраза' not in text


def test_admin_edit_is_left_alone(titles):
    titles._about['shiki:3'] = {'text': 'Описание.', 'genres': [], 'at': datetime.now(timezone.utc).isoformat()}
    news = {'_edited_text': 'Текст админа', '_work_key': 'shiki:3'}
    assert bot.format_news_short(news) == 'Текст админа'


# ---------------------------------------------------------------- капс

def test_caps_title_is_tamed():
    text = "Вышел трейлер «IN A WORLD FULL OF ZOMBIES I'M THE ONLY ONE WHO DOESN'T GET ATTACKED»"
    assert bot._tame_caps_latin(text) == ("Вышел трейлер «In a World Full of Zombies I'm the Only "
                                          "One Who Doesn't Get Attacked»")


@pytest.mark.parametrize('text', ['Студия MAPPA показала PV', 'DR.STONE: TERRAFORMING',
                                  'Аниме «Mera×Death» выйдет в 2027 году'])
def test_short_acronyms_and_stylised_names_stay(text):
    assert bot._tame_caps_latin(text) == text


def test_roman_numerals_survive():
    assert bot._tame_caps_latin('THE LEGEND OF HEROES PART II') == 'The Legend of Heroes Part II'
