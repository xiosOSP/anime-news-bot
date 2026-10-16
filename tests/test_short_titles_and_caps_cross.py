"""Мелочи из разбора постов (октябрь 2026).

1. Короткие названия («Ганц», «Дюна») не получали ключа тайтла, и защита
   «один тайтл — один пост» их не видела: второй пост о том же тайтле прошёл бы.
2. Капс в названии коллаборации не приводился, если его разрывал знак «×».
Примеры пересказаны, это не корпус канала.
"""
import pytest

import anime_news_bot as bot


@pytest.fixture
def memory(monkeypatch, tmp_path):
    store = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', store)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    monkeypatch.setattr(bot, 'anilist', None)
    return store


def test_short_anchored_title_repeats(memory):
    memory.remember(bot._work_identity_keys({'title': 'Новый трейлер аниме «Ганц»'}), 'Ганц', 'bot')
    # Другой падеж, тоже после слова-типа: ключ по основе совпадает.
    assert bot._work_repeat_blocks({'title': 'Кадры ремейка аниме «Ганца»'})


def test_short_latin_anchored_title_gets_key(memory):
    assert bot._work_identity_keys({'title': 'Постер аниме «Nana»'}) == ['name:nana']


def test_short_title_without_anchor_stays_keyless(memory):
    """Короткое слово в кавычках без «аниме»/«фильма» — часто не тайтл."""
    assert bot._work_identity_keys({'title': 'Сбер купил «Окко»'}) == []
    assert bot._work_identity_keys({'title': 'Тизер «Блич»'}) == []


def test_anchor_does_not_unlock_three_letters(memory):
    assert bot._work_identity_keys({'title': 'Трейлер аниме «Мох»'}) == []


def test_platform_after_anchor_is_still_not_a_title(memory):
    """«Иви» и «Okko» в списке площадок: слово «сериал» их тайтлом не делает."""
    assert bot._work_identity_keys({'title': 'Права на сериал «Okko» не продлены'}) == []


@pytest.mark.parametrize('text, expected', [
    ('Трейлер AO BUTA × EXPELLED FROM PARADISE', 'Трейлер Ao Buta × Expelled from Paradise'),
    ('Трейлер AO BUTA x EXPELLED FROM PARADISE', 'Трейлер Ao Buta x Expelled from Paradise'),
    ('Трейлер AO BUTA×EXPELLED FROM PARADISE', 'Трейлер Ao Buta×Expelled from Paradise'),
])
def test_caps_across_cross_sign(text, expected):
    assert bot._tame_caps_latin(text) == expected


def test_short_caps_with_cross_is_left_alone():
    """Два коротких слова через «x» — не строка капса: аббревиатуры не трогаем."""
    assert bot._tame_caps_latin('Тираж ONE x NHK') == 'Тираж ONE x NHK'
