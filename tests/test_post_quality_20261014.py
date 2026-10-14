"""Качество постов: аудит октября 2026 (пересказ найденного в канале).

Ностальгия «N лет назад вышел первый сезон» проходила, потому что слово
«сезон» считалось новым событием. «5 серия уже вышла» проходила мимо правила
о выходе серий из-за порядка слов. Анонс с карточкой фактов («🎬 Студия:
MAPPA») при альбоме постеров считался подборкой. Пересказ с теми же числами,
но с новым фактом выбрасывался как повтор. Модель ставила «Слух:» прямым
словам автора и переносила в пост меню ссылок источника.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot as bot
import llm_protocol
import post_text


# ---------------------------------------------------------------- юбилеи

@pytest.mark.parametrize('title', [
    'Ровно 10 лет назад вышел первый сезон аниме «Re:Zero»',
    '5 лет назад анонсировали аниме «Магическая битва»',
    'Исполнилось 20 лет со дня выхода трейлера «Наруто»',
    'Naruto celebrates 20 years since its anime premiere',
])
def test_looking_back_is_noise_even_with_event_words(title):
    assert bot.noise_reason({'title': title}) == 'годовщина и ностальгия'


@pytest.mark.parametrize('title', [
    '«Атака титанов» празднует 10-летие: к юбилею выйдет новый фильм',
    'В честь 20-летия «Наруто» покажут новый фильм',
    'Аниме «Боксёр» исполнилось 5 лет — студия анонсировала второй сезон',
    'Bleach 20th Anniversary project announced',
    'К 10-летию со дня премьеры «Паразита» выйдет новый фильм',
])
def test_new_event_for_the_anniversary_is_news(title):
    assert bot.noise_reason({'title': title}) == ''


# ---------------------------------------------------------------- выход серий

@pytest.mark.parametrize('title', [
    '5 серия аниме «Ледяная стена» уже вышла',
    'Можно посмотреть 5-ю серию «Ледяной стены»',
    '12 эпизод «Магической битвы» доступен на Кинопоиске',
    'Смотреть 3 серию «Ван Пис» уже на Wink',
])
def test_episode_release_in_any_word_order_is_noise(title):
    assert bot.noise_reason({'title': title}) == 'выход серии'


@pytest.mark.parametrize('title', [
    'Первая серия аниме выйдет 5 января',
    'Сериал продлили на второй сезон из 12 серий, которые будут доступны на Netflix',
    'Кадры 5 серии уже доступны',
    'Постер 5 серии опубликован',
    'Опенинг 5 серии «Ледяной стены» вышел',
])
def test_episode_material_and_future_dates_stay(title):
    assert bot.noise_reason({'title': title}) == ''


# ---------------------------------------------------------------- подборки

FACT_CARD = {
    'title': 'Анонсирован фильм «Магическая битва: Сибуя»',
    'summary': '🎬 Студия: MAPPA\n📅 Премьера: зима 2027\n🎥 Режиссёр: Сёта Госёдзоно\n'
               '🎵 Опенинг: King Gnu',
    'images': [f'https://x.example/{i}.jpg' for i in range(4)],
}


def test_announcement_with_a_fact_card_is_not_a_digest():
    assert not bot._looks_like_digest(dict(FACT_CARD))
    assert bot.noise_reason(dict(FACT_CARD)) == ''


def test_dashed_fact_card_is_not_a_digest_either():
    news = dict(FACT_CARD, summary='— Студия: MAPPA\n— Премьера: зима 2027\n— Режиссёр: Сёта Госёдзоно')
    assert not bot._looks_like_digest(news)


def test_emoji_lines_with_titles_still_make_a_digest():
    news = {'title': 'Сиквелы осени',
            'summary': '🔥 «Наруто» — новый проект\n🔥 «Доктор Стоун» — финал\n🔥 «Ванпанчмен» — 3 сезон',
            'images': [f'https://x.example/{i}.jpg' for i in range(6)]}
    assert bot._looks_like_digest(news)


def test_emoji_lines_without_titles_are_points_of_one_news():
    news = {'title': 'Анонсирован 2 сезон аниме «Ледяная стена»',
            'summary': '🔥 Новая студия\n🔥 Тот же режиссёр\n🔥 Премьера летом',
            'images': [f'https://x.example/{i}.jpg' for i in range(4)]}
    assert not bot._looks_like_digest(news)


def test_numbered_list_without_quotes_is_still_a_digest():
    news = {'title': 'Сиквелы, которые анонсировали в сентябре',
            'summary': '1. Наруто: новый проект\n2. Доктор Стоун: финал\n3. Ванпанчмен: третий сезон',
            'images': [f'https://x.example/{i}.jpg' for i in range(6)]}
    assert bot._looks_like_digest(news)


@pytest.mark.parametrize('title', ['🎨 Арты за неделю', 'Арты дня: Фрирен и Ферн'])
def test_weekly_art_album_is_a_digest(title):
    assert bot.noise_reason({'title': title}) == 'подборка'


# ---------------------------------------------------------------- повтор с теми же числами

BOX = '«Тайный дневник Верити» заработал 113 млн долларов при бюджете в 40 млн'


def test_same_numbers_with_a_new_fact_are_kept():
    body = 'Фильм собрал 113 млн при бюджете 40 млн и стал самым кассовым хоррором года.'
    assert bot._drop_repetitive_paragraphs(BOX, [body]) == [body]


def test_same_numbers_in_other_words_are_still_an_echo():
    assert bot._number_echo(BOX, 'Фильм собрал 113 млн долларов, превысив бюджет в 40 млн долларов.')
    assert bot._number_echo(BOX, 'Фильм принёс почти 113 млн при бюджете 40 млн.')


def test_one_number_and_one_new_word_is_still_an_echo():
    assert bot._number_echo('Сезон выйдет 5 января', 'Дебют сериала — 5 января.')


# ---------------------------------------------------------------- «Слух:» без слуха

SOURCE_DIRECT = ('Автор манги «Black Torch» Цуёси Такаки готов ко второму сезону аниме\n'
                 'Цуёси Такаки заявил, что открыт к созданию оригинального второго сезона.')


def test_rumor_label_is_dropped_when_the_author_said_it_himself():
    title = 'Слух: автор манги «Black Torch» готов ко второму сезону'
    assert llm_protocol._drop_unfounded_rumor_label(title, SOURCE_DIRECT) == \
        'Автор манги «Black Torch» готов ко второму сезону'


@pytest.mark.parametrize('source', [
    'По данным инсайдера, сезон выйдет в 2027 году',
    'Утечка: в сети появился список серий',
    'The studio is reportedly working on season 2',
    'Источники сообщают, что фильм отменён',
])
def test_rumor_label_stays_when_the_source_is_a_rumor(source):
    title = 'Слух: второй сезон выйдет в 2027 году'
    assert llm_protocol._drop_unfounded_rumor_label(title, source) == title


@pytest.fixture
def enrich_env(tmp_path, monkeypatch):
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


def test_rewrite_loses_unfounded_rumor_label_and_source_meta(enrich_env):
    title, summary = SOURCE_DIRECT.split('\n')
    news = {'title': title, 'summary': summary + ' Пользователь bgette разместил запись. KINOLINK | KINOBOT',
            'source': 'TG: Black'}
    answer = {'topic': 'аниме', 'kind': 'слух', 'subject': 'Black Torch',
              'title': 'Слух: автор манги «Black Torch» готов ко второму сезону',
              'summary': 'Цуёси Такаки заявил, что открыт к созданию оригинального второго сезона. '
                         'Пользователь bgette разместил запись. Платформа KINOLINK | KINOBOT.',
              'tags': []}
    with patch.object(bot.requests, 'post', return_value=_answer(answer)):
        asyncio.run(bot._llm_enrich(news))
    text = news['_llm_text']
    assert text.startswith('Автор манги «Black Torch» готов ко второму сезону')
    assert news['_llm_kind'] == 'новость'
    assert 'bgette' not in text and 'KINOLINK' not in text
    assert 'открыт к созданию оригинального второго сезона' in text


# ---------------------------------------------------------------- меню ссылок источника

@pytest.mark.parametrize('line', ['KINOLINK | KINOBOT', '🎬 YouTube | 📺 VK | Telegram'])
def test_link_menu_of_the_source_is_not_part_of_the_news(line):
    text = f'Третий сезон «Перси Джексона» начнёт выходить 20 ноября.\nВсе восемь эпизодов выйдут по два.\n{line}'
    title, summary = post_text._tg_title_and_summary(text, 'kinochannel', 'TG: kino')
    assert 'Перси' in title and 'восемь' in summary
    assert '|' not in summary


def test_fact_line_with_a_bar_stays():
    assert not post_text._tg_is_signature('Жанр: экшен | фэнтези', 'kinochannel', 'TG: kino')
