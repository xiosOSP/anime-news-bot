"""Edits and concurrent arrivals must not mix target and context."""
import json
from collections import deque
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
from test_moderation_offline import handle, message, telegram_bot, state as moderation_state


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setattr(bot, '_moderation_windows', {5: deque([
        {'message_id': 10, 'user_id': 1, 'text': 'обсуждаем сериал'},
        {'message_id': 11, 'user_id': 2, 'text': 'исправленная реплика'},
        {'message_id': 12, 'user_id': 3, 'text': 'поздняя чужая реплика'},
    ])})


def test_edit_uses_only_messages_before_target(chat):
    rendered = bot._moderation_render_context(5, 'исправленная реплика', message_id=11)
    assert 'обсуждаем сериал' in rendered
    assert 'поздняя чужая реплика' not in rendered
    assert rendered.count('исправленная реплика') == 1


def test_first_message_has_no_foreign_context(chat):
    rendered = bot._moderation_render_context(5, 'обсуждаем сериал', message_id=10)
    assert 'Участник' not in rendered
    assert 'исправленная реплика' not in rendered


def test_evicted_target_does_not_acquire_unrelated_context(chat):
    rendered = bot._moderation_render_context(5, 'старое сообщение', message_id=1)
    assert 'Участник' not in rendered


def test_manual_probe_keeps_last_real_message(chat):
    rendered = bot._moderation_render_context(5, 'текст для проверки')
    assert 'поздняя чужая реплика' in rendered


def test_legacy_caller_excludes_target_only_when_it_matches(chat):
    rendered = bot._moderation_render_context(5, 'поздняя чужая реплика')
    assert rendered.count('поздняя чужая реплика') == 1


@pytest.mark.asyncio
async def test_classifier_sends_context_for_exact_target(chat, monkeypatch):
    monkeypatch.setattr(bot, '_moderation_llm_ready', lambda: True)
    monkeypatch.setattr(bot, '_moderation_llm_budget_left', lambda: 10)
    call = AsyncMock(return_value=json.dumps({'violation': False}))
    monkeypatch.setattr(bot, '_llm_call', call)
    assert (await bot._moderation_classify(5, 'исправленная реплика', message_id=11))['violation'] is False
    prompt = call.call_args.args[0][1]['content']
    assert 'поздняя чужая реплика' not in prompt
    assert prompt.count('исправленная реплика') == 1
    call.assert_awaited_once()


@pytest.fixture
def state(tmp_path, monkeypatch):
    return moderation_state.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['edit', 'eviction', 'other_message', 'none'])
async def test_delayed_verdict_cannot_punish_replaced_message(state, monkeypatch, change):
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    async def classify(chat_id, text, *, message_id):
        assert message_id == 11
        if change == 'edit':
            bot._mod_note_message(chat_id, 7, 'User', 'исправил опечатку',
                                  message_id=11, counts_as_new=False)
        elif change == 'eviction':
            bot._moderation_windows[chat_id].clear()
        elif change == 'other_message':
            bot._mod_note_message(chat_id, 8, 'Other', 'новое сообщение', message_id=12)
        # Уверенность выше порога: с PR #63 вердикт модели без неё уходит в
        # ручной разбор и ничего не удаляет. Тест проверяет устаревание
        # вердикта, а не порог, поэтому вердикт здесь заведомо годный.
        return {'violation': True, 'category': 'toxic', 'severity': 2,
                'confidence': .99, 'reason': 'оскорбление'}
    model = AsyncMock(side_effect=classify)
    monkeypatch.setattr(bot, '_moderation_classify', model)
    tg = telegram_bot()
    await handle(message('ты дурак что ли', number=11), tg)
    model.assert_awaited_once()
    if change in ('edit', 'eviction'):
        tg.delete_message.assert_not_awaited()
        tg.restrict_chat_member.assert_not_awaited()
        tg.send_message.assert_not_awaited()
        assert state.warn_count(-100, 7) == 0
    else:
        tg.delete_message.assert_awaited_once()
