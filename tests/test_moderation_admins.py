"""The same content checks apply to admins, without impossible Telegram mutes."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

import anime_news_bot as bot


@pytest.fixture
def state(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7, 9])
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports'):
        monkeypatch.setattr(bot, name, {})
    return store


def message(number=1, text='Я тебя убью', **kw):
    row = dict(chat_id=-100, message_id=number, from_user=NS(id=7, full_name='Admin', is_bot=False),
               text=text, caption=None, sender_chat=None, reply_to_message=None, media_group_id=None)
    row.update(kw)
    return NS(**row)


def telegram(status='administrator'):
    return NS(get_chat_member=AsyncMock(return_value=NS(status=status)),
              delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
              ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
              send_message=AsyncMock(return_value=NS(message_id=900)), edit_message_text=AsyncMock())


async def handle(tg, msg, edited=False):
    await bot.moderation_message_handler(NS(effective_message=msg, effective_user=msg.from_user,
                                           effective_chat=NS(id=-100), edited_message=msg if edited else None), NS(bot=tg))


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['administrator', 'creator'])
@pytest.mark.parametrize('text', ['Я тебя убью', 'Твоя мать шлюха'])
async def test_admin_text_is_removed_and_warned_without_muting(state, status, text):
    tg = telegram(status)
    await handle(tg, message(text=text))
    tg.delete_message.assert_awaited_once_with(-100, 1)
    tg.restrict_chat_member.assert_not_awaited()
    tg.ban_chat_member.assert_not_awaited()
    tg.promote_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 1
    public = next(c for c in tg.send_message.call_args_list if c.args[0] == -100)
    assert 'Правила действуют и для администраторов' in public.args[1]
    assert 'Мут на' not in public.args[1] and 'Следующее нарушение — мут' not in public.args[1]
    for call in tg.send_message.call_args_list:
        if 'reply_markup' in call.kwargs:
            assert all(':ban:' not in b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['photo', 'animation', 'sticker', 'video', 'document'])
async def test_admin_media_is_scanned_without_llm(state, monkeypatch, kind):
    tg = telegram()
    scanner = AsyncMock(return_value=NS(status='checked', category='nsfw', reason='нагота', score=.95, frames=2))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    monkeypatch.setattr(bot, '_moderation_classify', AsyncMock(side_effect=AssertionError('offline')))
    item = NS(file_unique_id=kind, file_id='f', mime_type='video/mp4', is_video=True, is_animated=False)
    msg = message(text=None, **{kind: [item] if kind == 'photo' else item})
    await handle(tg, msg)
    scanner.assert_awaited_once()
    tg.delete_message.assert_awaited_once()
    assert state.warn_count(-100, 7) == 1
    tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_operator_without_chat_admin_role_gets_normal_mute(state):
    tg = telegram('member')
    await handle(tg, message())
    tg.restrict_chat_member.assert_awaited_once()
    assert state.warn_count(-100, 7) == 1


@pytest.mark.asyncio
async def test_admin_repeat_updates_and_albums_only_warn_once(state):
    tg = telegram()
    await handle(tg, message(1, media_group_id='a'))
    await handle(tg, message(1, media_group_id='a'))
    await handle(tg, message(2, media_group_id='a'))
    await handle(tg, message(3))
    assert state.warn_count(-100, 7) == 2
    assert tg.delete_message.await_count == 3
    tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('sender_id', [-100, -200])
@pytest.mark.parametrize('fake_user', [None, NS(id=1087968824, is_bot=True, full_name='GroupAnonymousBot')])
async def test_sender_chat_not_attributed_to_fake_user(state, sender_id, fake_user):
    tg = telegram()
    msg = message(sender_chat=NS(id=sender_id, title='<Group>'), from_user=fake_user)
    await handle(tg, msg)
    tg.delete_message.assert_awaited_once()
    tg.get_chat_member.assert_not_awaited()
    tg.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, sender_id) == 0
    assert state.warn_count(-100, 1087968824) == 0
    for c in tg.send_message.call_args_list:
        assert 'tg://user?id=-' not in c.args[1]
        assert '1087968824' not in c.args[1]
    assert state.recent_log(1)[0]['user_id'] == sender_id


@pytest.mark.asyncio
async def test_anonymous_media_without_caption_is_scanned(state, monkeypatch):
    tg = telegram()
    scanner = AsyncMock(return_value=NS(status='checked', category='nsfw', reason='нагота', score=.98, frames=1))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    await handle(tg, message(text=None, sender_chat=NS(id=-100, title='Group'), from_user=None,
                             photo=[NS(file_unique_id='a')]))
    scanner.assert_awaited_once()
    tg.delete_message.assert_awaited_once()
    assert state.warn_count(-100, -100) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [BadRequest('no rights'), TimedOut()])
async def test_anonymous_delete_failure_is_not_reported_as_success(state, error):
    tg = telegram()
    tg.delete_message.side_effect = error
    await handle(tg, message(sender_chat=NS(id=-100, title='Group')))
    assert state.recent_log(1)[0]['action'] in ('failed', 'unknown')
    assert all(c.args[0] != -100 for c in tg.send_message.call_args_list)


@pytest.mark.asyncio
async def test_unknown_membership_never_attempts_mute(state):
    tg = telegram()
    tg.get_chat_member.side_effect = TimedOut()
    await handle(tg, message())
    tg.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0
    assert state.recent_log(1)[0]['action'] == 'unknown'


@pytest.mark.asyncio
async def test_observe_mode_checks_admin_media_without_actions(state, monkeypatch):
    state.set_mode('observe')
    tg = telegram()
    scanner = AsyncMock(return_value=NS(status='checked', category='nsfw', reason='нагота', score=.98, frames=1))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    await handle(tg, message(text=None, photo=[NS(file_unique_id='a')]))
    scanner.assert_awaited_once()
    tg.delete_message.assert_not_awaited()
    tg.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_admin_warning_undo_is_incident_scoped(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    tg = telegram()
    decisions = []
    for n in (1, 2):
        d = bot._mod_decide('nsfw', 2, state.warn_count(-100, 7))
        await bot._mod_apply(tg, message(n), d, 'nsfw', '')
        decisions.append(d)
    query = NS(data=f'mod:undo:-100:7:{decisions[0]["incident_id"]}', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=tg))
    assert state.warn_count(-100, 7) == 1
    tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_ban_callback_never_calls_user_ban(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    state.reserve_incident('a', -100, -200, 'family', 'delete')
    state.update_incident('a', status='confirmed')
    tg = telegram()
    query = NS(data='mod:ban:-100:-200:a', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=tg))
    tg.get_chat_member.assert_not_awaited()
    tg.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_harmless_admin_message_is_untouched(state):
    tg = telegram()
    await handle(tg, message(text='Мне понравилась новая серия'))
    tg.delete_message.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('spoiler,category,removed', [(False, 'spoiler_16', True), (True, 'spoiler_16', False), (True, 'nsfw', True)])
async def test_admin_spoiler_policy_matches_regular_members(state, monkeypatch, spoiler, category, removed):
    tg = telegram()
    scanner = AsyncMock(return_value=NS(status='checked', category=category, reason='медиа', score=.98, frames=1))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    await handle(tg, message(text=None, photo=[NS(file_unique_id='a')], has_media_spoiler=spoiler))
    assert bool(tg.delete_message.await_count) is removed
    assert state.warn_count(-100, 7) == int(removed)


@pytest.mark.asyncio
async def test_unchecked_anonymous_media_is_logged_without_punishment(state, monkeypatch):
    tg = telegram()
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=NS(status='unchecked', reason='занят'))))
    await handle(tg, message(text=None, photo=[NS(file_unique_id='a')], from_user=None,
                             sender_chat=NS(id=-100, title='Group')))
    assert state.recent_log(1)[0]['action'] == 'не проверено'
    tg.delete_message.assert_not_awaited()
    tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_edit_to_violation_is_checked(state):
    tg = telegram()
    await handle(tg, message(text='Хорошая серия'))
    await handle(tg, message(), edited=True)
    tg.delete_message.assert_awaited_once()
    assert state.warn_count(-100, 7) == 1


@pytest.mark.asyncio
async def test_bot_operator_can_be_banned_manually_when_regular_member(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, '_audit_update', lambda *a, **k: None)
    state.reserve_incident('a', -100, 7, 'family', 'escalate')
    state.update_incident('a', status='confirmed')
    tg = telegram('member')
    query = NS(data='mod:ban:-100:7:a', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=tg))
    tg.ban_chat_member.assert_awaited_once_with(-100, 7)
