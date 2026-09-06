"""Observable publication contracts kept stable across future refactors.

These tests intentionally assert results and persisted outcomes. They do not
assert which private helper was called or where a helper lives.
"""
from __future__ import annotations

import importlib
import sys

import pytest


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(('message', kwargs))
        return object()

    async def send_photo(self, **kwargs):
        self.messages.append(('photo', kwargs))
        return object()

    async def send_media_group(self, **kwargs):
        self.messages.append(('media_group', kwargs))
        return [object()]

    async def send_video(self, **kwargs):
        self.messages.append(('video', kwargs))
        return object()


@pytest.fixture
def running_bot_module(tmp_path, monkeypatch):
    # Import a fresh copy for an isolated DATA_DIR, but restore the process-wide
    # module afterwards. The full regression suite imports anime_news_bot during
    # collection, so permanently replacing sys.modules here would make later
    # importlib.reload() calls operate on a different module object.
    previous = sys.modules.get('anime_news_bot')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BOT_TOKEN', 'e2e-placeholder')
    monkeypatch.setenv('ADMIN_ID', '1')
    monkeypatch.setenv('CHANNEL_ID', '-1001234567890')
    monkeypatch.setenv('FEATURE_SOURCE_DISCOVERY', 'false')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('DEEPL_API_KEY', raising=False)
    sys.modules.pop('anime_news_bot', None)
    module = importlib.import_module('anime_news_bot')
    module._init_globals()  # bootstrap only; assertions below are external outcomes
    module.settings.llm_enabled = False
    module.settings.dedup_final_text = False
    module.settings.image_dedup_enabled = False
    module.settings.require_image = False
    try:
        yield module
    finally:
        sys.modules.pop('anime_news_bot', None)
        if previous is not None:
            sys.modules['anime_news_bot'] = previous


@pytest.mark.asyncio
async def test_same_story_is_delivered_once(running_bot_module):
    botmod = running_bot_module
    fake = FakeBot()
    news = {
        'title': 'Тестовая новость о новом аниме',
        'summary': 'Подтверждён новый проект и опубликованы первые подробности.',
        'source': 'E2E',
        'link': '',
        'lang': 'ru',
    }
    first = await botmod.send_news(fake, dict(news), apply_dedup=False, llm_side_effects=False)
    second = await botmod.send_news(fake, dict(news), apply_dedup=False, llm_side_effects=False)
    assert first == 'sent'
    assert second == 'skipped_dup'
    assert len(fake.messages) == 1


@pytest.mark.asyncio
async def test_preview_does_not_consume_channel_history(running_bot_module):
    botmod = running_bot_module
    fake = FakeBot()
    news = {
        'title': 'Отдельная тестовая новость',
        'summary': 'Сначала она просматривается администратором, затем публикуется.',
        'source': 'E2E',
        'link': '',
        'lang': 'ru',
    }
    preview = await botmod.send_news(
        fake, dict(news), chat_id=1, track_history=False,
        apply_dedup=False, llm_side_effects=False,
    )
    publish = await botmod.send_news(
        fake, dict(news), apply_dedup=False, llm_side_effects=False,
    )
    assert preview == 'sent'
    assert publish == 'sent'
    assert len(fake.messages) == 2
