"""Медиа, которое не удалось проверить сразу, проверяется заново.

Очередь детектора короткая, а после трёх таймаутов он берёт паузу на 10 минут:
всё, что пришло в это время, раньше оставалось непроверенным навсегда — ровно
тогда, когда медиа много (налёт).
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import moderation_media as media
from conftest import with_media_senders


@pytest.fixture
def chat(tmp_path, monkeypatch):
    bot._init_globals()
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
                 '_moderation_media_reports', '_moderation_rate'):
        monkeypatch.setattr(bot, name, {})
    monkeypatch.setattr(bot, '_moderation_media_retries', bot.OrderedDict())
    tg = with_media_senders(NS(
        get_chat_member=AsyncMock(return_value=NS(status='member')),
        delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
        send_message=AsyncMock(return_value=NS(message_id=900)),
        edit_message_text=AsyncMock()))
    return NS(store=store, tg=tg)


def gif(number=1):
    return NS(chat_id=-100, message_id=number, text=None, caption=None, sender_chat=None,
              reply_to_message=None, media_group_id=None, has_media_spoiler=False,
              from_user=NS(id=50 + number, full_name='Участник', is_bot=False),
              animation=NS(file_unique_id=f'g{number}', file_id='f', mime_type='video/mp4'))


async def handle(chat, message):
    await bot.moderation_message_handler(
        NS(effective_message=message, effective_user=message.from_user,
           effective_chat=NS(id=-100), edited_message=None), NS(bot=chat.tg))


BUSY = media.Scan('unchecked', '', 'Детектор занят', retry_after=30)
FOUND = media.Scan('checked', 'nsfw', 'Обнажённое тело: FEMALE_BREAST_EXPOSED', 20, .97, hits=12)


def scanner(monkeypatch, *scans):
    check = AsyncMock(side_effect=list(scans))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=check))
    return check


def admin_texts(chat):
    return [str(c.args[1]) for c in chat.tg.send_message.await_args_list if c.args[0] == 7]


# ------------------------------------------------------- планирование

@pytest.mark.asyncio
async def test_retry_is_capped_and_delay_is_clamped(chat, monkeypatch):
    delays = []

    async def fake(_bot, _message, delay):
        delays.append(delay)
    monkeypatch.setattr(bot, '_mod_media_retry', fake)
    message = gif()
    results = [bot._mod_schedule_media_retry(chat.tg, message, 30) for _ in range(5)]
    assert results == [True, True, True, False, False]
    await asyncio.sleep(0)
    assert delays == [30, 30, 30]
    # Другое сообщение — свой счётчик; задержка ограничена сверху и снизу.
    assert bot._mod_schedule_media_retry(chat.tg, gif(2), 100000)
    assert bot._mod_schedule_media_retry(chat.tg, gif(3), 1)
    await asyncio.sleep(0)
    assert delays[3:] == [900, 5]


@pytest.mark.asyncio
async def test_retry_needs_the_media_check_and_a_delay(chat, monkeypatch):
    assert bot._mod_schedule_media_retry(chat.tg, gif(), 0) is False
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', False)
    assert bot._mod_schedule_media_retry(chat.tg, gif(), 30) is False


# ------------------------------------------------------- обработчик

@pytest.mark.asyncio
async def test_busy_detector_schedules_a_retry_and_stays_quiet(chat, monkeypatch):
    scheduled = []
    monkeypatch.setattr(bot, '_mod_schedule_media_retry',
                        lambda _b, message, delay: scheduled.append((message.message_id, delay)) or True)
    scanner(monkeypatch, BUSY)
    await handle(chat, gif())
    assert scheduled == [(1, 30)]
    chat.tg.delete_message.assert_not_awaited()
    assert admin_texts(chat) == []          # письмо не нужно: проверка будет повторена


@pytest.mark.asyncio
async def test_no_retry_left_means_the_owner_is_told(chat, monkeypatch):
    monkeypatch.setattr(bot, '_mod_schedule_media_retry', lambda *_a: False)
    scanner(monkeypatch, BUSY)
    await handle(chat, gif())
    assert len(admin_texts(chat)) == 1


@pytest.mark.asyncio
async def test_scan_without_retry_hint_is_not_repeated(chat, monkeypatch):
    scheduled = []
    monkeypatch.setattr(bot, '_mod_schedule_media_retry',
                        lambda *a: scheduled.append(a) or True)
    scanner(monkeypatch, media.Scan('unchecked', '', 'Формат не поддерживается'))
    await handle(chat, gif())
    assert scheduled == []


# ------------------------------------------------------- сама повторная проверка

@pytest.mark.asyncio
async def test_second_look_finds_and_removes_the_media(chat, monkeypatch):
    check = scanner(monkeypatch, BUSY, FOUND)
    message = gif()
    await handle(chat, message)
    chat.tg.delete_message.assert_not_awaited()
    await bot._mod_media_retry(chat.tg, message, 0)
    assert check.await_count == 2
    chat.tg.delete_message.assert_awaited_once_with(-100, 1)
    chat.tg.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_handled_media_is_not_scanned_again(chat, monkeypatch):
    check = scanner(monkeypatch, BUSY, FOUND)
    message = gif()
    await handle(chat, message)
    bot._mod_recent_media_row(-100, 1)['handled'] = True     # админ уже решил
    await bot._mod_media_retry(chat.tg, message, 0)
    assert check.await_count == 1
    chat.tg.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_does_not_spend_the_flood_pace(chat, monkeypatch):
    scanner(monkeypatch, BUSY, media.Scan('checked'))
    message = gif()
    await handle(chat, message)
    before = {k: dict(v) for k, v in bot._moderation_rate.items()}
    await bot._mod_media_retry(chat.tg, message, 0)
    assert {k: v['tokens'] for k, v in bot._moderation_rate.items()} == {
        k: v['tokens'] for k, v in before.items()}
    chat.tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_is_not_counted_as_another_message(chat, monkeypatch):
    # Иначе «повтор того же гифа» набирал бы счётчики флуда и повторов.
    scanner(monkeypatch, BUSY, media.Scan('checked'))
    message = gif()
    await handle(chat, message)
    await bot._mod_media_retry(chat.tg, message, 0)
    rows = [r for r in bot._moderation_windows.get(-100, ()) if r.get('message_id') == 1]
    assert len(rows) == 1
