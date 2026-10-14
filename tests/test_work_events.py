"""Один тайтл — один пост, но по событию, а не по тайтлу вообще.

Аудит октября 2026 (пересказ): защита от повторов закрывала тайтл на полтора
дня после любого поста о нём. Пост админа со сравнением аниме и манги закрыл
бы и анонс нового сезона, «Кадры 4 серии» — «Кадры 5 серии». А настоящие
повторы проходили: у nexvlsz тайтл в прямых кавычках, и ключа не было вовсе;
«Ненасытного Берсерка» и «Ненасытный Берсерк» давали разные ключи; заголовок
админа без кавычек («…аниме Голубая шкатулка») не давал ключа совсем.
"""
import time

import pytest

import anime_news_bot as bot


@pytest.fixture
def memory(monkeypatch, tmp_path):
    store = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', store)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    monkeypatch.setattr(bot, 'anilist', None)
    return store


def _post(title, text=None, **kw):
    news = {'title': title, 'summary': ''}
    if text:
        news['_llm_text'] = text
    news.update(kw)
    return news


def _published(memory, news, by='bot', hours_ago=0.0):
    bot._work_remember(news, by)
    memory._items[-1]['at'] = time.time() - hours_ago * 3600


# ---------------------------------------------------------------- ключи тайтла

def test_straight_quotes_of_the_source_give_a_key():
    keys = bot._work_identity_keys(_post('Постер к аниме "Едок: Сверхзаказ".'))
    assert 'ru:едоксверхзаказ' in keys


def test_final_title_of_the_rewrite_gives_keys():
    news = _post('Poster della seconda stagione', 'Постер второго сезона аниме «Киберпанк: Бегущие по краю».')
    assert 'ru:киберпанкбегущиепокраю' in bot._work_identity_keys(news)


def test_declensions_of_one_name_meet():
    a = bot._work_identity_keys(_post('Показан первый тизер 2 сезона «Ненасытного Берсерка»'))
    b = bot._work_identity_keys(_post('Трейлер к 2-му сезону аниме «Ненасытный Берсерк»'))
    assert set(a) & set(b)


def test_admin_title_without_quotes_gives_a_key():
    admin = bot._work_identity_keys(_post('Кадры к 2 серии 2 сезона аниме Голубая шкатулка.'))
    ours = bot._work_identity_keys(_post('Постер 2 сезона аниме «Голубой шкатулки»'))
    assert set(admin) & set(ours)


def test_name_without_quotes_ends_at_the_comma():
    keys = bot._work_identity_keys(_post('Анонсировано аниме по манге Оммёдзи Нуэ, премьера летом 2027'))
    assert 'ru:оммёдзинуэ' in keys


def test_different_titles_with_similar_words_do_not_meet():
    a = bot._work_identity_keys(_post('Кадры аниме «Магическая битва»'))
    b = bot._work_identity_keys(_post('Кадры аниме «Магическая битва: Смертельная миграция»'))
    assert not set(a) & set(b)


def test_russian_keys_do_not_push_out_the_latin_name():
    news = _post('«Альфа один», «Бета два», «Гамма три», «Дельта четыре», «Эпсилон пять»',
                 '«Дзета шесть», «Эта семь», «Тета восемь», «Йота девять» и «Каппа десять»',
                 _llm_subject='Nue no Onmyouji')
    keys = bot._work_identity_keys(news)
    assert len(keys) == bot.WORK_KEYS_MAX
    assert 'name:nuenoonmyouji' in keys


# ---------------------------------------------------------------- события

def test_two_posters_of_the_same_season_are_a_repeat(memory):
    """Живой случай: два поста подряд о постере второго сезона «Киберпанка»."""
    _published(memory, _post('Poster della seconda stagione di Cyberpunk: Edgerunners',
                             'Постер второго сезона аниме «Киберпанк: Бегущие по краю».'), hours_ago=3)
    second = _post('Новый постер к аниме "Киберпанк: Бегущие по краю"',
                   'Новый постер 2 сезона аниме «Киберпанк: Бегущие по краю».')
    assert bot._work_repeat_blocks(second)


def test_admin_art_does_not_close_the_title(memory):
    _published(memory, _post('🔥 Сравнение аниме и манги «Голубая шкатулка» уже в TikTok!'), by='admin')
    assert not bot._work_repeat_blocks(_post('Анонсирован третий сезон аниме «Голубая шкатулка»'))


def test_announcement_and_same_day_poster_are_one_news(memory):
    _published(memory, _post('Аниме «Оммёдзи Нуэ» официально анонсировано!'), by='admin', hours_ago=5)
    assert bot._work_repeat_blocks(_post('Постер и тизер аниме «Оммёдзи Нуэ» от студии Hibari'))


def test_trailer_two_days_after_the_announcement_is_news(memory):
    monkey_hours = bot.WORK_PACKAGE_HOURS + 6
    _published(memory, _post('Аниме «Оммёдзи Нуэ» официально анонсировано!'), hours_ago=monkey_hours)
    assert not bot._work_repeat_blocks(_post('Вышел трейлер аниме «Оммёдзи Нуэ»'))
    assert bot._work_repeat_blocks(_post('Анонсировано аниме по манге «Оммёдзи Нуэ»'))


def test_next_episode_frames_are_news_and_the_same_episode_is_not(memory):
    _published(memory, _post('Кадры 4 серии аниме «Ледяная стена»'), hours_ago=20)
    assert not bot._work_repeat_blocks(_post('Кадры 5 серии аниме «Ледяная стена»'))
    assert bot._work_repeat_blocks(_post('Новые кадры 4 серии аниме «Ледяная стена»'))


def test_next_season_announcement_is_not_closed_by_a_season_poster(memory):
    _published(memory, _post('Новая иллюстрация ко 2-му сезону аниме «Голубая шкатулка»'), by='admin')
    assert not bot._work_repeat_blocks(_post('Анонсирован 3 сезон аниме «Голубая шкатулка»'))


def test_own_post_without_event_closes_only_a_post_without_event(memory):
    _published(memory, _post('«Голубая шкатулка» возглавила рейтинг продаж манги'))
    assert bot._work_repeat_blocks(_post('Манга «Голубая шкатулка» стала самой продаваемой'))
    assert not bot._work_repeat_blocks(_post('Вышел трейлер аниме «Голубая шкатулка»'))


def test_event_and_numbers_of_the_rewrite_are_used(memory):
    _published(memory, _post('Blue Box: novità', 'Вышел трейлер аниме «Голубая шкатулка».'))
    assert memory._items[-1]['events'] == ['trailer']
    _published(memory, _post('Koori no Jouheki: nuovi frame', 'Кадры 4 серии аниме «Ледяная стена».'))
    assert not bot._work_repeat_blocks(_post('Кадры 5 серии аниме «Ледяная стена»'))


def test_post_without_event_is_not_closed_by_an_event_post(memory):
    _published(memory, _post('Вышел трейлер аниме «Голубая шкатулка»'))
    assert not bot._work_repeat_blocks(_post('Манга «Голубая шкатулка» стала самой продаваемой'))


def test_records_written_before_events_still_close_the_title(memory):
    memory.remember(['ru:голубаяшкатулка'], 'старый пост', 'bot')
    assert bot._work_repeat_blocks(_post('Анонсирован третий сезон аниме «Голубая шкатулка»'))


def test_window_still_applies(memory):
    _published(memory, _post('Анонсировано аниме «Оммёдзи Нуэ»'), hours_ago=40)
    assert not bot._work_repeat_blocks(_post('Анонсировано аниме по манге «Оммёдзи Нуэ»'))
