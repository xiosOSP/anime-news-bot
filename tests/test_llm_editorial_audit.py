"""Regressions for source dates, stale rewrites and partial-batch cost."""
import json
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('source, output, expected', [
    ('Premiere October 4.', 'Премьера 4 мая.', False),
    ('A new trailer by Mark.', 'Трейлер выйдет в марте.', False),
    ('Augustus revealed the trailer.', 'Трейлер выйдет в августе.', False),
    ('The premiere is in May.', 'Премьера в мае.', True),
    ('The premiere is in MAY.', 'Премьера в мае.', True),
    ('Premiere Sept. 4.', 'Премьера 4 сентября.', True),
    ('Премьера в мае.', 'Премьера в мае.', True),
    ('A trailer featuring Martin.', 'В трейлере появился Мартин.', True),
])
def test_month_validation_uses_month_words(source, output, expected):
    assert bot._llm_dates_supported(source, output) is expected


@pytest.fixture
def editorial(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    bot.settings.llm_read_article = False
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, 'recent_subjects', None)
    monkeypatch.setattr(bot, 'entity_memory', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', False)


@pytest.mark.asyncio
async def test_two_items_reserve_only_two_output_budgets(editorial, monkeypatch):
    monkeypatch.setattr(bot, 'LLM_BATCH_SIZE', 4)
    monkeypatch.setattr(bot, 'LLM_MAX_TOKENS', 700)
    monkeypatch.setattr(bot, 'LLM_BATCH_MAX_TOKENS', 2800)
    reply = AsyncMock(return_value=json.dumps({'items': [
        {'id': 1, 'title': 'Первый трейлер'},
        {'id': 2, 'title': 'Второй трейлер'},
    ]}))
    monkeypatch.setattr(bot, '_llm_call', reply)
    assert await bot._llm_enrich_chunk([
        {'title': 'First trailer', 'summary': 'First story.'},
        {'title': 'Second trailer', 'summary': 'Second story.'},
    ]) == 2
    assert reply.call_args.kwargs['max_tokens'] == 1400


@pytest.mark.asyncio
async def test_rejected_new_rewrite_cannot_publish_previous_text(editorial, monkeypatch):
    news = {'title': 'Bleach trailer revealed', 'summary': 'New footage revealed.',
            '_llm_text': 'Старый текст с уже неактуальной датой.', '_llm_tags': '#устарело'}
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=json.dumps({
        'topic': 'аниме', 'kind': 'трейлер', 'title': 'Bleach выйдет в 2099 году',
        'summary': '', 'tags': [],
    }, ensure_ascii=False)))
    assert await bot._llm_enrich(news, side_effects=False) == 'ok'
    assert '_llm_text' not in news
    assert '_llm_tags' not in news
