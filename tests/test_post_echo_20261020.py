"""Пост повторял заголовок второй строкой (октябрь 2026).

Пересказ: заголовок «Появился тизер-постер 5 сезона «X»», а под ним «Тизер-
постер к 5 сезону «X» (латинское название) опубликован, дата выхода не
указана». Нового в строке нет, но латинское имя и «не указана» спасали её от
проверки на повтор. Ещё подписчики просили писать «постер», а не «тизер-постер».
"""
import pytest

import anime_news_bot as bot

TITLE = 'Появился тизер-постер 5 сезона «Добро пожаловать в класс превосходства»'
ECHO = ('Тизер-постер к 5 сезону «Добро пожаловать в класс превосходства» '
        '(Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e) опубликован, дата выхода не указана.')


def test_echo_with_romaji_and_empty_claim_is_dropped():
    assert bot._drop_repetitive_paragraphs(TITLE, [ECHO]) == []


def test_romaji_of_another_title_is_still_news():
    """Латинское имя другого тайтла — новое имя, строка остаётся."""
    para = 'Тизер-постер к 5 сезону «Другой тайтл» (Youkoso) опубликован.'
    assert bot._drop_repetitive_paragraphs(TITLE, [para])


@pytest.mark.parametrize('para, expected', [
    ('Сериал покажет Netflix, дата выхода пока не объявлена.', 'Сериал покажет Netflix.'),
    ('Дата выхода пока не объявлена. Режиссёр — Сэйдзи Киси.', 'Режиссёр — Сэйдзи Киси.'),
    ('Сериал покажет Netflix; точная дата премьеры станет известна позже.', 'Сериал покажет Netflix.'),
])
def test_empty_claims_are_removed_facts_stay(para, expected):
    assert bot._drop_repetitive_paragraphs('Анонсирован фильм «Икс»', [para]) == [expected]


def test_paragraph_without_empty_claim_is_untouched():
    para = 'Режиссёр сменился; студия осталась прежней.'
    assert bot._drop_empty_claims(para) == para


@pytest.mark.parametrize('text, expected', [
    ('Появился тизер-постер 5 сезона.', 'Появился постер 5 сезона.'),
    ('Тизер-постер вышел.', 'Постер вышел.'),
    ('Показали два тизер-постера и тизерный постер.', 'Показали два постера и постер.'),
    ('ТИЗЕР-ПОСТЕР', 'ПОСТЕР'),
    ('Вышли тизер и постер.', 'Вышли тизер и постер.'),
])
def test_teaser_poster_becomes_poster(text, expected):
    assert bot._polish_post_text(text) == expected


def test_romaji_counts_only_after_the_exact_title_name():
    """Скобку снимаем, только если название в «ёлочках» дословно есть в
    заголовке: иначе латиница может быть новым именем (студии, проекта)."""
    title = 'Появился постер 5 сезона «Класса превосходства»'
    para = 'Постер к 5 сезону «Класс превосходства» (Lerche Studio Project) опубликован.'
    assert bot._drop_repetitive_paragraphs(title, [para])
