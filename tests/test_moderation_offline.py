"""Offline moderation regressions: content rules, media and actual sanctions."""
import asyncio
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions
from telegram.error import BadRequest, TimedOut

import anime_news_bot as bot
import moderation_media as media
from moderation_rules import check_text, normalize


@pytest.mark.parametrize(('text', 'category'), [
    ('Твоя мама шлюха', 'family'),
    ('твoя мaма шлюха', 'family'),
    ('твоя ма\u200bма шлюха', 'family'),
    ('я ебал твою мать', 'family'),
    ('Путин опять выступает', 'politics'),
    ('Голосуйте за партию!', 'politics'),
    ('Сливаю его номер: +7 (999) 123-45-67', 'doxxing'),
    ('Ее адрес: улица Ленина, 15', 'doxxing'),
    ('Пришли код из смс для подтверждения', 'scam'),
    ('Отправьте сид-фразу в личку', 'scam'),
    ('Удвою крипту: https://example.org/money', 'scam'),
    ('Давайте заспамим их чат https://t.me/example', 'raid'),
    ('Рейдим https://t.me/+abcdef', 'raid'),
    ('Я тебя убью', 'aggression'),
    ('Убейся, это рофл', 'aggression'),
    ('Админ ты долбоеб', 'toxic_admin'),
    ('Заткнись уже', 'aggression'),
    ('Твое мнение никому не нужно', 'belittling'),
    ('Твоя сеструха шлюха', 'family'),
    ('ты конченый дебил', 'toxic'),
    ('https://www.pornhub.com/view_video', 'nsfw'),
])
def test_local_rules_without_llm(text, category):
    verdict = check_text(text, reply_to_user=True)
    assert verdict is not None and verdict.confident and verdict.category == category


@pytest.mark.parametrize('text', [
    'Мама тоже смотрит аниме', 'Семья шпиона — отличный сериал',
    'Твоя мама хорошо готовит', 'Война титанов в новом сезоне',
    'Выбор лучшего героя', 'Политика студии изменилась',
    'Игровой рейд в 20:00', 'Не спамим в чужой чат https://t.me/example',
    'Никому не отправляй код из смс', 'Мне написали: «пришли пароль»',
    'Мой номер +7 (999) 123-45-67', 'Ты дурак что ли 😂',
    'Этот злодей идиот', 'Не согласен с админом, он ошибся',
    'Он написал: «твоя мама шлюха»', 'Сдохнет ли злодей в следующей серии?',
    'Твоя мама не шлюха', 'Не сдохни там от смеха',
])
def test_local_false_positive_corpus(text):
    assert check_text(text) is None


def test_admin_reply_requires_actual_context():
    assert check_text('Ты дебил') is None
    assert check_text('Ты дебил', reply_to_admin=True).category == 'toxic_admin'


def test_zero_width_normalization():
    assert normalize('ма\u200bма') == 'мама'


@pytest.fixture
def state(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, '_moderation_action_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_moderation_update_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_moderation_seen_updates', {})
    monkeypatch.setattr(bot, '_moderation_windows', {})
    monkeypatch.setattr(bot, '_moderation_recent', {})
    monkeypatch.setattr(bot, '_MOD_LAST_ACTION', {})
    monkeypatch.setattr(bot, '_moderation_media_reports', {})
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [])
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, '_moderation_classify', AsyncMock(side_effect=AssertionError('LLM must not be called')))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=media.Scan('checked'))))
    return store


def message(text='Заткнись', number=1, **kwargs):
    values = dict(chat_id=-100, message_id=number, from_user=NS(id=7, full_name='User', is_bot=False),
                  text=text, caption=None, sender_chat=None, media_group_id=None, reply_to_message=None)
    values.update(kwargs)
    return NS(**values)


def telegram_bot():
    return NS(delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(return_value=True),
              get_chat_member=AsyncMock(return_value=NS(status='member')),
              send_message=AsyncMock(), ban_chat_member=AsyncMock(return_value=True),
              get_chat=AsyncMock(return_value=NS(permissions=ChatPermissions.all_permissions())))


async def handle(message, telegram):
    await bot.moderation_message_handler(NS(effective_message=message, effective_user=message.from_user,
                                            effective_chat=NS(id=message.chat_id)), NS(bot=telegram))


@pytest.mark.asyncio
async def test_text_enforced_and_retry_deduplicated_without_llm(state):
    telegram = telegram_bot()
    msg = message()
    await asyncio.gather(handle(msg, telegram), handle(msg, telegram))
    assert state.warn_count(-100, 7) == 1
    assert telegram.delete_message.await_count == 1
    assert telegram.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [BadRequest('not enough rights'), TimedOut(), False])
async def test_failed_mute_never_adds_warn_or_claims_success(state, failure):
    telegram = telegram_bot()
    if failure is False:
        telegram.restrict_chat_member.return_value = False
    else:
        telegram.restrict_chat_member.side_effect = failure
    decision = bot._mod_decide('nsfw', 2, 0)
    result = await bot._mod_apply(telegram, message(), decision, 'nsfw', '')
    assert state.warn_count(-100, 7) == 0
    assert 'не подтверждён' in result
    assert decision['applied_action'] in ('failed', 'unknown')
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mute_disables_all_media_and_survives_restart(state, monkeypatch):
    telegram = telegram_bot()
    decision = bot._mod_decide('nsfw', 2, 0)
    await bot._mod_apply(telegram, message(), decision, 'nsfw', '')
    assert not any(telegram.restrict_chat_member.call_args.kwargs['permissions'].to_dict().values())
    reloaded = bot.ChatModerationStore(state.path)
    monkeypatch.setattr(bot, 'chat_moderation', reloaded)
    await bot._mod_apply(telegram, message(), dict(decision), 'nsfw', '')
    assert reloaded.warn_count(-100, 7) == 1
    assert telegram.restrict_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_cooldown_still_deletes_forbidden_content(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 60)
    telegram = telegram_bot()
    await bot._mod_apply(telegram, message(), bot._mod_decide('aggression', 2, 0), 'aggression', '')
    await bot._mod_apply(telegram, message(number=2), bot._mod_decide('nsfw', 2, 1), 'nsfw', '')
    assert telegram.delete_message.await_count == 2
    assert state.warn_count(-100, 7) == 1


@pytest.mark.asyncio
async def test_observe_has_no_writes_to_telegram_or_warns(state):
    state.set_mode('observe')
    telegram = telegram_bot()
    await handle(message('Я тебя убью'), telegram)
    telegram.delete_message.assert_not_awaited()
    telegram.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_storage_failure_prevents_new_sanction(state, monkeypatch):
    monkeypatch.setattr(state, '_save', lambda: False)
    telegram = telegram_bot()
    result = await bot._mod_apply(telegram, message(), bot._mod_decide('nsfw', 2, 0), 'nsfw', '')
    assert 'не удалось сохранить' in result
    telegram.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_existing_admin_restriction_is_never_shortened(state):
    telegram = telegram_bot()
    telegram.get_chat_member.return_value = NS(status='restricted', until_date=datetime.now(timezone.utc) + timedelta(days=3))
    await bot._mod_apply(telegram, message(), bot._mod_decide('nsfw', 2, 0), 'nsfw', '')
    telegram.restrict_chat_member.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


def test_edits_replace_correct_message_and_albums_count_once(state):
    for i in range(10):
        bot._mod_note_message(-100, 7, 'u', str(i), message_id=i, media_group_id='album')
    assert len(bot._moderation_recent['-100:7']) == 1
    bot._mod_note_message(-100, 7, 'u', 'edited', message_id=2, counts_as_new=False)
    assert next(row for row in bot._moderation_windows[-100] if row['message_id'] == 2)['text'] == 'edited'
    assert bot._moderation_windows[-100][-1]['text'] == '9'


def test_different_stickers_and_photos_are_not_repeats(state):
    for kind in ('sticker', 'photo'):
        rows = []
        for i in range(3):
            item = NS(file_unique_id=f'{kind}-{i}', emoji='🐸')
            msg = message('', i, **{kind: [item] if kind == 'photo' else item})
            rows.append(bot._mod_message_text(msg))
        assert len(set(rows)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(('category', 'spoiler', 'sanction'), [
    ('nsfw', False, True), ('nsfw', True, True),
    ('spoiler_16', False, True), ('spoiler_16', True, False), ('', False, False),
])
async def test_media_checked_even_without_caption_or_llm(state, category, spoiler, sanction):
    bot._moderation_media_scanner.check.return_value = media.Scan('checked', category, 'test', 4, .95)
    telegram = telegram_bot()
    await handle(message('', photo=[NS(file_unique_id='photo')], has_media_spoiler=spoiler), telegram)
    assert bot._moderation_media_scanner.check.await_count == 1
    assert bool(telegram.delete_message.await_count) is sanction
    assert state.warn_count(-100, 7) == int(sanction)


@pytest.mark.asyncio
async def test_unchecked_media_is_visible_in_log_without_punishment(state):
    bot._moderation_media_scanner.check.return_value = media.Scan('unchecked', reason='timeout')
    telegram = telegram_bot()
    await handle(message('', animation=NS(file_unique_id='gif')), telegram)
    assert state.recent_log()[0]['action'] == 'не проверено'
    telegram.delete_message.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_album_deletes_each_violation_but_only_one_penalty(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    telegram = telegram_bot()
    for i in (1, 2):
        await bot._mod_apply(telegram, message(number=i, media_group_id='album'),
                             bot._mod_decide('nsfw', 2, 0), 'nsfw', '')
    assert telegram.delete_message.await_count == 2
    assert telegram.restrict_chat_member.await_count == 1
    assert state.warn_count(-100, 7) == 1


@pytest.mark.asyncio
async def test_undo_removes_only_its_warn_and_preserves_later_mute(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    for incident_id in ('old', 'new'):
        state.reserve_incident(incident_id, -100, 7, 'spam', 'mute')
        state.update_incident(incident_id, status='confirmed', add_warning=True,
                              mute_until=100 if incident_id == 'old' else 200)
    telegram = telegram_bot()
    telegram.get_chat_member.return_value = NS(status='restricted', until_date=200)
    query = NS(data='mod:undo:-100:7:old', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    assert state.warn_count(-100, 7) == 1
    telegram.restrict_chat_member.assert_not_awaited()
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    assert state.warn_count(-100, 7) == 1
    assert state.stats()['overturned_total'] == 1
    assert state.stats()['by_category']['spam']['overturned'] == 1


@pytest.mark.asyncio
async def test_undo_mute_restores_chat_permissions_and_handles_failure(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    state.reserve_incident('a', -100, 7, 'nsfw', 'mute')
    state.update_incident('a', status='confirmed', add_warning=True, mute_until=100)
    telegram = telegram_bot()
    telegram.get_chat_member.return_value = NS(status='restricted', until_date=100)
    query = NS(data='mod:undo:-100:7:a', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    telegram.restrict_chat_member.side_effect = BadRequest('denied')
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    assert state.warn_count(-100, 7) == 1
    telegram.restrict_chat_member.side_effect = None
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    assert state.warn_count(-100, 7) == 0
    assert telegram.restrict_chat_member.call_args.kwargs['permissions'] == telegram.get_chat.return_value.permissions


def test_log_zero_does_not_keep_messages(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_LOG_MAX', 0)
    state.log_decision(-100, 7, 'x', 'spam', 'warn', 'local', '', 'private text')
    assert state.recent_log() == []


@pytest.mark.parametrize('label', sorted(media.EXPLICIT))
def test_explicit_detection_ignores_spoiler(label):
    assert media.classify_detections([{'class': label, 'score': .95}]).category == 'nsfw'


@pytest.mark.parametrize('label', ['BELLY_EXPOSED', 'MALE_BREAST_EXPOSED', 'FEET_EXPOSED', 'FEMALE_BREAST_COVERED'])
def test_non_explicit_body_parts_do_not_trigger_nudity(label):
    assert media.classify_detections([{'class': label, 'score': .99}]).category == ''


def test_confidence_and_severity_priority():
    assert not media.classify_detections([{'class': 'ANUS_EXPOSED', 'score': .79}]).category
    assert not media.classify_detections([{'class': 'ANUS_EXPOSED', 'score': 'nan'}]).category
    detections = [{'class': 'BUTTOCKS_EXPOSED', 'score': .95}, {'class': 'ANUS_EXPOSED', 'score': .9}]
    assert media.classify_detections(detections).category == 'nsfw'
    assert media.sample_indices(100) == sorted(set(media.sample_indices(100)))
    assert media.sample_indices(100)[-1] == 99


@pytest.mark.asyncio
async def test_scanner_rejects_oversize_without_download():
    telegram = NS(get_file=AsyncMock())
    scanner = media.MediaScanner()
    result = await scanner.check(telegram, message('', photo=[NS(file_size=media.MAX_BYTES + 1)]))
    assert result.status == 'unchecked'
    telegram.get_file.assert_not_awaited()


def test_real_offline_detector_on_generated_benign_image(tmp_path):
    from PIL import Image
    path = tmp_path / 'blank.png'
    Image.new('RGB', (64, 64), 'blue').save(path)
    result = media.run_worker(path, 'image', 40, .8, .85)
    assert result.status == 'checked', result
    assert result.category == '' and result.frames == 1


def test_gif_late_frames_and_tgs_rendering(tmp_path):
    from PIL import Image
    path = tmp_path / 'test.gif'
    Image.new('RGB', (24, 24), 'blue').save(path, save_all=True,
        append_images=[Image.new('RGB', (24, 24), 'red')], duration=100, loop=0)
    frames = list(media._pillow_frames(path))
    assert len(frames) == 2 and frames[0].getpixel((0, 0)) != frames[-1].getpixel((0, 0))
    tgs = tmp_path / 'sticker.tgs'
    data = dict(v='5.5.2', fr=30, ip=0, op=2, w=32, h=32, nm='safe', ddd=0, assets=[], layers=[])
    tgs.write_bytes(gzip.compress(json.dumps(data).encode()))
    frames = list(media._tgs_frames(tgs))
    assert 2 <= len(frames) <= media.MAX_FRAMES
    assert all(frame.size == (512, 512) for frame in frames)


def test_playlist_is_never_opened_by_video_decoder(tmp_path):
    path = tmp_path / 'fake.mp4'
    path.write_text('#EXTM3U\nhttps://example.org/private.mp4\n')
    result = media.scan_file(path, 'video')
    assert result.status == 'unchecked'


def test_real_video_decoder_samples_last_frame(tmp_path):
    import cv2
    import numpy as np
    path = tmp_path / 'video.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (32, 32))
    assert writer.isOpened()
    try:
        for i in range(30):
            frame = np.zeros((32, 32, 3), dtype=np.uint8)
            frame[:, :, 0 if i < 29 else 2] = 255
            writer.write(frame)
    finally:
        writer.release()
    frames = list(media._video_frames(path))
    assert len(frames) == media.MAX_FRAMES
    assert frames[0].getpixel((0, 0))[2] > 200
    assert frames[-1].getpixel((0, 0))[0] > 200


@pytest.mark.asyncio
async def test_captions_on_distinct_media_and_album_are_not_spam(state):
    telegram = telegram_bot()
    for i in range(3):
        await handle(message('', i, caption='Новый сезон', photo=[NS(file_unique_id=str(i))],
                             media_group_id='album'), telegram)
    telegram.delete_message.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_scanner_cache_and_worker_timeout(tmp_path, monkeypatch):
    path = tmp_path / 'file'
    real_worker = media.run_worker
    async def download(**kwargs):
        kwargs['custom_path'].write_bytes(b'content')
    telegram = NS(get_file=AsyncMock(return_value=NS(file_size=7, download_to_drive=download)))
    calls = []
    def worker(*args):
        calls.append(args)
        return media.Scan('checked', 'nsfw', 'test', 1, .9)
    monkeypatch.setattr(media, 'run_worker', worker)
    scanner = media.MediaScanner()
    msg = message('', sticker=NS(file_id='telegram-id', file_unique_id='unique', file_size=7))
    assert (await scanner.check(telegram, msg)).category == 'nsfw'
    assert (await scanner.check(telegram, msg)).category == 'nsfw'
    assert len(calls) == 1
    telegram.get_file.assert_awaited_once()
    assert not calls[0][0].exists()
    # Independently exercise supervisor timeout handling without a real hang.
    def timed_out(*args, **kwargs):
        raise media.subprocess.TimeoutExpired('worker', 1)
    monkeypatch.setattr(media.subprocess, 'run', timed_out)
    assert real_worker(path, 'image', 5, .8, .85).status == 'unchecked'


@pytest.mark.asyncio
async def test_media_probe_uses_detector_without_sanctions(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    msg = message('', photo=[NS(file_unique_id='test')])
    query_message = NS(reply_to_message=msg, reply_text=AsyncMock())
    telegram = telegram_bot()
    await bot.modtest_command(NS(message=query_message), NS(args=[], bot=telegram))
    bot._moderation_media_scanner.check.assert_awaited_once()
    telegram.delete_message.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_llm_boolean_string_is_not_a_violation(monkeypatch):
    # Explicitly call classifier: text path defaults to offline mode.
    monkeypatch.setattr(bot, '_moderation_llm_ready', lambda: True)
    monkeypatch.setattr(bot, '_moderation_llm_budget_left', lambda: 100)
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value='{"violation":"false","category":"nsfw"}'))
    assert (await bot._moderation_classify(-100, 'test'))['violation'] is False


def test_failed_settings_and_clear_are_rolled_back(state, monkeypatch):
    state.add_warn(-100, 7, 'spam')
    monkeypatch.setattr(state, '_save', lambda: False)
    assert state.set_mode('observe') is False
    assert state.mode == 'active'
    assert state.set_chat(-100, False) is False
    assert state.is_enabled(-100)
    with pytest.raises(OSError):
        state.clear_warns(-100, 7)
    assert state.warn_count(-100, 7) == 1
    with pytest.raises(OSError):
        state.add_warn(-100, 7, 'spam')
    assert state.warn_count(-100, 7) == 1


@pytest.mark.asyncio
async def test_manual_ban_requires_current_admin_check_and_is_idempotent(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    state.reserve_incident('a', -100, 7, 'family', 'escalate')
    state.update_incident('a', status='confirmed')
    query = NS(data='mod:ban:-100:7:a', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    telegram = telegram_bot()
    telegram.get_chat_member.return_value = NS(status='administrator')
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    telegram.ban_chat_member.assert_not_awaited()
    telegram.get_chat_member.return_value = NS(status='member')
    monkeypatch.setattr(bot, '_audit_update', lambda *a, **k: None)
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    await bot.moderation_callback(NS(callback_query=query), NS(bot=telegram))
    telegram.ban_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_decision_respects_disabled_chat_and_new_admin(state):
    telegram = telegram_bot()
    state.set_chat(-100, False)
    decision = bot._mod_decide('aggression', 2, 0)
    await bot._mod_apply(telegram, message(), decision, 'aggression', '')
    telegram.delete_message.assert_not_awaited()
    state.set_chat(-100, True)
    telegram.get_chat_member.return_value = NS(status='administrator')
    await bot._mod_apply(telegram, message(), decision, 'aggression', '')
    telegram.delete_message.assert_awaited_once()
    telegram.restrict_chat_member.assert_not_awaited()
    assert decision['restriction_blocked'] == 'admin'
    assert state.warn_count(-100, 7) == 1


# ---------- условная «угроза» — это шутка чата, а не намерение ----------

def test_conditional_threat_is_not_punished():
    """«Убью, если заспойлеришь» — обычная шутка аниме-чата.

    Тяжесть 3 пропускает ступень предупреждения, поэтому такая фраза стоила
    человеку часа мута с первого раза — без модели и без единого человека в
    решении. Правила чата прямо разрешают дружеские подколы; настоящая угроза
    условия не ставит.
    """
    assert check_text('я тебя убью если заспойлеришь') is None
    assert check_text('убью тебя когда встретимся в рейде') is None


def test_unconditional_threat_is_still_caught():
    """Послабление не должно распространяться на прямую угрозу."""
    for text in ('я тебя убью', 'сдохни тварь', 'убью тебя сука'):
        verdict = check_text(text)
        assert verdict is not None and verdict.category == 'aggression', text
        assert verdict.severity == 3


# ============== Детектор медиа: почему он молчит и кого он пропускает ==============
# На проде отчёты выглядели так: «Локальный детектор недоступен или завершился
# с ошибкой» и дважды «Локальная проверка занята». Первый текст не позволял
# понять причину — stderr воркера выбрасывался; второй означал, что медиа,
# пришедшее во время чужой проверки, не проверялось вообще.

def _completed(returncode=0, stdout='', stderr=''):
    return NS(returncode=returncode, stdout=stdout, stderr=stderr)


class TestWorkerFailureIsExplained:
    def test_missing_library_is_named(self, monkeypatch):
        stderr = ('Traceback (most recent call last):\n'
                  "  File \"moderation_media.py\", line 1\n"
                  "ModuleNotFoundError: No module named 'nudenet'")
        monkeypatch.setattr(media, '_invoke_worker',
                            lambda *a, **k: _completed(1, '', stderr))
        scan = media.run_worker('file', 'image', 5, .8, .85)
        assert scan.status == 'unchecked'
        assert 'nudenet' in scan.reason, scan.reason

    def test_killed_worker_is_told_apart_from_a_crash(self):
        """Смерть по SIGKILL — это память хостинга, а не сломанная установка.

        Лечится это разными способами, и одинаковый текст на оба случая
        отправлял чинить не то.
        """
        killed = media.worker_failure_reason(-9, '')
        crashed = media.worker_failure_reason(2, 'ValueError: broken')
        assert 'памят' in killed.lower()
        assert 'памят' not in crashed.lower() and 'ValueError: broken' in crashed

    def test_unparsable_answer_is_not_reported_as_success(self, monkeypatch):
        monkeypatch.setattr(media, '_invoke_worker',
                            lambda *a, **k: _completed(0, 'не json', ''))
        assert media.run_worker('file', 'image', 5, .8, .85).status == 'unchecked'

    def test_probe_reports_the_real_output(self, monkeypatch):
        monkeypatch.setattr(media, '_invoke_worker',
                            lambda *a, **k: _completed(1, '', 'onnxruntime не установлен'))
        scan, details, spent = media.probe(timeout=5)
        assert scan.status == 'unchecked'
        assert 'onnxruntime не установлен' in details
        assert spent >= 0


@pytest.mark.skipif(os.name == 'nt', reason='жёсткие лимиты есть только на POSIX')
def test_worker_survives_a_host_with_lower_hard_limits(tmp_path):
    """Хостинг с потолком ниже запрошенного не должен ронять проверку.

    Воркер просил RLIMIT 3 ГБ жёстко. Там, где потолок ниже, setrlimit кидает
    ValueError — не ImportError, который единственный ловился, — и процесс
    падал с трейсбеком ещё до первого кадра. В отчёте это выглядело как
    «детектор недоступен», хотя установлено было всё.
    """
    import resource
    from PIL import Image
    path = tmp_path / 'probe.png'
    Image.new('RGB', (64, 64), 'slategray').save(path)

    def lower_the_ceiling():
        resource.setrlimit(resource.RLIMIT_CPU, (40, 40))

    result = media.subprocess.run(
        [media.sys.executable, str(media.Path(media.__file__).resolve()),
         str(path), 'image', '0.80', '0.85'],
        capture_output=True, text=True, timeout=120, preexec_fn=lower_the_ceiling)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'checked'


class TestQueueInsteadOfSkipping:
    """«Занято» означало «не проверено»: 18+ проходил, когда медиа много."""

    @staticmethod
    def _telegram():
        async def download(**kwargs):
            kwargs['custom_path'].write_bytes(b'content')
        return NS(get_file=AsyncMock(return_value=NS(file_size=7, download_to_drive=download)))

    @staticmethod
    def _message(unique):
        return message('', sticker=NS(file_id=f'id-{unique}', file_unique_id=unique, file_size=7))

    @pytest.mark.asyncio
    async def test_second_media_waits_and_gets_checked(self, monkeypatch):
        started = []

        def slow_worker(*args):
            started.append(args)
            time.sleep(.05)
            return media.Scan('checked', 'nsfw', 'test', 1, .9)

        monkeypatch.setattr(media, 'run_worker', slow_worker)
        scanner = media.MediaScanner()
        telegram = self._telegram()
        results = await asyncio.gather(scanner.check(telegram, self._message('a')),
                                       scanner.check(telegram, self._message('b')))
        assert [r.status for r in results] == ['checked', 'checked']
        assert [r.category for r in results] == ['nsfw', 'nsfw']
        assert len(started) == 2

    @pytest.mark.asyncio
    async def test_overflowing_queue_still_refuses_instead_of_hanging(self, monkeypatch):
        """Потолок нужен: ждущая проверка держит обработчик апдейта."""
        def slow_worker(*args):
            time.sleep(.05)
            return media.Scan('checked')

        monkeypatch.setattr(media, 'run_worker', slow_worker)
        scanner = media.MediaScanner(max_waiting=0)
        telegram = self._telegram()
        results = await asyncio.gather(scanner.check(telegram, self._message('a')),
                                       scanner.check(telegram, self._message('b')))
        assert sorted(r.status for r in results) == ['checked', 'unchecked']
        refused = [r for r in results if r.status == 'unchecked'][0]
        assert 'переполнена' in refused.reason

    @pytest.mark.asyncio
    async def test_failed_check_releases_the_slot(self, monkeypatch):
        monkeypatch.setattr(media, 'run_worker',
                            lambda *a: media.Scan('checked', 'nsfw', 'x', 1, .9))
        scanner = media.MediaScanner()
        broken = NS(get_file=AsyncMock(side_effect=OSError('сеть отвалилась')))
        assert (await scanner.check(broken, self._message('a'))).status == 'unchecked'
        # Слот обязан вернуться, иначе одна сетевая ошибка глушит проверку навсегда.
        assert (await scanner.check(self._telegram(), self._message('b'))).category == 'nsfw'

    def test_scanner_survives_a_new_event_loop(self, monkeypatch):
        """Сканер живёт с импорта, а цикл событий у каждого теста свой.

        Примитив asyncio привязывается к циклу в момент состязания за него, и
        один замок на все циклы давал бы «bound to a different event loop» —
        падение, зависящее от порядка файлов, то есть худшее из возможных.
        Поэтому в каждом цикле здесь именно состязание, а не одиночный вызов.
        """
        monkeypatch.setattr(media, 'run_worker',
                            lambda *a: media.Scan('checked', 'nsfw', 'x', 1, .9))
        scanner = media.MediaScanner()
        telegram = self._telegram()

        async def two_at_once(mark):
            return await asyncio.gather(scanner.check(telegram, self._message(f'{mark}1')),
                                        scanner.check(telegram, self._message(f'{mark}2')))

        for mark in ('a', 'b'):
            results = asyncio.run(two_at_once(mark))
            assert [r.category for r in results] == ['nsfw', 'nsfw']


@pytest.mark.asyncio
async def test_mediaping_shows_the_reason_and_punishes_nobody(monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'media_probe',
                        lambda timeout, memory_mb: (
                            media.Scan('unchecked', reason='Детектор убит (SIGKILL)'),
                            'onnxruntime: cannot allocate memory', 1.5))
    edit = AsyncMock()
    msg = NS(reply_text=AsyncMock(return_value=NS(edit_text=edit)))
    await bot.mediaping_command(NS(message=msg), NS(args=[], bot=telegram_bot()))
    report = edit.await_args.args[0]
    assert 'SIGKILL' in report and 'cannot allocate memory' in report
    assert 'не наказывает' in report


@pytest.mark.asyncio
async def test_mediaping_names_the_number_the_machine_asked_for(monkeypatch):
    """Замер обязан дойти до чата — иначе он остался в коде и не помог.

    Владелец правит MODERATION_MEDIA_MEMORY_MB в панели хостинга, глядя на
    ответ бота. Если число знает только воркер, подбор снова идёт наугад.
    И подпись обязана говорить про адресное пространство: «память» отправляла
    сравнивать потолок с объёмом ОЗУ, то есть занижать его до отказа на
    каждой проверке.
    """
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'media_probe',
                        lambda timeout, memory_mb: (
                            media.Scan('checked', address_space_mb=1408), '', 2.0))
    edit = AsyncMock()
    msg = NS(reply_text=AsyncMock(return_value=NS(edit_text=edit)))
    await bot.mediaping_command(NS(message=msg), NS(args=[], bot=telegram_bot()))
    report = edit.await_args.args[0]
    assert '1408' in report, report
    assert 'адресного пространства' in report, report


class TestBrokenDetectorStopsPilingUp:
    """Сломанный детектор ломается одинаково для всех.

    На проде это выглядело так: сначала «очередь локальной проверки
    переполнена», и только потом становилось видно, что детектор вообще не
    работает. Отчёт говорил про очередь — то есть про следствие.
    """

    @staticmethod
    def _telegram():
        async def download(**kwargs):
            kwargs['custom_path'].write_bytes(b'content')
        return NS(get_file=AsyncMock(return_value=NS(file_size=7, download_to_drive=download)))

    @staticmethod
    def _message(unique):
        return message('', sticker=NS(file_id=f'id-{unique}', file_unique_id=unique, file_size=7))

    @pytest.mark.asyncio
    async def test_pause_after_a_run_of_failures(self, monkeypatch):
        calls = []

        def broken(*args):
            calls.append(args)
            return media.Scan('unchecked', reason='Детектор убит (SIGABRT)')

        monkeypatch.setattr(media, 'run_worker', broken)
        scanner = media.MediaScanner()
        telegram = self._telegram()
        for i in range(scanner.failure_limit):
            assert (await scanner.check(telegram, self._message(f'x{i}'))).status == 'unchecked'
        spent = len(calls)
        result = await scanner.check(telegram, self._message('after'))
        assert len(calls) == spent, 'после паузы воркер запускаться не должен'
        assert 'пауза' in result.reason and 'подряд' in result.reason

    @pytest.mark.asyncio
    async def test_a_working_check_clears_the_run(self, monkeypatch):
        answers = [media.Scan('unchecked', reason='сбой'),
                   media.Scan('checked'),
                   media.Scan('unchecked', reason='сбой')]

        monkeypatch.setattr(media, 'run_worker', lambda *a: answers.pop(0))
        scanner = media.MediaScanner()
        scanner.failure_limit = 2
        telegram = self._telegram()
        for unique in ('a', 'b', 'c'):
            await scanner.check(telegram, self._message(unique))
        # Один сбой до и один после удачной проверки — это не серия.
        assert scanner.paused_for() == 0

    @pytest.mark.asyncio
    async def test_refusals_before_the_worker_are_not_detector_failures(self, monkeypatch):
        """Слишком большой файл — не поломка детектора, а штатный отказ."""
        monkeypatch.setattr(media, 'run_worker', lambda *a: media.Scan('checked'))
        scanner = media.MediaScanner()
        telegram = self._telegram()
        oversize = message('', photo=[NS(file_size=media.MAX_BYTES + 1, file_unique_id='big')])
        for _ in range(scanner.failure_limit + 2):
            assert (await scanner.check(telegram, oversize)).status == 'unchecked'
        assert scanner.paused_for() == 0


def test_detector_session_is_pinned_to_one_thread_without_arena():
    """Настройки сессии — причина, по которой детектор влезает в 2 ГБ.

    nudenet поднимает onnxruntime по числу ядер и с ареной памяти: на машине с
    четырьмя ядрами это 1.34 ГБ адресного пространства вместо 1.05 ГБ, и на
    хостинге проверка падала с SIGABRT. Передать настройки nudenet не даёт,
    поэтому конструктор подменяется на время вызова.
    """
    import onnxruntime
    seen = {}
    original = onnxruntime.InferenceSession

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def get_inputs(self):
            return [NS(name='images')]

    onnxruntime.InferenceSession = _FakeSession
    try:
        media.build_detector()
        # Снимаем состояние ДО собственного восстановления, иначе проверка
        # подмены проверяла бы аккуратность теста, а не кода.
        after_call = onnxruntime.InferenceSession
    finally:
        onnxruntime.InferenceSession = original

    options = seen.get('sess_options')
    assert options is not None, 'сессия создана с настройками nudenet, а не нашими'
    assert options.enable_cpu_mem_arena is False
    assert options.intra_op_num_threads == 1
    # Конструктор обязан вернуться на место: подмена живёт только на время вызова.
    assert after_call is _FakeSession, 'подмена конструктора пережила вызов'
