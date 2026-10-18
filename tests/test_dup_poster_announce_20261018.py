"""Дубль «постер» → «анонс» одного тайтла (октябрь 2026).

Пересказ: бот выложил «Опубликован постер аниме «Тёпленько»», а через час —
«Анонсировано аниме по манге NullMeta (Тёпленько)» с тем же постером. Русское
имя во втором стояло в скобках и ключа не давало, а проверка картинок сочла
постер и анонс разными событиями.
"""
from datetime import datetime, timedelta, timezone

import pytest

import anime_news_bot as bot

POSTER = 'Опубликован постер аниме «Тёпленько»'
ANNOUNCE = 'Анонсировано аниме по манге NullMeta (Тёпленько)'


@pytest.fixture
def memory(monkeypatch, tmp_path):
    store = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', store)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    monkeypatch.setattr(bot, 'anilist', None)
    return store


def test_russian_name_in_brackets_after_latin_is_a_key(memory):
    assert set(bot._work_identity_keys({'title': POSTER})) & set(
        bot._work_identity_keys({'title': ANNOUNCE}))


def test_announce_after_poster_is_a_repeat(memory):
    bot._work_remember({'title': POSTER}, 'bot')
    assert bot._work_repeat_blocks({'title': ANNOUNCE})


@pytest.mark.parametrize('title', ['Netflix (обновлено) продлил сериал', 'Chainsaw Man (Трейлер)',
                                   'Сериал «Ведьмак» (Озвучка)', 'Новость дня (Обновлено)'])
def test_bracket_note_is_not_a_title(memory, title):
    note = title.rsplit('(', 1)[1].rstrip(')').split(')')[0].casefold()
    assert not any(note[:5] in k for k in bot._work_identity_keys({'title': title}))


FP = 'd:' + '0' * 16


def _hashes(tmp_path, title, hours_ago):
    store = bot.ImageHashes(tmp_path / 'h.json')
    store.add(FP, title)
    store._items[-1]['at'] = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return store


def test_same_poster_for_announce_within_a_day(tmp_path):
    assert _hashes(tmp_path, POSTER, 1).find_duplicate(FP, ANNOUNCE)


def test_same_poster_later_is_a_new_story(tmp_path):
    """Кадр студии кочует по новостям франшизы: через двое суток анонс — уже новость."""
    assert not _hashes(tmp_path, POSTER, 48).find_duplicate(FP, ANNOUNCE)


def test_same_poster_for_trailer_stays_a_new_story(tmp_path):
    """Трейлер с тем же кадром — новое содержание, не пересказ постера."""
    assert not _hashes(tmp_path, POSTER, 1).find_duplicate(FP, 'Вышел трейлер аниме «Тёпленько»')
