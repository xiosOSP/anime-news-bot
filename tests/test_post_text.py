"""Текст поста: заголовок из телеграм-источника и отсутствие подписи канала.

Все примеры взяты из реальных постов, ушедших в канал: декоративный эмодзи
становился заголовком («🔍» превращалось в пост «🔍.»), а подпись канала-
источника уезжала в тело поста.
"""
import pytest

import anime_news_bot as bot


# ---------- заголовок ----------

def test_decorative_emoji_is_not_a_title():
    """Реальный случай: пост начинался строкой «🔍», и она стала заголовком."""
    title, _ = bot._tg_title_and_summary(
        '🔍\nПремьера 2 сезона аниме «Детектив уже мертв» состоится 7 октября 2026 года.',
        'anime_ch', 'Аниме')
    assert title.startswith('Премьера')


@pytest.mark.parametrize('decoration', ['🔍', '➖➖➖', '...', '⚡️', '👇👇'])
def test_any_decoration_line_is_skipped(decoration):
    title, _ = bot._tg_title_and_summary(f'{decoration}\nНастоящий заголовок новости.',
                                         'ch', 'Канал')
    assert title == 'Настоящий заголовок новости.'


def test_post_without_meaningful_text_is_dropped():
    """Если после чистки ничего не осталось, публиковать нечего."""
    title, summary = bot._tg_title_and_summary('🔥🔥🔥\n➖➖➖', 'ch', 'Канал')
    assert title == '' and summary == ''


# ---------- подпись канала ----------

def test_channel_signature_is_stripped():
    """Реальный случай: «📰 Гиковский Вестник» уходило в текст поста."""
    title, summary = bot._tg_title_and_summary(
        "Уиллем Дефо для журнала A Rabbit's Foot.\n📰 Гиковский Вестник",
        'geekvestnik', 'Гиковский Вестник')
    assert 'Гиковский' not in title and 'Гиковский' not in summary


@pytest.mark.parametrize('signature', ['@kinonews', 't.me/kinonews', '📰 Кино'])
def test_signature_forms_are_stripped(signature):
    title, summary = bot._tg_title_and_summary(
        f'Новые фото со съёмок.\n\n«Бэтмен 2».\n{signature}', 'kinonews', 'Кино')
    assert signature.lstrip('@') not in f'{title} {summary}'
    assert '«Бэтмен 2».' in summary          # содержание не пострадало


def test_long_line_with_channel_name_survives():
    """Название канала внутри длинной строки — это текст новости, а не подпись.

    Слишком жадная чистка вырезала бы содержание, что хуже лишней строки.
    """
    long_line = ('Гиковский Вестник сообщает, что съёмки второго сезона '
                 'официально завершились на прошлой неделе в Ванкувере.')
    title, _ = bot._tg_title_and_summary(long_line, 'geekvestnik', 'Гиковский Вестник')
    assert title == long_line


def test_ordinary_post_is_untouched():
    title, summary = bot._tg_title_and_summary(
        'Заголовок новости.\nПодробности во втором абзаце.', 'ch', 'Канал')
    assert title == 'Заголовок новости.'
    assert summary == 'Подробности во втором абзаце.'
