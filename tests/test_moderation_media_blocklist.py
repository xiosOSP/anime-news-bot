"""Чёрный список медиа: одна и та же гифка не может получить разные исходы.

Живой случай: откровенная гифка в чате, оценка детектора у порога. Одну копию
бот удалил, другую оставил. «✅ Верно» в отчёте только записывало оценку —
гифка оставалась в чате, а её копии бот не узнавал.
"""
import asyncio
import io
import json
import random
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

import anime_news_bot as bot
from conftest import with_media_senders
import moderation_media as media


def picture(seed, size=(320, 180)):
    """Картинка с рисунком: цветные прямоугольники по зерну."""
    rng = random.Random(seed)
    image = Image.new('RGB', size, (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    draw = ImageDraw.Draw(image)
    for _ in range(14):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        draw.rectangle((x, y, x + rng.randrange(20, 140), y + rng.randrange(20, 90)),
                       fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    return image


def reencoded(image, quality=30, scale=.5):
    small = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = io.BytesIO()
    small.save(buffer, 'JPEG', quality=quality)
    return Image.open(io.BytesIO(buffer.getvalue())).convert('RGB')


def hashes_of(*images):
    return [media.frame_hash(image) for image in images]


# --------------------------------------------------------------- отпечатки

def test_fingerprint_survives_reencoding_but_not_other_pictures():
    base = media.frame_hash(picture(1))
    assert base and len(base) == 64
    copy = media.frame_hash(reencoded(picture(1)))
    assert media.hashes_match([copy], [base])
    others = [media.frame_hash(picture(seed)) for seed in range(2, 40)]
    assert not any(media.hashes_match([other], [base]) for other in others)


@pytest.mark.parametrize('image', [
    Image.new('RGB', (200, 120), 'black'),
    Image.new('RGB', (200, 120), (200, 30, 30)),
    Image.linear_gradient('L').resize((200, 120)).convert('RGB'),
])
def test_frames_without_a_picture_have_no_fingerprint(image):
    # Затемнение в чёрный или заливка «совпали» бы с любой другой гифкой.
    assert media.frame_hash(image) is None


def test_animation_needs_two_close_frames():
    frames = [picture(seed) for seed in (10, 11, 12, 13)]
    known = hashes_of(*frames)
    one_shared = hashes_of(reencoded(frames[0]), picture(50), picture(51), picture(52))
    two_shared = hashes_of(reencoded(frames[0]), reencoded(frames[2]), picture(51), picture(52))
    assert not media.hashes_match(one_shared, known)
    assert media.hashes_match(two_shared, known)
    # Картинка против гифки не совпадает по одному кадру: безобидная сцена из
    # осуждённой нарезки не должна делать запрещённым свой скриншот.
    assert not media.hashes_match(hashes_of(reencoded(frames[1])), known)
    assert media.hashes_match(hashes_of(reencoded(frames[1])), hashes_of(frames[1]))
    assert media.hashes_match(two_shared, [int(h, 16) for h in known])
    assert not media.hashes_match([], known)
    assert not media.hashes_match(['не-хэш'], known)


def test_scan_reports_fingerprints_of_checked_frames(tmp_path, monkeypatch):
    path = tmp_path / 'anim.gif'
    Image.new('RGB', (8, 8)).save(path)
    frames = [picture(20), Image.new('RGB', (64, 64), 'black'), picture(21)]
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    monkeypatch.setattr(media, '_pillow_frames', lambda _path: frames)
    monkeypatch.setattr(media, 'scan_frame', lambda *_a: media.Scan('checked'))
    scan = media.scan_file(path, 'animation', .80, .85)
    assert scan.status == 'checked' and scan.frames == 3
    assert list(scan.hashes) == [media.frame_hash(frames[0]), media.frame_hash(frames[2])]


def test_borderline_frame_is_marked_and_scored():
    class Detector:
        def detect(self, _image):
            return [{'class': 'BUTTOCKS_EXPOSED', 'score': .80}]
    result = media.scan_frame(Detector(), picture(3), .80, .85)
    assert (result.status, result.borderline, result.score) == ('unchecked', True, .80)


def test_worker_output_keeps_fingerprints(monkeypatch):
    payload = json.dumps({'status': 'checked', 'hashes': ['ab' * 32, 'cd' * 32]})
    monkeypatch.setattr(media, '_invoke_worker', lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout=payload, stderr=''))
    scan = media.run_worker('x', 'image', 5, .8, .85)
    assert scan.status == 'checked' and scan.hashes == ('ab' * 32, 'cd' * 32)


def test_borderline_is_not_a_detector_failure():
    scanner = media.MediaScanner()
    for _ in range(scanner.failure_limit + 1):
        scanner._note_worker_result(media.Scan('unchecked', reason='x', borderline=True))
    assert scanner.paused_for() == 0


# --------------------------------------------------------------- хранилище

def test_blocklist_store_roundtrip(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    first = hashes_of(picture(30))
    entry = store.block_media('r1', -100, 'spoiler_16', ['fid1'], first, 'auto')
    assert entry['id'] == 'r1' and entry['source'] == 'auto'
    assert store.find_blocked_media('fid1')['id'] == 'r1'
    assert store.find_blocked_media(hashes=hashes_of(reencoded(picture(30))))['id'] == 'r1'
    assert store.find_blocked_media('other', hashes_of(picture(31))) == {}
    # Человек подтвердил то, что бот удалил сам: запись становится «админской».
    assert store.block_media('r1', -100, 'spoiler_16', ['fid2'], (), 'admin')['source'] == 'admin'
    store.note_blocked_hit('r1', 'fid3')
    # Отпечатки записи — только исходные: и повторное внесение, и найденные
    # копии их не расширяют, иначе запись «расползалась» бы на чужие кадры.
    store.block_media('r1', -100, 'spoiler_16', [], hashes_of(picture(33)), 'admin')
    assert store.find_blocked_media(hashes=hashes_of(picture(33))) == {}
    store.flush()
    reloaded = bot.ChatModerationStore(tmp_path / 'mod.json')
    row = reloaded.find_blocked_media('fid3')
    assert row['file_ids'] == ['fid1', 'fid2', 'fid3'] and row['hits'] == 1
    assert row['hashes'] == first
    assert reloaded.blocked_media_count() == 1
    assert reloaded.unblock_media('r1') and not reloaded.find_blocked_media('fid1')
    assert not reloaded.unblock_media('r1')
    assert store.block_media('r2', -100, 'nsfw', [], [], 'admin') == {}


def test_blocklist_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_BLOCKLIST_MAX', 50)
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    for index in range(60):
        store.block_media(f'r{index}', -100, 'nsfw', [f'f{index}'], (), 'auto')
    assert store.blocked_media_count() == 50
    assert not store.find_blocked_media('f0') and store.find_blocked_media('f59')


def test_detector_verdict_below_category_threshold_always_needs_a_human():
    state = bot._mod_decision_state('spoiler_16', bot.MODERATION_MEDIA_SOURCE,
                                    confidence=.97, needs_review=True)
    assert state[0] == 'review'
    assert bot._mod_decision_state('spoiler_16', bot.MODERATION_MEDIA_SOURCE,
                                   confidence=.97)[0] == 'auto'


# --------------------------------------------------------------- обработчик

@pytest.fixture
def chat(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports', '_moderation_recent_media',
                 '_moderation_report_bursts'):
        monkeypatch.setattr(bot, name, {})
    tg = with_media_senders(NS(
        get_chat_member=AsyncMock(return_value=NS(status='member')),
        delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
        send_message=AsyncMock(return_value=NS(message_id=900)),
        edit_message_text=AsyncMock()))
    scanner = AsyncMock()
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    unchecked = AsyncMock()
    monkeypatch.setattr(bot, '_mod_media_unchecked', unchecked)
    return NS(store=store, tg=tg, scanner=scanner, unchecked=unchecked)


def gif(number, user, file_id, spoiler=False):
    return NS(chat_id=-100, message_id=number, text=None, caption=None, sender_chat=None,
              reply_to_message=None, media_group_id=None, has_media_spoiler=spoiler,
              message_thread_id=None, link=None,
              from_user=NS(id=user, full_name=f'Участник {user}', is_bot=False),
              animation=NS(file_unique_id=file_id, file_id='f' + file_id, mime_type='video/mp4'))


async def send(chat, message, edited=False):
    await bot.moderation_message_handler(
        NS(effective_message=message, effective_user=message.from_user,
           effective_chat=NS(id=-100), edited_message=message if edited else None), NS(bot=chat.tg))


def deleted(chat):
    return [call.args[1] for call in chat.tg.delete_message.await_args_list]


def review_ids(chat):
    ids = []
    for call in chat.tg.send_message.await_args_list:
        markup = call.kwargs.get('reply_markup')
        for row in getattr(markup, 'inline_keyboard', ()) or ():
            for button in row:
                if button.callback_data.startswith('mod:ok:'):
                    ids.append(button.callback_data)
    return ids


async def press(chat, data):
    query = NS(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
               message=NS(chat_id=7), from_user=NS(id=7))
    before = len(chat.tg.send_message.await_args_list)
    await bot.moderation_callback(NS(callback_query=query), NS(bot=chat.tg))
    # Ответ на кнопку — сразу и коротко; итог — отдельным письмом админу.
    query.result = '\n'.join(str(call.args[1]) for call in chat.tg.send_message.await_args_list[before:]
                             if call.args[0] == 7)
    return query


GIF_FRAMES = [picture(seed) for seed in (60, 61, 62, 63)]
GIF_HASHES = tuple(hashes_of(*GIF_FRAMES))
COPY_HASHES = tuple(hashes_of(*(reencoded(frame) for frame in GIF_FRAMES)))
REASON = 'Пограничная оценка наготы; нужна ручная проверка'


def borderline(hashes=GIF_HASHES):
    return media.Scan('unchecked', reason=REASON, frames=4, score=.80,
                      borderline=True, hashes=hashes)


@pytest.mark.asyncio
async def test_borderline_gif_gets_a_report_with_buttons_not_a_hidden_notice(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    chat.unchecked.assert_not_awaited()
    assert deleted(chat) == []
    assert len(review_ids(chat)) == 1
    report = chat.tg.send_message.await_args_list[0].args[1]
    assert 'ручная оценка' in report and '0.80' in report
    # В отчёте — сама гифка под спойлером, а не только её ID.
    assert chat.tg.media_sent == [('send_animation', 7, 'fg1', True)]


@pytest.mark.asyncio
async def test_copies_of_media_awaiting_review_do_not_bring_new_letters(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    chat.scanner.return_value = borderline(COPY_HASHES)   # пересжатая копия
    await send(chat, gif(2, 52, 'g2'))
    await send(chat, gif(3, 53, 'g1'))                     # та же гифка ещё раз
    assert len(review_ids(chat)) == 1
    assert [row['action'] for row in chat.store.recent_log(2)] == ['ждёт оценки (копия)'] * 2
    # Другая гифка — отдельный отчёт.
    chat.scanner.return_value = borderline(tuple(hashes_of(*(picture(s) for s in range(80, 84)))))
    await send(chat, gif(4, 54, 'g4'))
    assert len(review_ids(chat)) == 2
    # «✅ Верно» на первом отчёте убирает и копии.
    await press(chat, review_ids(chat)[0])
    assert sorted(deleted(chat)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_after_wrong_the_same_media_is_not_asked_about_again(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    data = review_ids(chat)[0]
    assert data.rsplit(':', 1)[1] in bot._moderation_pending_media
    query = await press(chat, data.replace('mod:ok:', 'mod:wrong:'))
    assert 'больше спрашивать не буду' in query.result
    assert data.rsplit(':', 1)[1] not in bot._moderation_pending_media
    chat.scanner.return_value = borderline(COPY_HASHES)
    await send(chat, gif(2, 52, 'g2'))
    assert len(review_ids(chat)) == 1
    assert chat.store.recent_log(1)[0]['action'] == 'разрешено админом'
    # Разрешение переживает перезапуск.
    chat.store.flush()
    assert bot.ChatModerationStore(chat.store.path).is_allowed_media('g1')


@pytest.mark.asyncio
async def test_copies_from_the_blocklist_are_removed_silently(chat):
    chat.store.block_media('orig', -100, 'spoiler_16', ['g1'], GIF_HASHES, 'admin')
    await send(chat, gif(1, 51, 'g1'))
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=COPY_HASHES)
    await send(chat, gif(2, 52, 'g2'))
    assert deleted(chat) == [1, 2]
    admin_letters = [c for c in chat.tg.send_message.await_args_list if c.args[0] == 7]
    assert admin_letters == [] and chat.tg.media_sent == []
    assert chat.store.find_blocked_media('g1')['hits'] == 2
    assert chat.store.blocked_media_hits() == 2
    assert 'Чёрный список медиа: 1 (копий удалено молча: 2)' in bot._moderation_stats_text()


@pytest.mark.asyncio
async def test_confirming_a_review_removes_the_gif_its_copies_and_future_copies(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    chat.scanner.return_value = borderline(COPY_HASHES)
    await send(chat, gif(2, 52, 'g2'))          # та же гифка, другая кодировка
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=tuple(hashes_of(
        *(picture(seed) for seed in (70, 71, 72, 73)))))
    await send(chat, gif(3, 53, 'g3'))          # чужая гифка
    assert deleted(chat) == []

    query = await press(chat, review_ids(chat)[0])
    assert sorted(deleted(chat)) == [1, 2]
    # Подтверждённое сообщение получает санкцию; копия, найденная только по
    # сходству кадров, — лишь удаляется (у похожих шаблонов отпечаток общий).
    assert chat.store.warn_count(-100, 51) == 1 and chat.store.warn_count(-100, 52) == 0
    assert chat.store.warn_count(-100, 53) == 0
    assert query.answer.await_args.args[0] == 'Записал: решение верное'
    assert 'чёрном списке' in query.result
    chat.tg.ban_chat_member.assert_not_awaited()
    chat.tg.restrict_chat_member.assert_not_awaited()

    # Следующая отправка того же file_id удаляется без детектора.
    chat.scanner.reset_mock()
    await send(chat, gif(4, 54, 'g1'))
    chat.scanner.assert_not_awaited()
    assert deleted(chat)[-1] == 4
    # Новая загрузка той же гифки узнаётся по отпечаткам, даже «чистая» по оценке.
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=COPY_HASHES)
    await send(chat, gif(5, 55, 'g5'))
    assert deleted(chat)[-1] == 5
    # Найдена только по сходству кадров: file_id в записи не добавляется, а
    # автор не получает санкции — у похожего шаблона отпечаток общий.
    assert not chat.store.find_blocked_media('g5') and chat.store.warn_count(-100, 55) == 0


@pytest.mark.asyncio
async def test_confirmation_after_the_message_left_memory_still_deletes_it(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    bot._moderation_recent_media.clear()
    await press(chat, review_ids(chat)[0])
    assert deleted(chat) == [1]
    assert chat.store.warn_count(-100, 51) == 1


@pytest.mark.asyncio
async def test_automatic_deletion_catches_copies_already_in_chat(chat):
    chat.scanner.return_value = borderline(COPY_HASHES)
    await send(chat, gif(1, 51, 'g1'))          # копия с оценкой ниже порога
    chat.scanner.return_value = media.Scan(
        'checked', 'spoiler_16', 'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED',
        4, .85, hits=4, hashes=GIF_HASHES)
    await send(chat, gif(2, 52, 'g2'))
    assert sorted(deleted(chat)) == [1, 2]
    entry = chat.store.find_blocked_media('g2')
    assert entry['source'] == 'auto'


@pytest.mark.asyncio
async def test_wrong_and_undo_take_media_out_of_the_blocklist(chat):
    chat.scanner.return_value = media.Scan(
        'checked', 'spoiler_16', 'x', 4, .85, hits=4, hashes=GIF_HASHES)
    await send(chat, gif(1, 51, 'g1'))
    assert chat.store.find_blocked_media('g1')
    undo = next(button.callback_data
                for call in chat.tg.send_message.await_args_list
                for row in getattr(call.kwargs.get('reply_markup'), 'inline_keyboard', ())
                for button in row if button.callback_data.startswith('mod:undo:'))
    await press(chat, undo)
    assert not chat.store.find_blocked_media('g1')

    chat.tg.send_message.reset_mock()
    await send(chat, gif(2, 52, 'g2'))
    assert chat.store.find_blocked_media('g2')
    query = await press(chat, review_ids(chat)[0].replace('mod:ok:', 'mod:wrong:'))
    assert not chat.store.find_blocked_media('g2')
    assert 'убрано из чёрного списка' in query.result


@pytest.mark.asyncio
async def test_spoilered_copy_of_16_plus_gif_is_allowed(chat):
    chat.store.block_media('r', -100, 'spoiler_16', ['g1'], GIF_HASHES, 'admin')
    await send(chat, gif(1, 51, 'g1', spoiler=True))
    assert deleted(chat) == []
    await send(chat, gif(2, 52, 'g1'))
    assert deleted(chat) == [2]


@pytest.mark.asyncio
async def test_observe_mode_neither_blocks_nor_sweeps(chat):
    chat.store.set_mode('observe')
    chat.scanner.return_value = borderline(COPY_HASHES)
    await send(chat, gif(1, 51, 'g1'))
    chat.scanner.return_value = media.Scan('checked', 'spoiler_16', 'x', 4, .85, hits=4,
                                           hashes=GIF_HASHES)
    await send(chat, gif(2, 52, 'g2'))
    assert deleted(chat) == []
    assert chat.store.blocked_media_count() == 0


@pytest.mark.asyncio
async def test_text_reviews_never_touch_the_blocklist(chat):
    chat.store.ensure_review('t1', -100, 51, 'toxic', 'warn', 'модель', 'x', 'текст')
    await press(chat, 'mod:ok:-100:51:t1')
    assert chat.store.blocked_media_count() == 0
    assert deleted(chat) == []


@pytest.mark.asyncio
async def test_second_confirmation_does_not_touch_already_removed_copies(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    chat.scanner.return_value = borderline(COPY_HASHES)
    bot._moderation_pending_media.clear()   # второй отчёт: будто первый уже устарел
    await send(chat, gif(2, 52, 'g2'))
    first, second = review_ids(chat)
    await press(chat, first)
    assert sorted(deleted(chat)) == [1, 2]
    await press(chat, second)
    assert sorted(deleted(chat)) == [1, 2]
    assert chat.store.warn_count(-100, 52) == 0


@pytest.mark.asyncio
async def test_sweep_spares_a_spoilered_copy_of_16_plus(chat):
    chat.scanner.return_value = borderline(COPY_HASHES)
    await send(chat, gif(1, 51, 'g1', spoiler=True))
    chat.scanner.return_value = media.Scan('checked', 'spoiler_16', 'x', 4, .85, hits=4,
                                           hashes=GIF_HASHES)
    await send(chat, gif(2, 52, 'g2'))
    assert deleted(chat) == [2]


@pytest.mark.asyncio
async def test_confirming_a_text_review_applies_the_sanction(chat, monkeypatch):
    # Модель не уверена (0.80 при автопороге 0.95): санкции нет, отчёт админу.
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, '_mod_local_check', lambda *a, **k: dict(
        category='toxic', confident=False, severity=2, reason='подозрение'))
    monkeypatch.setattr(bot, '_moderation_classify', AsyncMock(return_value=dict(
        violation=True, category='toxic', severity=2, confidence=.80, needs_review=False,
        reason='оскорбление участника')))
    message = NS(chat_id=-100, message_id=9, text='текст с оскорблением', caption=None,
                 sender_chat=None, reply_to_message=None, media_group_id=None,
                 message_thread_id=None, has_media_spoiler=False, link=None,
                 from_user=NS(id=61, full_name='Участник', is_bot=False))
    await send(chat, message)
    assert deleted(chat) == [] and chat.store.warn_count(-100, 61) == 0
    [data] = review_ids(chat)

    query = await press(chat, data)
    assert deleted(chat) == [9]
    assert chat.store.warn_count(-100, 61) == 1
    assert 'Сообщение: сообщение удалено' in query.result
    assert chat.store.blocked_media_count() == 0
    chat.tg.ban_chat_member.assert_not_awaited()
    # Повторное нажатие ничего не делает: оценка уже записана.
    await press(chat, data)
    assert deleted(chat) == [9] and chat.store.warn_count(-100, 61) == 1


@pytest.mark.asyncio
async def test_confirming_an_automatic_verdict_does_not_punish_twice(chat):
    chat.scanner.return_value = media.Scan('checked', 'spoiler_16', 'x', 4, .85, hits=4,
                                           hashes=GIF_HASHES)
    await send(chat, gif(1, 51, 'g1'))
    assert deleted(chat) == [1] and chat.store.warn_count(-100, 51) == 1
    await press(chat, review_ids(chat)[0])
    assert deleted(chat) == [1] and chat.store.warn_count(-100, 51) == 1
    assert chat.store.find_blocked_media('g1')['source'] == 'admin'


@pytest.mark.asyncio
async def test_confirmation_never_invents_a_sanction_the_rules_do_not_give(chat):
    # Одиночное принижение — не нарушение: подтверждение не должно наказывать.
    stub = bot._mod_message_stub(-100, 61, 9)
    assert await bot._mod_enforce(chat.tg, stub, 'belittling', 'x', 'модель') == ''
    assert await bot._mod_enforce(chat.tg, stub, 'нет-такой', 'x', 'модель') == ''
    assert deleted(chat) == []


def test_entry_keeps_at_most_sixteen_fingerprints(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    many = [f'{index:064x}' for index in range(1, 40)]
    assert len(store.block_media('r', -100, 'nsfw', [], many, 'auto')['hashes']) == 16


def test_raid_of_automatic_entries_does_not_evict_admin_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_BLOCKLIST_MAX', 50)
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('admin-1', -100, 'nsfw', ['keep'], (), 'admin')
    for index in range(80):
        store.block_media(f'a{index}', -100, 'nsfw', [f'f{index}'], (), 'auto')
    assert store.find_blocked_media('keep')['id'] == 'admin-1'


def test_broken_blocklist_rows_are_dropped_on_load(tmp_path):
    path = tmp_path / 'mod.json'
    path.write_text(json.dumps({'media_blocklist': {'bad': 'строка', 'ok': {
        'file_ids': ['x'], 'hashes': [], 'category': 'nsfw'}}}), encoding='utf-8')
    store = bot.ChatModerationStore(path)
    assert store.blocked_media_count() == 1 and store.find_blocked_media('x')['id'] == 'ok'


def test_unblock_removes_every_entry_that_recognises_the_media(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('a', -100, 'nsfw', ['g1'], GIF_HASHES, 'auto')
    store.block_media('b', -100, 'nsfw', ['g2'], COPY_HASHES, 'auto')
    store.block_media('c', -100, 'nsfw', ['other'], (), 'auto')
    assert store.unblock_media('nope', ['g9'], GIF_HASHES) == 2
    assert store.blocked_media_count() == 1 and store.find_blocked_media('other')


@pytest.mark.asyncio
async def test_every_pending_review_gets_buttons_even_from_the_same_sender(chat, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_RATE_MESSAGES', 0)  # пять гифок подряд — не про темп
    chat.scanner.return_value = borderline()
    for number in range(1, 6):
        chat.scanner.return_value = borderline(tuple(hashes_of(
            *(picture(100 * number + seed) for seed in range(4)))))
        await send(chat, gif(number, 51, f'g{number}'))
    assert len(review_ids(chat)) == 5


@pytest.mark.asyncio
async def test_copy_condemned_while_waiting_for_the_detector_is_still_removed(chat):
    async def slow_check(_bot, message):
        # Пока эта копия ждала очередь, другую осудили и внесли в список.
        chat.store.block_media('orig', -100, 'spoiler_16', ['g1'], GIF_HASHES, 'admin')
        return media.Scan('unchecked', reason='Очередь локальной проверки переполнена')
    chat.scanner.side_effect = slow_check
    await send(chat, gif(1, 51, 'g1'))
    assert deleted(chat) == [1]
    chat.unchecked.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_after_the_file_was_replaced_does_not_punish_the_new_one(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    # Автор подменил файл в том же сообщении.
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=tuple(hashes_of(
        *(picture(seed) for seed in (90, 91, 92, 93)))))
    await send(chat, gif(1, 51, 'gNEW'), edited=True)
    await press(chat, review_ids(chat)[0])
    assert deleted(chat) == [] and chat.store.warn_count(-100, 51) == 0
    # Осуждённый файл при этом всё равно в чёрном списке.
    assert chat.store.find_blocked_media('g1')['source'] == 'admin'
    assert not chat.store.find_blocked_media('gNEW')


@pytest.mark.asyncio
async def test_confirmation_after_a_caption_only_edit_still_acts_on_the_file(chat):
    chat.scanner.return_value = borderline()
    await send(chat, gif(1, 51, 'g1'))
    edited = gif(1, 51, 'g1')
    edited.caption = 'исправил подпись'
    await send(chat, edited, edited=True)
    await press(chat, review_ids(chat)[0])
    assert deleted(chat) == [1] and chat.store.warn_count(-100, 51) == 1
    assert chat.store.find_blocked_media('g1')['source'] == 'admin'

    # Отчёт о тексте после правки текста по-прежнему не наказывает.
    chat.store.ensure_review('t9', -100, 61, 'toxic', 'warn', 'модель', 'x', 'текст', media=dict(
        message_id=9, pending=True, version='старая-версия', severity=2))
    bot._moderation_latest[(-100, 9)] = {'repeat_key': 'другая-версия'}
    query = await press(chat, 'mod:ok:-100:61:t9')
    assert 'изменено после отчёта' in query.result and chat.store.warn_count(-100, 61) == 0


@pytest.mark.asyncio
async def test_failed_deletion_does_not_feed_the_blocklist(chat):
    from telegram.error import BadRequest
    chat.tg.delete_message.side_effect = BadRequest("Message can't be deleted")
    chat.scanner.return_value = media.Scan('checked', 'spoiler_16', 'x', 4, .85, hits=4,
                                           hashes=GIF_HASHES)
    await send(chat, gif(1, 51, 'g1'))
    assert chat.store.blocked_media_count() == 0


@pytest.mark.asyncio
async def test_media_already_gone_is_still_blocklisted(chat):
    # «Запостил и удалил» во время налёта: сообщения уже нет, но вердикт
    # уверенный — копии того же медиа должны удаляться сразу.
    from telegram.error import BadRequest
    chat.tg.delete_message.side_effect = BadRequest('Message to delete not found')
    chat.scanner.return_value = media.Scan('checked', 'spoiler_16', 'x', 4, .85, hits=4,
                                           hashes=GIF_HASHES)
    await send(chat, gif(1, 51, 'g1'))
    assert chat.store.blocked_media_count() == 1


@pytest.mark.asyncio
async def test_confirmed_mild_text_keeps_its_severity(chat, monkeypatch):
    chat.store.ensure_review('t1', -100, 61, 'toxic', 'warn', 'модель', 'x', 'текст', media=dict(
        message_id=9, pending=True, severity=1, version=''))
    await press(chat, 'mod:ok:-100:61:t1')
    # Тяжесть 1: предупреждение без удаления, как решил бы автоматический путь.
    assert deleted(chat) == [] and chat.store.warn_count(-100, 61) == 1


def command_update(reply):
    return NS(message=NS(reply_to_message=reply, reply_text=AsyncMock()),
              effective_chat=NS(id=-100, type='supergroup'), effective_user=NS(id=7))


@pytest.mark.asyncio
async def test_modmiss_on_media_blocklists_it_without_punishing_the_author(chat, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=GIF_HASHES)
    await send(chat, gif(1, 51, 'g2'))  # копия в чате, бот её пропустил
    update = command_update(gif(2, 52, 'g1'))
    await bot.modmiss_command(update, NS(bot=chat.tg, args=['spoiler_16']))
    assert chat.store.find_blocked_media('g1')['source'] == 'admin'
    # Копия, уже висящая в чате, убрана; само отмеченное сообщение и его
    # автор не тронуты: /modmiss — отметка, а не санкция.
    assert deleted(chat) == [1]
    assert chat.store.warn_count(-100, 52) == 0 and chat.store.warn_count(-100, 51) == 0
    assert 'чёрном списке' in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_modunblock_command(chat, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    chat.store.block_media('orig', -100, 'nsfw', ['g1'], GIF_HASHES, 'admin')
    chat.scanner.return_value = media.Scan('checked', frames=4, hashes=COPY_HASHES)
    update = command_update(gif(5, 55, 'g7'))
    await bot.modunblock_command(update, NS(bot=chat.tg, args=[]))
    assert chat.store.blocked_media_count() == 0
    assert 'записей: 1' in update.message.reply_text.await_args.args[0]
    update = command_update(None)
    await bot.modunblock_command(update, NS(bot=chat.tg, args=[]))
    assert update.message.reply_text.await_args.args[0] == 'Чёрный список медиа пуст.'


def test_unblock_by_file_id_alone(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.block_media('a', -100, 'nsfw', ['g1', 'g2'], (), 'auto')
    assert store.unblock_media('nope', ['g2']) == 1 and store.blocked_media_count() == 0


@pytest.mark.asyncio
async def test_wrong_frees_the_entry_named_in_the_report_even_without_a_match(chat):
    # file_id копии вытеснен из записи, отпечатков нет — но отчёт помнит,
    # какая запись удалила сообщение.
    chat.store.block_media('orig', -100, 'nsfw', ['g1'], (), 'auto')
    chat.store.ensure_review('rv', -100, 51, 'nsfw', 'mute', bot.MODERATION_BLOCKLIST_SOURCE,
                             'x', '[гиф]', media=dict(file_unique_id='g-old', blocked_id='orig'))
    await press(chat, 'mod:wrong:-100:51:rv')
    assert chat.store.blocked_media_count() == 0


# ------------------------------------------------------------ наборы стикеров

def sticker(number, user, file_id, pack='lewd_pack'):
    return NS(chat_id=-100, message_id=number, text=None, caption=None, sender_chat=None,
              reply_to_message=None, media_group_id=None, has_media_spoiler=False,
              message_thread_id=None, link=None,
              from_user=NS(id=user, full_name=f'Участник {user}', is_bot=False),
              sticker=NS(file_unique_id=file_id, file_id='f' + file_id, set_name=pack,
                         is_animated=False, is_video=False, emoji='😳'))


def buttons(chat, prefix):
    return [button.callback_data
            for call in chat.tg.send_message.await_args_list
            for row in getattr(call.kwargs.get('reply_markup'), 'inline_keyboard', ()) or ()
            for button in row if button.callback_data.startswith(prefix)]


@pytest.mark.asyncio
async def test_sticker_report_offers_to_block_the_whole_pack(chat):
    chat.scanner.return_value = borderline(hashes=())
    await send(chat, sticker(1, 51, 's1'))
    assert len(buttons(chat, 'mod:pack:')) == 1
    # У гифки такой кнопки нет: набора у неё нет.
    await send(chat, gif(2, 52, 'g2'))
    assert len(buttons(chat, 'mod:pack:')) == 1


@pytest.mark.asyncio
async def test_blocking_the_pack_removes_its_stickers_now_and_later(chat):
    chat.scanner.return_value = media.Scan('checked', frames=1)
    await send(chat, sticker(1, 53, 's-earlier'))          # прошёл раньше как «чистый»
    await send(chat, sticker(2, 54, 's-other', pack='cats'))
    chat.scanner.return_value = borderline(hashes=())
    await send(chat, sticker(3, 51, 's1'))
    query = await press(chat, buttons(chat, 'mod:pack:')[0])
    assert sorted(deleted(chat)) == [1, 3]                # само сообщение и стикер из пака
    assert 'Весь набор стикеров «lewd_pack»' in query.result
    assert chat.store.find_blocked_media(sticker_set='lewd_pack')['source'] == 'admin'
    chat.scanner.reset_mock()
    await send(chat, sticker(4, 55, 's-new'))             # новый стикер того же пака
    chat.scanner.assert_not_awaited()
    assert deleted(chat)[-1] == 4
    await send(chat, sticker(5, 56, 's-cat', pack='cats'))
    assert 5 not in deleted(chat) and 2 not in deleted(chat)
    chat.tg.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_modunblock_on_a_sticker_frees_its_pack(chat, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    chat.store.block_media('p', -100, 'nsfw', [], (), 'admin', sticker_set='lewd_pack')
    chat.scanner.return_value = media.Scan('checked', frames=1)
    update = command_update(sticker(9, 59, 'another'))
    await bot.modunblock_command(update, NS(bot=chat.tg, args=[]))
    assert chat.store.blocked_media_count() == 0


def test_pack_entry_needs_no_file_id_or_fingerprint(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    assert store.block_media('p', -100, 'nsfw', [], (), 'admin', sticker_set='pack')['sticker_set'] == 'pack'
    assert store.find_blocked_media('', (), 'pack')['id'] == 'p'
    assert store.find_blocked_media('', (), 'other') == {}
    assert store.unblock_media('', sticker_set='pack') == 1



@pytest.mark.asyncio
async def test_wrong_frees_the_pack_named_in_the_report(chat):
    chat.store.block_media('p', -100, 'nsfw', [], (), 'admin', sticker_set='lewd_pack')
    chat.store.ensure_review('rv', -100, 51, 'nsfw', 'mute', bot.MODERATION_MEDIA_SOURCE,
                             'x', '[стикер]', media=dict(file_unique_id='zz', sticker_set='lewd_pack'))
    await press(chat, 'mod:wrong:-100:51:rv')
    assert chat.store.blocked_media_count() == 0


@pytest.mark.asyncio
async def test_pack_button_only_for_nudity_reports(chat):
    message = sticker(1, 51, 's1')
    for category, expected in (('toxic', 0), ('spoiler_16', 1)):
        chat.tg.send_message.reset_mock()
        bot._moderation_pending_media.clear()
        decision = dict(action='warn', media=dict(file_unique_id='s1', sticker_set='lewd_pack'),
                        decision_state='review')
        await bot._mod_report(chat.tg, message, category, decision, 'x', 'y', 'модель')
        assert len(buttons(chat, 'mod:pack:')) == expected, category


# ------------------------------------------------------ медиа в отчёте админу

def plain_tg():
    return with_media_senders(NS(send_message=AsyncMock(return_value=NS(message_id=900))))


@pytest.mark.asyncio
async def test_sticker_report_is_the_sticker_then_the_text_under_it():
    tg = plain_tg()
    await bot._mod_send_report(tg, 7, '<b>отчёт</b>', 'markup', sticker(1, 51, 's1'), True)
    assert tg.media_sent == [('send_sticker', 7, 'fs1', None)]
    call = tg.send_message.await_args
    assert call.args == (7, '<b>отчёт</b>')
    assert call.kwargs['reply_to_message_id'] == 901 and call.kwargs['reply_markup'] == 'markup'


@pytest.mark.asyncio
async def test_long_report_goes_under_the_media_not_in_its_caption():
    tg = plain_tg()
    photo = NS(photo=[NS(file_id='small'), NS(file_id='big')])
    await bot._mod_send_report(tg, 7, 'x' * 1001, None, photo, False)
    assert tg.media_sent == [('send_photo', 7, 'big', True)]
    assert tg.send_message.await_args.args == (7, 'x' * 1001)
    # Теги не считаются в лимит подписи: видимого текста 1000 — влезает.
    tg = plain_tg()
    await bot._mod_send_report(tg, 7, '<b>' + 'x' * 1000 + '</b>', None, photo, False)
    assert tg.send_message.await_args.args == (7, '<b>' + 'x' * 1000 + '</b>')
    assert tg.send_message.await_args.kwargs.get('reply_to_message_id') is None


@pytest.mark.asyncio
async def test_report_survives_a_media_that_cannot_be_sent():
    from telegram.error import BadRequest
    tg = plain_tg()
    tg.send_animation = AsyncMock(side_effect=BadRequest('wrong file identifier'))
    await bot._mod_send_report(tg, 7, 'отчёт', 'm', gif(1, 51, 'g1'), False)
    assert tg.send_message.await_args.args == (7, 'отчёт')
    assert tg.send_message.await_args.kwargs['reply_markup'] == 'm'


@pytest.mark.asyncio
async def test_text_report_and_documents():
    tg = plain_tg()
    await bot._mod_send_report(tg, 7, 'отчёт', None, bot._mod_message_stub(-100, 51, 9), False)
    assert tg.media_sent == [] and tg.send_message.await_count == 1
    await bot._mod_send_report(tg, 7, 'отчёт', None, NS(document=NS(file_id='doc')), False)
    assert tg.media_sent[-1] == ('send_document', 7, 'doc', None)  # у документа нет спойлера
    await bot._mod_send_report(tg, 7, 'x' * 1500, None, NS(document=NS(file_id='doc')), False)
    assert tg.media_sent[-1] == ('send_document', 7, 'doc', None)


# ------------------------------------------- снять запись без стикера под рукой

@pytest.mark.asyncio
async def test_modunblock_lists_entries_with_remove_buttons(chat, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    chat.store.block_media('p', -100, 'spoiler_16', [], (), 'admin', sticker_set='cute_pack')
    chat.store.block_media('miss:abc', -100, 'nsfw', ['g1'], (), 'auto')
    update = command_update(None)
    await bot.modunblock_command(update, NS(bot=chat.tg, args=[]))
    text = update.message.reply_text.await_args.args[0]
    markup = update.message.reply_text.await_args.kwargs['reply_markup']
    assert '1. медиа: контент 18+ · удалял бот' in text
    assert '2. набор стикеров «cute_pack» · подтвердил админ' in text
    data = [row[0].callback_data for row in markup.inline_keyboard]
    assert all(len(d) <= 64 for d in data)
    # Кнопка первой записи снимает именно её (id с двоеточием), пак остаётся.
    await press(chat, data[0])
    assert not chat.store.find_blocked_media('g1')
    assert chat.store.find_blocked_media(sticker_set='cute_pack')['id'] == 'p'
    await press(chat, data[1])
    assert chat.store.blocked_media_count() == 0
    query = await press(chat, data[0])
    assert query.answer.await_args.args[0] == 'Этой записи уже нет в списке'


@pytest.mark.asyncio
async def test_modunblock_by_pack_name(chat, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    chat.store.block_media('p', -100, 'spoiler_16', [], (), 'admin', sticker_set='cute_pack')
    update = command_update(None)
    await bot.modunblock_command(update, NS(bot=chat.tg, args=['cute_pack']))
    assert 'убран' in update.message.reply_text.await_args.args[0]
    assert chat.store.blocked_media_count() == 0
    await bot.modunblock_command(update, NS(bot=chat.tg, args=['cute_pack']))
    assert 'нет' in update.message.reply_text.await_args.args[0]
    await bot.modunblock_command(update, NS(bot=chat.tg, args=[]))
    assert update.message.reply_text.await_args.args[0] == 'Чёрный список медиа пуст.'


@pytest.mark.asyncio
async def test_pack_block_comes_with_an_undo_button(chat):
    chat.scanner.return_value = borderline(hashes=())
    await send(chat, sticker(1, 51, 's1'))
    await press(chat, buttons(chat, 'mod:pack:')[0])
    undo = [b for b in buttons(chat, 'mod:unblk:')]
    assert len(undo) == 1
    await press(chat, undo[0])
    assert not chat.store.find_blocked_media(sticker_set='lewd_pack')
