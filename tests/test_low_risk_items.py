"""Мелкие хвосты аудита: повтор ролика YouTube из разных источников и прочее."""
import asyncio
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot


# ------------------------------------------------- один ролик с разных сайтов

@pytest.fixture
def hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(image_dedup=True))
    store = bot.ImageHashes(tmp_path / 'h.json')
    monkeypatch.setattr(bot, 'image_hashes', store)
    return store


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=10',
    'https://youtu.be/dQw4w9WgXcQ?si=abc',
    'https://www.youtube.com/shorts/dQw4w9WgXcQ',
    'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ',
    'https://m.youtube.com/live/dQw4w9WgXcQ',
])
def test_youtube_id_from_every_link_shape(url):
    assert bot._youtube_id(url) == 'dQw4w9WgXcQ'


@pytest.mark.parametrize('url', ['', 'https://vimeo.com/123', 'https://youtube.com/watch?v=short',
                                 'https://youtube.com/watch?v=dQw4w9WgXcQextra'])
def test_not_a_youtube_video(url):
    assert bot._youtube_id(url) == ''


def run(coro):
    return asyncio.run(coro)


def test_same_trailer_from_another_site_is_a_duplicate(hashes):
    first = {'title': 'Frieren season 2 trailer revealed', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    assert run(bot._video_duplicate(first)) is None
    bot._commit_image_fingerprint(first)
    second = {'title': 'New Frieren trailer is out',
              'video': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}
    assert run(bot._video_duplicate(second)) == 'Frieren season 2 trailer revealed'


@pytest.mark.asyncio
async def test_failed_send_frees_the_video(hashes):
    # Та же задача: бронь не освобождается сама по завершении владельца.
    first = {'title': 'Frieren trailer', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    await bot._video_duplicate(first)
    second = {'title': 'Frieren trailer again', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    assert await bot._video_duplicate(dict(second)) is not None    # пока занято
    bot._release_publish_reservations(first)
    assert await bot._video_duplicate(second) is None


def test_another_event_with_the_old_trailer_inside_is_news(hashes):
    first = {'title': 'Frieren season 2 trailer revealed', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    run(bot._video_duplicate(first))
    bot._commit_image_fingerprint(first)
    later = {'title': 'Frieren season 2 delayed to 2027', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    assert run(bot._video_duplicate(later)) is None


def test_other_videos_and_switch(hashes, monkeypatch):
    first = {'title': 'A trailer', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    run(bot._video_duplicate(first))
    bot._commit_image_fingerprint(first)
    other = {'title': 'A trailer', 'video': 'https://youtu.be/aaaaaaaaaaa'}
    assert run(bot._video_duplicate(other)) is None
    monkeypatch.setattr(bot, 'settings', NS(image_dedup=False))
    same = {'title': 'A trailer', 'video': 'https://youtu.be/dQw4w9WgXcQ'}
    assert run(bot._video_duplicate(same)) is None


def test_video_id_never_matches_a_picture(hashes):
    # Префиксы разные — расстояние не считается, картинка не «совпадёт» с роликом.
    assert bot._hash_distance('v:dQw4w9WgXcQ', 'd:0000000000000000') is None
    assert bot._hash_distance('v:dQw4w9WgXcQ', 'v:dQw4w9WgXcR') == 64


def test_publication_path_checks_the_video():
    # Проверка встроена в подготовку поста сразу после дедупа по картинке.
    import inspect
    source = inspect.getsource(bot._prepare_news_for_send)
    assert source.index('await _image_duplicate(news)') < source.index('await _video_duplicate(news)')


# ------------------------------------- флуд, который не удалось удалить

@pytest.fixture
def chat(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from conftest import with_media_senders
    import moderation_media as media
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
    scan = AsyncMock(return_value=media.Scan('checked', 'nsfw', 'Обнажённое тело', 20, .97, hits=12))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scan))
    tg = with_media_senders(NS(
        get_chat_member=AsyncMock(return_value=NS(status='member')),
        delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
        send_message=AsyncMock(return_value=NS(message_id=900)),
        edit_message_text=AsyncMock()))
    return NS(store=store, tg=tg, scan=scan)


def photo(number):
    return NS(chat_id=-100, message_id=number, text=None, caption=f'подпись {number}',
              sender_chat=None, reply_to_message=None, media_group_id=None,
              message_thread_id=None, has_media_spoiler=False, link=None,
              from_user=NS(id=51, full_name='Участник', is_bot=False),
              photo=[NS(file_unique_id=f'p{number}', file_id=f'f{number}', file_size=1000,
                        width=100, height=100)])


async def send(chat, message):
    await bot.moderation_message_handler(
        NS(effective_message=message, effective_user=message.from_user,
           effective_chat=NS(id=-100), edited_message=None), NS(bot=chat.tg))


@pytest.mark.asyncio
async def test_flood_message_that_stayed_in_chat_is_still_checked(chat):
    from telegram.error import BadRequest
    chat.scan.return_value = NS(status='checked', category='', reason='', score=0.0, hits=0,
                                frames=1, hashes=(), near_explicit=0, retry_after=0)
    for number in range(1, 5):                       # запас темпа исчерпан
        await send(chat, photo(number))
    chat.scan.reset_mock()
    import moderation_media as media
    chat.scan.return_value = media.Scan('checked', 'nsfw', 'Обнажённое тело', 20, .97, hits=12)
    chat.tg.delete_message.side_effect = BadRequest("Message can't be deleted")
    await send(chat, photo(5))
    chat.scan.assert_awaited()                        # раньше — return без проверки
    assert any(row.get('category') == 'nsfw' for row in chat.store.recent_log(10))


@pytest.mark.asyncio
async def test_removed_flood_message_is_not_scanned_again(chat):
    chat.scan.return_value = NS(status='checked', category='', reason='', score=0.0, hits=0,
                                frames=1, hashes=(), near_explicit=0, retry_after=0)
    for number in range(1, 5):
        await send(chat, photo(number))
    chat.scan.reset_mock()
    await send(chat, photo(5))
    chat.scan.assert_not_awaited()
    assert 5 in [c.args[1] for c in chat.tg.delete_message.await_args_list]


# ------------------------------------- остановка бота

@pytest.mark.asyncio
async def test_stop_removes_pause_notices_and_cancels_retries(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bot, '_moderation_rate_tasks', set())
    monkeypatch.setattr(bot, 'MODERATION_RATE_PAUSE_SEC', 60)
    tg = NS(send_message=AsyncMock(return_value=NS(message_id=901)),
            delete_message=AsyncMock(return_value=True))
    await bot._mod_rate_notice(tg, NS(chat_id=-100, message_thread_id=None), 51, 'Участник')
    retried = []

    async def retry():
        await asyncio.sleep(600)
        retried.append(1)
    task = asyncio.get_running_loop().create_task(retry())
    bot._moderation_rate_tasks.add(task)
    await asyncio.sleep(0)
    assert await bot._cancel_moderation_tasks(timeout=2) == 2
    tg.delete_message.assert_awaited_once_with(-100, 901)    # уведомление убрано сразу
    assert task.cancelled() and retried == []


@pytest.mark.asyncio
async def test_post_stop_is_wired_and_never_raises(monkeypatch):
    async def boom(timeout=5.0):
        raise RuntimeError('x')
    monkeypatch.setattr(bot, '_cancel_moderation_tasks', boom)
    await bot._post_stop(None)
    import inspect
    assert '.post_stop(_post_stop)' in inspect.getsource(bot.main)


# ------------------------------------- «✅ Верно» по уже удалённому сообщению

def test_gone_message_is_recognised():
    from telegram.error import BadRequest, TimedOut
    assert bot._tg_message_gone(BadRequest('Message to delete not found'))
    assert not bot._tg_message_gone(BadRequest("Message can't be deleted"))
    assert not bot._tg_message_gone(TimedOut())


@pytest.mark.asyncio
@pytest.mark.parametrize('anonymous', [False, True])
async def test_enforcing_on_an_already_deleted_message(chat, anonymous):
    from telegram.error import BadRequest
    chat.tg.delete_message.side_effect = BadRequest('Message to delete not found')
    message = photo(1)
    if anonymous:
        message.from_user, message.sender_chat = None, NS(id=-555, title='Канал')
    result = await bot._mod_enforce(chat.tg, message, 'nsfw', 'подтверждено', 'админ', report=False)
    assert 'сообщение уже удалено' in result and 'не подтверждено' not in result
    row = chat.store.recent_log(1)[0]
    assert row['action'] != 'failed'
    if not anonymous:
        assert chat.store.warn_count(-100, 51) == 1      # нарушение было — варн остаётся


@pytest.mark.asyncio
async def test_refused_delete_is_still_reported_as_a_failure(chat):
    from telegram.error import BadRequest
    chat.tg.delete_message.side_effect = BadRequest("Message can't be deleted")
    result = await bot._mod_enforce(chat.tg, photo(1), 'nsfw', 'x', 'админ', report=False)
    assert 'удаление не подтверждено' in result


# ------------------------------------- осуждённый file_id не вытесняется копиями

def test_condemned_file_survives_many_copies(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('e1', -100, 'nsfw', ['orig'], ['d' * 16], 'admin')
    for i in range(50):
        store.note_blocked_hit('e1', f'copy{i}')
    assert store.find_blocked_media('orig', chat_id=-100)            # исходник узнаётся точно
    assert store.find_blocked_media('copy49', chat_id=-100)          # свежая копия тоже
    assert not store.find_blocked_media('copy0', chat_id=-100)       # старые копии вытеснены
    row = store._data['media_blocklist']['e1']
    assert len(row['file_ids']) == 1 + bot.MODERATION_MEDIA_COPY_IDS


def test_admin_confirmed_copies_are_pinned_too(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('e1', -100, 'nsfw', ['orig'], ['d' * 16], 'auto')
    store.note_blocked_hit('e1', 'copyA')
    # Админ подтвердил отчёт о копии — её файл осуждён напрямую.
    store.block_media('e1', -100, 'nsfw', ['copyB'], [], 'admin')
    for i in range(50):
        store.note_blocked_hit('e1', f'copy{i}')
    assert store.find_blocked_media('orig', chat_id=-100)
    assert store.find_blocked_media('copyB', chat_id=-100)
    assert not store.find_blocked_media('copyA', chat_id=-100)


def test_old_rows_keep_their_first_file(tmp_path):
    # Записи, созданные до появления закрепления (без поля pinned): первый id — исходник.
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('e1', -100, 'nsfw', ['orig'], ['d' * 16], 'admin')
    store._data['media_blocklist']['e1'].pop('pinned')
    for i in range(50):
        store.note_blocked_hit('e1', f'copy{i}')
    assert store.find_blocked_media('orig', chat_id=-100)


# ------------------------------------- часы Telegram и «вечный» мут

from datetime import datetime, timedelta, timezone  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(bot, '_tg_clock_samples', bot.deque(maxlen=50))
    return bot._tg_clock_samples


def test_offset_is_learned_from_message_dates(clock):
    now = datetime.now(timezone.utc)
    # Часы Telegram на 40 с впереди; сообщения доходят с задержкой 1–5 с.
    for delay in (5, 1, 3):
        bot._note_tg_clock(NS(date=now + timedelta(seconds=40 - delay), edit_date=None))
    assert 38 <= (bot._tg_now() - datetime.now(timezone.utc)).total_seconds() <= 40


def test_edits_and_garbage_are_not_samples(clock):
    now = datetime.now(timezone.utc)
    bot._note_tg_clock(NS(date=now + timedelta(seconds=40), edit_date=now))
    bot._note_tg_clock(NS(date=now + timedelta(days=3), edit_date=None))
    bot._note_tg_clock(NS(date=None, edit_date=None))
    bot._note_tg_clock(NS())
    assert list(clock) == []


def test_restriction_is_never_shorter_than_the_safe_minimum(clock):
    # Часы Telegram впереди на 40 с: минутная пауза по нашим часам — это
    # 20 с по часам Telegram, то есть бессрочный мут.
    clock.append(40.0)
    until = bot._tg_until(60)
    remaining_at_telegram = (until - (datetime.now(timezone.utc) + timedelta(seconds=40))).total_seconds()
    assert 58 <= remaining_at_telegram <= 60
    tiny = bot._tg_until(5)
    assert (tiny - bot._tg_now()).total_seconds() >= bot.TG_RESTRICT_MIN_SEC - 1


@pytest.mark.asyncio
async def test_flood_pause_uses_telegram_time(chat, clock, monkeypatch):
    clock.append(40.0)
    await bot._mod_rate_pause(chat.tg, photo(1), 51, 'Участник', 'текст')
    until = chat.tg.restrict_chat_member.await_args.kwargs['until_date']
    lead = (until - datetime.now(timezone.utc)).total_seconds()
    assert lead >= bot.MODERATION_RATE_PAUSE_SEC + 38


@pytest.mark.asyncio
async def test_handler_samples_new_messages_only(chat, clock):
    message = photo(1)
    message.date = datetime.now(timezone.utc) + timedelta(seconds=30)
    message.edit_date = None
    await send(chat, message)
    assert len(clock) == 1
    await bot.moderation_message_handler(
        NS(effective_message=message, effective_user=message.from_user,
           effective_chat=NS(id=-100), edited_message=message), NS(bot=chat.tg))
    assert len(clock) == 1



@pytest.mark.asyncio
async def test_same_burst_refused_delete_lets_checks_continue(chat):
    from telegram.error import BadRequest
    chat.tg.delete_message.side_effect = BadRequest("Message can't be deleted")
    outcome = {}
    await bot._mod_rate_pause(chat.tg, photo(1), 51, 'Участник', 'т', state='inflight', outcome=outcome)
    assert outcome == {'removed': False}
    chat.tg.delete_message.side_effect = None
    await bot._mod_rate_pause(chat.tg, photo(2), 51, 'Участник', 'т', state='inflight', outcome=outcome)
    assert outcome == {'removed': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('left, restored', [(20, False), (3600, True)])
async def test_undo_does_not_turn_a_short_previous_mute_into_a_permanent_one(chat, monkeypatch,
                                                                             left, restored):
    from unittest.mock import AsyncMock
    from telegram import ChatPermissions
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    now = int(datetime.now(timezone.utc).timestamp())
    store = chat.store
    store.reserve_incident('old', -100, 51, 'spam', 'mute')
    store.update_incident('old', status='confirmed', add_warning=True, mute_until=now + left)
    store.reserve_incident('new', -100, 51, 'spam', 'mute')
    store.update_incident('new', status='confirmed', add_warning=True, mute_until=now + 7200,
                          previous_mute_id='old')
    chat.tg.get_chat_member = AsyncMock(return_value=NS(status='restricted', until_date=now + 7200))
    chat.tg.get_chat = AsyncMock(return_value=NS(permissions=ChatPermissions.all_permissions()))
    chat.tg.restrict_chat_member = AsyncMock(return_value=True)
    query = NS(data='mod:undo:-100:51:new', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=chat.tg))
    kwargs = chat.tg.restrict_chat_member.await_args.kwargs
    if restored:
        assert kwargs['until_date'] == now + left               # прежний мут продолжен
    else:
        assert kwargs['until_date'] == 0                        # остаток меньше минуты — снят
        assert kwargs['permissions'] == ChatPermissions.all_permissions()
