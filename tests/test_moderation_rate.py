"""Антифлуд-темп: в среднем 2 сообщения за 5 секунд, запас на очередь до 4.

Превышение — не предупреждение: лишнее удаляется, человек молчит минуту.
Нарушением «флуд» становится третья пауза за 10 минут.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


# ------------------------------------------------------------------ ведро

def charge(n, *, at=0.0, cost=1, album=None, user=1):
    return [bot._mod_rate_charge(-100, user, cost, album=album, now=at) for _ in range(n)]


def test_burst_of_four_passes_fifth_is_over():
    assert charge(4) == ['', '', '', '']
    assert charge(1) == ['over']
    # Пауза открыта сразу: остальное из залпа — то же нарушение, а не новое.
    assert charge(1, at=2.5) == ['inflight']
    # После паузы ведро снова полное.
    assert charge(1, at=bot.MODERATION_RATE_PAUSE_SEC + 1) == ['']


def test_steady_two_per_five_seconds_is_never_over():
    assert all(r == '' for r in (bot._mod_rate_charge(-100, 1, 1, now=i * 2.5)
                                 for i in range(200)))


def test_sustained_faster_pace_is_caught():
    results = [bot._mod_rate_charge(-100, 1, 1, now=i * 2.0) for i in range(40)]
    first = results.index('over')
    assert 10 <= first <= 20, first


def test_stickers_cost_two():
    assert charge(3, cost=2) == ['', '', 'over']


def test_album_counts_once():
    assert charge(10, cost=2, album='a') == [''] * 10
    assert charge(1, cost=2, album='b') == ['']
    assert charge(1, cost=2, album='c') == ['over']


def test_users_and_chats_have_separate_buckets():
    charge(5, user=1)
    assert charge(1, user=2) == ['']
    assert bot._mod_rate_charge(-200, 1, 1, now=0.0) == ''


def test_rate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_RATE_MESSAGES', 0)
    assert charge(50) == [''] * 50


def test_messages_sent_before_the_pause_are_the_same_burst():
    charge(5)
    bot._mod_rate_after_pause(-100, 1, now=0.0)
    assert charge(3, at=10.0) == ['inflight'] * 3
    # После паузы ведро полное.
    assert charge(4, at=bot.MODERATION_RATE_PAUSE_SEC + 1.0) == [''] * 4


def test_cost_of_message_kinds():
    assert bot._mod_rate_cost(NS(text='привет', caption=None)) == 1
    assert bot._mod_rate_cost(NS(text=None, caption=None, sticker=NS(file_unique_id='s'))) == 2
    assert bot._mod_rate_cost(NS(text=None, caption='подпись', animation=NS(file_unique_id='g'))) == 2
    assert bot._mod_rate_cost(NS(text=None, caption=None, photo=[NS(file_unique_id='p')])) == 2
    assert bot._mod_rate_cost(NS(text=None, caption='подпись', photo=[NS(file_unique_id='p')])) == 1


def test_pause_counter_forgets_after_ten_minutes():
    assert bot._mod_rate_note_pause(-100, 1, now=0) == 1
    assert bot._mod_rate_note_pause(-100, 1, now=100) == 2
    assert bot._mod_rate_note_pause(-100, 1, now=100 + bot.MODERATION_RATE_ESCALATE_WINDOW_SEC + 1) == 1


# ------------------------------------------------------------- обработчик

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
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    tg = NS(get_chat_member=AsyncMock(return_value=NS(status='member')),
            delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
            send_message=AsyncMock(return_value=NS(message_id=900)),
            edit_message_text=AsyncMock())
    return NS(store=store, tg=tg)


def text(number, words='обычная реплика', user=51, **kw):
    row = dict(chat_id=-100, message_id=number, text=f'{words} {number}', caption=None,
               sender_chat=None, reply_to_message=None, media_group_id=None,
               message_thread_id=None, has_media_spoiler=False, link=None,
               from_user=NS(id=user, full_name='Участник', is_bot=False))
    row.update(kw)
    return NS(**row)


async def send(chat, message, edited=False):
    await bot.moderation_message_handler(
        NS(effective_message=message, effective_user=message.from_user,
           effective_chat=NS(id=-100), edited_message=message if edited else None),
        NS(bot=chat.tg))


def deleted(chat):
    return [call.args[1] for call in chat.tg.delete_message.await_args_list]


def chat_messages(chat):
    return [call.args[1] for call in chat.tg.send_message.await_args_list if call.args[0] == -100]


def admin_messages(chat):
    return [call.args[1] for call in chat.tg.send_message.await_args_list if call.args[0] == 7]


@pytest.mark.asyncio
async def test_fifth_quick_message_is_removed_and_the_author_paused(chat):
    for number in range(1, 6):
        await send(chat, text(number))
    assert deleted(chat) == [5]
    chat.tg.restrict_chat_member.assert_awaited_once()
    call = chat.tg.restrict_chat_member.await_args
    assert call.args[:2] == (-100, 51)
    seconds = (call.kwargs['until_date'] - bot.datetime.now(bot.timezone.utc)).total_seconds()
    assert bot.MODERATION_RATE_PAUSE_SEC - 5 <= seconds <= bot.MODERATION_RATE_PAUSE_SEC
    assert any('пауза на 1 мин' in m and 'не предупреждение' in m for m in chat_messages(chat))
    assert chat.store.warn_count(-100, 51) == 0
    assert admin_messages(chat) == []  # пауза — не повод писать админам
    assert chat.store.recent_log(1)[0]['action'] == 'пауза'
    chat.tg.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_burst_sent_all_at_once_is_one_pause(chat):
    await asyncio.gather(*(send(chat, text(n)) for n in range(1, 12)))
    assert sorted(deleted(chat)) == list(range(5, 12))
    chat.tg.restrict_chat_member.assert_awaited_once()
    assert chat.store.warn_count(-100, 51) == 0


@pytest.mark.asyncio
async def test_third_pause_in_ten_minutes_is_a_flood_violation(chat):
    number = 0
    for burst in range(3):
        for _ in range(5):
            number += 1
            await send(chat, text(number))
        # Пауза прошла (в тесте время не идёт — снимаем её вручную).
        bot._moderation_rate['-100:51']['paused_until'] = 0.0
        bot._moderation_rate['-100:51']['tokens'] = float(bot.MODERATION_RATE_BURST)
        bot._moderation_recent.clear()  # и прежние окна флуда за это время опустели
    assert chat.tg.restrict_chat_member.await_count == 2
    assert chat.store.warn_count(-100, 51) == 1
    assert any('флуд' in m for m in admin_messages(chat))
    assert deleted(chat) == [5, 10, 15]
    chat.tg.ban_chat_member.assert_not_awaited()
    # После нарушения счёт пауз начинается заново: следующий залп — снова пауза.
    for number in range(16, 21):
        await send(chat, text(number))
    assert chat.tg.restrict_chat_member.await_count == 3
    assert chat.store.warn_count(-100, 51) == 1


@pytest.mark.asyncio
async def test_real_violation_inside_a_flood_takes_its_own_path(chat):
    for number in range(1, 5):
        await send(chat, text(number))
    await send(chat, text(5, words='Твоя мать шлюха'))
    assert chat.store.recent_log(1)[0]['category'] == 'family'
    chat.tg.restrict_chat_member.assert_not_awaited()
    assert any('уровня бана' in m for m in admin_messages(chat))


@pytest.mark.asyncio
async def test_edits_do_not_spend_the_pace(chat):
    for number in range(1, 5):
        await send(chat, text(number))
    for version in range(6):
        await send(chat, text(4, words=f'исправление {version}'), edited=True)
    assert deleted(chat) == []


@pytest.mark.asyncio
async def test_someone_elses_restriction_is_left_alone(chat):
    chat.tg.get_chat_member.return_value = NS(status='restricted')
    for number in range(1, 6):
        await send(chat, text(number))
    assert deleted(chat) == [5]
    chat.tg.restrict_chat_member.assert_not_awaited()
    assert chat.store.recent_log(1)[0]['action'] == 'удалено'


@pytest.mark.asyncio
async def test_anonymous_senders_are_not_paced_at_all(chat):
    # У анонимных администраторов общее «ведро» на всех: считать их нельзя.
    for number in range(1, 9):
        await send(chat, text(number, from_user=None, sender_chat=NS(id=-555, title='Канал')))
    assert deleted(chat) == []
    chat.tg.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_mode_only_logs(chat):
    chat.store.set_mode('observe')
    for number in range(1, 6):
        await send(chat, text(number))
    assert deleted(chat) == []
    chat.tg.restrict_chat_member.assert_not_awaited()
    assert chat.store.recent_log(1)[0]['action'] == 'observe:пауза'


@pytest.mark.asyncio
async def test_sticker_flood_is_paused_on_the_third_sticker(chat):
    for number in range(1, 4):
        await send(chat, text(number, text=None, sticker=NS(file_unique_id=f's{number}', emoji='😀')))
    assert deleted(chat) == [3]
    chat.tg.restrict_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_notice_disappears_with_the_pause(chat, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_RATE_PAUSE_SEC', 0)
    for number in range(1, 6):
        await send(chat, text(number))
    await asyncio.sleep(0.01)
    assert 900 in deleted(chat)
