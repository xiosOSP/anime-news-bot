"""16+ в чате разрешено: наказание и удаление — только за 18+.

Админы попросили (октябрь 2026): бельё, купальники и ягодицы в аниме-чате —
обычный фансервис, предупреждения за них были самой шумной санкцией бота.
Находку 16+ с признаками 18+ чуть ниже порога всё ещё смотрит человек.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import moderation_media as media
from conftest import with_media_senders

BUTTOCKS = 'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED'


@pytest.fixture
def chat(tmp_path, monkeypatch):
    bot._init_globals()
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', False)
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports'):
        monkeypatch.setattr(bot, name, {})

    async def run(scan, number=1):
        tg = with_media_senders(NS(
            get_chat_member=AsyncMock(return_value=NS(status='member')),
            delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
            send_message=AsyncMock(return_value=NS(message_id=900)),
            edit_message_text=AsyncMock()))
        monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=scan)))
        msg = NS(chat_id=-100, message_id=number, text=None, caption=None, sender_chat=None,
                 reply_to_message=None, media_group_id=None, has_media_spoiler=False,
                 from_user=NS(id=50 + number, full_name='Участник', is_bot=False),
                 animation=NS(file_unique_id=f'g{number}', file_id='f', mime_type='video/mp4'))
        await bot.moderation_message_handler(
            NS(effective_message=msg, effective_user=msg.from_user,
               effective_chat=NS(id=-100), edited_message=None), NS(bot=tg))
        return tg
    return NS(run=run, store=store)


def _untouched(tg, store, user=51):
    tg.delete_message.assert_not_awaited()
    tg.restrict_chat_member.assert_not_awaited()
    assert store.warn_count(-100, user) == 0


@pytest.mark.asyncio
async def test_confident_16_plus_is_left_alone(chat):
    tg = await chat.run(media.Scan('checked', 'spoiler_16', BUTTOCKS, 16, .97, hits=8))
    _untouched(tg, chat.store)
    tg.send_message.assert_not_awaited()          # и админа не дёргаем


@pytest.mark.asyncio
async def test_borderline_16_plus_does_not_call_an_admin(chat):
    scan = media.Scan('unchecked', reason='Похоже на 16+; нужна ручная проверка',
                      frames=3, score=.88, borderline=True)
    tg = await chat.run(scan)
    _untouched(tg, chat.store)
    tg.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_16_plus_close_to_18_goes_to_a_human_without_sanction(chat):
    scan = media.Scan('checked', 'spoiler_16', BUTTOCKS, 16, .97, hits=8, near_explicit=2)
    tg = await chat.run(scan)
    _untouched(tg, chat.store)
    tg.send_message.assert_awaited()               # отчёт админу на ручную оценку


@pytest.mark.asyncio
async def test_18_plus_is_still_removed_and_muted(chat):
    scan = media.Scan('checked', 'nsfw', 'Обнаружена явная нагота: FEMALE_BREAST_EXPOSED', 4, .97)
    tg = await chat.run(scan)
    tg.delete_message.assert_awaited_once_with(-100, 1)
    tg.restrict_chat_member.assert_awaited()


@pytest.mark.asyncio
async def test_blocklisted_16_plus_copy_stays(chat):
    chat.store.block_media('r1', -100, 'spoiler_16', ['g1'], [], 'test')
    tg = await chat.run(media.Scan('checked', '', '', 4, 0.0))
    _untouched(tg, chat.store)


def test_decision_for_16_plus_is_none_unless_owner_turns_it_back_on(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', False)
    assert bot._mod_decide('spoiler_16', 2, 0)['action'] == 'none'
    assert bot._mod_decide('nsfw', 2, 0)['action'] == 'mute'
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', True)
    assert bot._mod_decide('spoiler_16', 2, 0)['action'] == 'warn'
