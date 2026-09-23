"""/posts — опубликованные посты файлом, чтобы разбирать то, что реально вышло.

Ветка с постами закрытая, снаружи её не прочесть, а прогон на стенде идёт без
модели — то есть совсем не так, как пишет бот в канале.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def _news(n):
    return {'title': f'Grand Blue Season {n} Announced', 'summary': 'Teaser visual revealed',
            'source': 'Anime Corner', 'link': f'https://animecorner.me/grand-blue-{n}/'}


@pytest.fixture
def history(tmp_path, monkeypatch):
    store = bot.PublishedStoryStore(tmp_path / 'published.json')
    monkeypatch.setattr(bot, 'story_history', store)
    return store


def test_export_shows_the_source_and_what_went_out(history):
    history.record(dict(_news(4), _prompt_version='v7'), 'Анонсирован 4 сезон «Необъятного океана».')
    history.record(_news(5), 'Grand Blue: сезон 5.')
    text = bot._posts_export_text(history.recent(10))
    first, second = text.split('#2 · ')
    assert first.startswith('Последние опубликованные посты: 2.')
    assert 'Anime Corner · без модели' in first and 'Season 5' in first       # новые первыми
    assert 'Анонсирован 4 сезон' in second and 'модель' in second
    assert 'https://animecorner.me/grand-blue-4' in second


def test_recent_is_newest_first_and_limited(history):
    for n in range(1, 6):
        history.record(_news(n), f'пост {n}')
    assert [row['rendered'] for row in history.recent(2)] == ['пост 5', 'пост 4']


@pytest.mark.asyncio
async def test_command_sends_a_text_file(history, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    history.record(_news(4), 'Анонсирован 4 сезон.')
    send = AsyncMock()
    update = NS(message=NS(reply_text=AsyncMock()), effective_chat=NS(id=42),
                effective_user=NS(id=1))
    await bot.posts_command(update, NS(args=['5'], bot=NS(send_document=send)))
    kwargs = send.await_args.kwargs
    assert kwargs['chat_id'] == 42 and kwargs['filename'].endswith('.txt')
    assert 'Анонсирован 4 сезон.' in kwargs['document'].decode('utf-8')


@pytest.mark.asyncio
async def test_empty_journal_says_so(history, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    reply = AsyncMock()
    update = NS(message=NS(reply_text=reply), effective_chat=NS(id=42), effective_user=NS(id=1))
    await bot.posts_command(update, NS(args=[], bot=NS(send_document=AsyncMock())))
    assert 'пока нет' in reply.await_args.args[0]
