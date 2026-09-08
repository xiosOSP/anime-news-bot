"""Immediate moderation, quiet notices, and independent news/chat LLM routes."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions

import anime_news_bot as bot
from moderation_rules import check_text


@pytest.fixture
def state(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, '_moderation_action_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_moderation_update_lock', asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates',
                 '_moderation_windows', '_moderation_recent', '_moderation_user_notices', '_moderation_report_recent'):
        monkeypatch.setattr(bot, name, {})
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [])
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    return store


def message(number=1, text='Заткнись', **kwargs):
    values = dict(chat_id=-100, message_id=number, from_user=NS(id=7, full_name='User', is_bot=False),
                  text=text, caption=None, sender_chat=None, media_group_id=None, reply_to_message=None)
    values.update(kwargs)
    return NS(**values)


def telegram():
    return NS(get_chat_member=AsyncMock(return_value=NS(status='member')),
              delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(return_value=True),
              send_message=AsyncMock(return_value=NS(message_id=900)), edit_message_text=AsyncMock(),
              get_chat=AsyncMock(return_value=NS(permissions=ChatPermissions.all_permissions())))


async def apply(store, tg, msg):
    decision = bot._mod_decide('aggression', 2, store.warn_count(-100, 7))
    await bot._mod_apply(tg, msg, decision, 'aggression', '')
    return decision


@pytest.mark.asyncio
async def test_new_violations_escalate_without_waiting_and_without_notice_spam(state):
    tg = telegram()
    first = await apply(state, tg, message(1))
    second = await apply(state, tg, message(2))
    assert first['applied_action'] == 'warn'
    assert second['applied_action'] == 'mute' and second['minutes'] == 60
    tg.get_chat_member.return_value = NS(status='restricted', until_date=tg.restrict_chat_member.call_args.kwargs['until_date'])
    third = await apply(state, tg, message(3))
    assert third['applied_action'] == 'mute' and third['minutes'] == 1440
    assert state.warn_count(-100, 7) == 3
    assert tg.send_message.await_count == 1
    assert tg.edit_message_text.await_count == 2
    assert tg.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_already_maximum_bot_mute_deletes_without_repeated_restrict(state):
    tg = telegram()
    await apply(state, tg, message(1))
    await apply(state, tg, message(2))
    tg.get_chat_member.return_value = NS(status='restricted', until_date=tg.restrict_chat_member.call_args.kwargs['until_date'])
    await apply(state, tg, message(3))
    tg.get_chat_member.return_value = NS(status='restricted', until_date=tg.restrict_chat_member.call_args.kwargs['until_date'])
    for number in range(4, 12):
        decision = await apply(state, tg, message(number))
        assert decision['applied_action'] == 'delete'
    assert state.warn_count(-100, 7) == 3
    assert tg.delete_message.await_count == 11
    assert tg.restrict_chat_member.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('next_severity', [2, 3])
async def test_severe_first_mute_escalates_on_next_violation(state, next_severity):
    tg = telegram()
    first = bot._mod_decide('aggression', 3, 0)
    await bot._mod_apply(tg, message(1), first, 'aggression', '')
    assert first['minutes'] == 60
    tg.get_chat_member.return_value = NS(status='restricted', until_date=tg.restrict_chat_member.call_args.kwargs['until_date'])
    second = bot._mod_decide('aggression', next_severity, state.warn_count(-100, 7))
    await bot._mod_apply(tg, message(2), second, 'aggression', '')
    assert second['minutes'] == 1440
    assert second['applied_action'] == 'mute'
    assert state.warn_count(-100, 7) == 2


@pytest.mark.asyncio
async def test_undo_extension_restores_previous_mute(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    tg = telegram()
    await apply(state, tg, message(1))
    previous = await apply(state, tg, message(2))
    previous_until = state.incident(previous['incident_id'])['mute_until']
    tg.get_chat_member.return_value = NS(status='restricted', until_date=datetime.fromtimestamp(previous_until, timezone.utc))
    latest = await apply(state, tg, message(3))
    tg.get_chat_member.return_value = NS(status='restricted', until_date=tg.restrict_chat_member.call_args.kwargs['until_date'])
    query = NS(data=f'mod:undo:-100:7:{latest["incident_id"]}', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=tg))
    assert tg.restrict_chat_member.call_args.kwargs['until_date'] == previous_until
    assert not any(tg.restrict_chat_member.call_args.kwargs['permissions'].to_dict().values())
    assert state.warn_count(-100, 7) == 2


@pytest.mark.asyncio
async def test_obvious_text_does_not_wait_for_media_or_model(state, monkeypatch):
    tg = telegram()
    scanner = AsyncMock(side_effect=AssertionError('must not wait for a decoder'))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    monkeypatch.setattr(bot, '_moderation_classify', AsyncMock(side_effect=AssertionError('local verdict')))
    msg = message(text='Я тебя убью', photo=[NS(file_unique_id='photo')])
    await bot.moderation_message_handler(NS(effective_message=msg, effective_user=msg.from_user,
                                             effective_chat=NS(id=-100)), NS(bot=tg))
    tg.delete_message.assert_awaited_once()
    tg.restrict_chat_member.assert_awaited_once()
    scanner.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_model_does_not_enter_news_lock_or_use_news_quota(monkeypatch):
    client = NS(configured=True, complete=AsyncMock(return_value='chat result'))
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, '_get_moderation_llm_client', lambda: client)
    lock = asyncio.Lock()
    monkeypatch.setattr(bot, '_llm_lock', lock)
    async with lock:
        result = await asyncio.wait_for(bot._llm_call([], task='moderation'), 1)
    assert result == 'chat result'
    client.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_outage_never_falls_back_to_news(monkeypatch):
    client = NS(configured=True, complete=AsyncMock(return_value=None))
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, '_get_moderation_llm_client', lambda: client)
    def forbid_news():
        raise AssertionError('chat failure must not spend news quota')
    monkeypatch.setattr(bot, '_llm_can_call', forbid_news)
    assert await bot._llm_call([], task='moderation') is None
    client.configured = False
    assert await bot._llm_call([], task='moderation') is None


def test_profiles_do_not_inherit_news_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    for name in ('MODERATION_LLM_API_KEY', 'MODERATION_LLM_MODEL', 'MODERATION_LLM_BASE_URL'):
        monkeypatch.setattr(bot, name, '')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'news-only-key')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'news-model')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://news.example/v1')
    client = bot._get_moderation_llm_client()
    assert not client.configured
    assert client.api_key == '' and client.model == ''
    monkeypatch.setattr(bot, 'MODERATION_LLM_API_KEY', 'chat-key')
    monkeypatch.setattr(bot, 'MODERATION_LLM_MODEL', 'chat-model')
    monkeypatch.setattr(bot, 'MODERATION_LLM_BASE_URL', 'https://chat.example/v1')
    client = bot._get_moderation_llm_client()
    assert client.configured and client.model == 'chat-model'
    assert client.api_key == 'chat-key'
    assert 'chat-key' not in str(client.snapshot())


def test_chat_model_key_redacted(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_LLM_API_KEY', 'chat-private-key-123456789')
    assert 'chat-private-key-123456789' not in bot._redact_secrets('error chat-private-key-123456789')


def test_prompt_and_context_are_bounded(state):
    for i in range(12):
        bot._mod_note_message(-100, i, 'private full name', 'sample text ' * 80)
    context = bot._moderation_render_context(-100, 'target')
    assert context.count('Участник') == 4
    assert 'private full name' not in context
    assert len(bot.MODERATION_SYSTEM_PROMPT) < 2200


@pytest.mark.parametrize('text', [
    'Я тебя убью если не переведешь деньги',
    'Убью тебя когда выйдешь из дома',
    'Я тебя зарежу если заспойлеришь',
])
def test_condition_does_not_exempt_real_threats(text):
    verdict = check_text(text)
    assert verdict is not None and verdict.category == 'aggression' and verdict.severity == 3
