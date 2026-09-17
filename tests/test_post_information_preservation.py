"""Post formatting must remove repetition without removing new facts."""
import json
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('detail', [
    'В сезоне будет 12 эпизодов.',
    'Показ состоится на Netflix.',
    'Производством занимается студия Bones.',
    'Режиссёр — Хироюки Имаиси.',
    'Премьера состоится 4 октября.',
    'Премьера сегодня.',
    'Дубляжа не будет.',
])
def test_repeated_headline_does_not_swallow_new_details(detail):
    title = 'Аниме «Атака титанов» получит новый сезон'
    summary = f'{title}. {detail}'
    assert not bot._too_similar(title, summary)
    assert bot._drop_repetitive_paragraphs(title, [summary]) == [detail]


@pytest.mark.parametrize('title, summary', [
    ('Сезон аниме выйдет 4 октября', 'Сезон аниме выйдет 14 октября'),
    ('Сезон аниме выйдет завтра', 'Сезон аниме выйдет сегодня'),
    ('Будет дубляж', 'Дубляжа не будет'),
    ('Аниме выйдет на Netflix', 'Аниме выйдет на Crunchyroll'),
    ('Продажи манги достигли 123450 копий', 'Продажи манги достигли 123459 копий'),
])
def test_overlap_does_not_prove_redundancy(title, summary):
    assert not bot._too_similar(title, summary)
    assert bot._drop_repetitive_paragraphs(title, [summary]) == [summary]


def test_actual_repeated_sentences_are_removed():
    title = 'Вышел трейлер «Атаки титанов»'
    assert bot._drop_repetitive_paragraphs(title, [
        title + '.', 'Показ на Netflix.', 'Показ на Netflix.',
    ]) == ['Показ на Netflix.']


def test_fallback_preserves_facts_after_verbatim_title(monkeypatch):
    monkeypatch.setattr(bot, 'settings', None)
    text = bot.format_news_short({
        'title': 'Анонсирован новый сезон «Блича»', 'lang': 'ru',
        'summary': 'Анонсирован новый сезон «Блича». В сезоне будет 12 эпизодов.',
    })
    assert '12 эпизодов' in text
    assert text.count('Анонсирован новый сезон') == 1


@pytest.mark.parametrize('body, date, expected', [
    ('Премьера 4 октября 2026.', '4 октября 2026', False),
    ('Премьера 4\nоктября 2026.', '4 октября 2026', False),
    ('Премьера 14 октября 2026.', '4 октября 2026', True),
    ('Премьера в октябре.', '4 октября 2026', True),
    ('Премьера скоро.', '', False),
])
def test_calendar_does_not_repeat_complete_date(body, date, expected):
    assert ('📅' in bot._append_release_date(body, date)) is expected


@pytest.mark.asyncio
async def test_enrichment_keeps_details_and_prints_date_once(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    bot.settings.llm_read_article = False
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, 'recent_subjects', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', False)
    call = AsyncMock(return_value=json.dumps({
        'topic': 'аниме', 'kind': 'новость', 'title': 'Вышел трейлер «Блича»',
        'summary': 'Вышел трейлер «Блича». Премьера 4 октября 2026 на Netflix. Всего 12 эпизодов.',
        'tags': ['аниме'],
    }, ensure_ascii=False))
    monkeypatch.setattr(bot, '_llm_call', call)
    news = {'title': 'Bleach trailer revealed',
            'summary': 'Premiere October 4, 2026 on Netflix. There are 12 episodes.'}
    assert await bot._llm_enrich(news, side_effects=False) == 'ok'
    text = bot.format_news_short(news)
    assert '12 эпизодов' in text and 'Netflix' in text
    assert text.count('Вышел трейлер') == 1
    assert text.count('4 октября 2026') == 1
    assert '#аниме' in text
    call.assert_awaited_once()
