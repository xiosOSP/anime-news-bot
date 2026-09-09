"""Regression checks for durable publication settings and moderation controls."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from PIL import Image, ImageEnhance

import anime_news_bot as bot
import moderation_media as media


@pytest.mark.asyncio
@pytest.mark.parametrize('role,allowed', [('administrator', True), ('creator', True), ('member', False)])
async def test_group_admin_can_switch_moderation_off_and_on(state, monkeypatch, role, allowed):
    monkeypatch.setattr(bot, 'is_admin', lambda update: False)
    monkeypatch.setattr(bot, 'MODERATION_CHATS_ENV', '-100')
    msg = NS(reply_text=AsyncMock(), sender_chat=None)
    update = NS(effective_message=msg, effective_user=NS(id=7, is_bot=False),
                effective_chat=NS(id=-100, type='supergroup'))
    ctx = NS(bot=NS(get_chat_member=AsyncMock(return_value=NS(status=role))), args=['off'])
    await bot.moderation_command(update, ctx)
    assert state.is_enabled(-100) == (not allowed)
    assert bot.ChatModerationStore(state.path).is_enabled(-100) == (not allowed)
    if allowed:
        ctx.args = ['on']
        await bot.moderation_command(update, ctx)
        assert bot.ChatModerationStore(state.path).is_enabled(-100)


@pytest.mark.asyncio
async def test_chat_switch_storage_error_preserves_state(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(state, '_save', lambda: False)
    msg = NS(reply_text=AsyncMock(), sender_chat=None)
    update = NS(effective_message=msg, effective_user=NS(id=7, is_bot=False),
                effective_chat=NS(id=-100, type='supergroup'))
    await bot.moderation_command(update, NS(args=['off']))
    assert state.is_enabled(-100)
    assert 'Не удалось' in msg.reply_text.call_args.args[0]


@pytest.mark.parametrize('mode,thread,channel', [('both', True, True), ('thread', True, False), ('channel', False, True)])
def test_publish_routing_and_interval_survive_real_reload(tmp_path, mode, thread, channel):
    path = tmp_path / 'settings.json'
    original = bot.BotSettings(path)
    original.publish_mode = mode
    original.channel_interval_min = 120
    original.check_interval_min = 10
    original.auto_enabled = True
    loaded = bot.BotSettings(path)
    assert loaded.publish_mode == mode
    assert loaded.thread_mode is thread and loaded.channel_autopost is channel
    assert loaded.channel_interval_min == 120
    assert loaded.check_interval_min == 10 and loaded.auto_enabled
    loaded.quiet_mode = True
    assert bot.BotSettings(path).publish_mode == mode


@pytest.mark.parametrize('legacy', [True, False])
def test_legacy_thread_mode_is_migrated_without_changing_destination(tmp_path, legacy):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'thread_mode': legacy}), encoding='utf-8')
    loaded = bot.BotSettings(path)
    assert loaded.publish_mode == ('thread' if legacy else 'channel')


@pytest.mark.parametrize('mode,interval', [(True, '120'), (['both'], False), ('unknown', -50)])
def test_invalid_publication_settings_keep_valid_legacy_route(tmp_path, mode, interval):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'publish_mode': mode, 'channel_interval_min': interval, 'thread_mode': True}), encoding='utf-8')
    loaded = bot.BotSettings(path)
    assert loaded.thread_mode and not loaded.channel_autopost
    assert loaded.channel_interval_min == loaded.check_interval_min


@pytest.fixture
def state(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'MODERATION_ADMINS_DEFAULT', True)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [])
    monkeypatch.setattr(bot, '_audit_update', lambda *a, **k: None)
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, '_moderation_update_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_moderation_action_lock', asyncio.Lock())
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent'):
        monkeypatch.setattr(bot, name, {})
    return store


def message(sender_chat=None):
    return NS(chat_id=-100, message_id=1, from_user=NS(id=7, full_name='User', is_bot=False),
              text='Я тебя убью', caption=None, sender_chat=sender_chat, reply_to_message=None)


def telegram(status='administrator'):
    return NS(get_chat_member=AsyncMock(return_value=NS(status=status)),
              delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(return_value=True),
              send_message=AsyncMock(return_value=NS(message_id=99)), edit_message_text=AsyncMock())


async def handle(tg, msg):
    await bot.moderation_message_handler(NS(effective_message=msg, effective_user=msg.from_user,
                                           effective_chat=NS(id=-100)), NS(bot=tg))


def test_admin_toggle_persists_and_failed_save_rolls_back(state, monkeypatch):
    assert state.moderate_admins
    assert state.set_moderate_admins(False)
    assert bot.ChatModerationStore(state.path).moderate_admins is False
    monkeypatch.setattr(state, '_save', lambda: False)
    assert state.set_moderate_admins(True) is False
    assert state.moderate_admins is False


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['administrator', 'creator', 'member'])
async def test_disabled_admin_moderation_preserves_regular_member_sanctions(state, status):
    state.set_moderate_admins(False)
    tg = telegram(status)
    await handle(tg, message())
    if status == 'member':
        tg.delete_message.assert_awaited_once()
        tg.restrict_chat_member.assert_awaited_once()
    else:
        tg.delete_message.assert_not_awaited()
        tg.restrict_chat_member.assert_not_awaited()
        assert state.warn_count(-100, 7) == 0


@pytest.mark.asyncio
async def test_disabled_admin_moderation_does_not_call_media_scanner(state, monkeypatch):
    state.set_moderate_admins(False)
    scanner = AsyncMock(side_effect=AssertionError('admin excluded before media work'))
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=scanner))
    msg = message()
    msg.text = None
    msg.photo = [NS(file_unique_id='a')]
    await handle(telegram(), msg)
    scanner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('sender_id,excluded', [(-100, True), (-200, False)])
async def test_toggle_covers_anonymous_admin_but_not_unrelated_channel(state, sender_id, excluded):
    state.set_moderate_admins(False)
    tg = telegram()
    await handle(tg, message(NS(id=sender_id, title='Chat')))
    assert bool(tg.delete_message.await_count) is not excluded


@pytest.mark.asyncio
async def test_admin_toggle_is_rechecked_before_applying_pending_decision(state):
    decision = bot._mod_decide('nsfw', 2, 0)
    state.set_moderate_admins(False)
    tg = telegram()
    await bot._mod_apply(tg, message(), decision, 'nsfw', '')
    tg.delete_message.assert_not_awaited()
    assert decision['applied_action'] == 'none'


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [True, False])
async def test_admin_menu_toggle_requires_bot_admin(state, monkeypatch, authorized):
    monkeypatch.setattr(bot, 'is_admin', lambda update: authorized)
    monkeypatch.setattr(bot, '_safe_edit', AsyncMock())
    query = NS(data='mods:admins', answer=AsyncMock())
    await bot.moderation_settings_callback(NS(callback_query=query), NS())
    assert state.moderate_admins is not authorized


@pytest.mark.asyncio
async def test_admin_command_changes_setting_and_reports_it(state, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    msg = NS(reply_text=AsyncMock())
    await bot.modadmins_command(NS(message=msg), NS(args=['off']))
    assert state.moderate_admins is False
    assert 'выключена' in msg.reply_text.call_args.args[0]
    await bot.modadmins_command(NS(message=msg), NS(args=['on']))
    assert state.moderate_admins is True


def gradient():
    data = np.zeros((80, 120, 3), dtype=np.uint8)
    data[:, :, 0] = np.arange(120)[None, :] * 2
    data[:, :, 1] = np.arange(80)[:, None] * 3
    data[:, :, 2] = 50
    return Image.fromarray(data)


@pytest.mark.parametrize('transform', [lambda x: x, lambda x: ImageEnhance.Color(x).enhance(.1),
                                     lambda x: ImageEnhance.Brightness(x).enhance(.3)])
def test_normalized_views_are_bounded_and_remove_color_cast(transform):
    frame = transform(gradient())
    views = list(media.detection_variants(frame))
    assert len(views) == 3 and all(v.size == frame.size and v.mode == 'RGB' for v in views)
    assert np.array_equal(np.asarray(views[0]), np.asarray(frame))
    gray = np.asarray(views[1])
    assert np.array_equal(gray[:, :, 0], gray[:, :, 1])
    assert np.array_equal(gray[:, :, 1], gray[:, :, 2])
    assert gray.max() > gray.min()


@pytest.mark.parametrize('label,category', [('ANUS_EXPOSED', 'nsfw'), ('BUTTOCKS_EXPOSED', 'spoiler_16')])
def test_structural_detection_survives_original_color_miss(label, category):
    def detect(array):
        if np.array_equal(array[:, :, 0], array[:, :, 1]):
            return [{'class': label, 'score': .95}]
        return []
    detector = NS(detect=Mock(side_effect=detect))
    result = media.scan_frame(detector, gradient())
    assert result.category == category
    assert 2 <= detector.detect.call_count <= 3


def test_original_high_confidence_stops_extra_work():
    detector = NS(detect=Mock(return_value=[{'class': 'ANUS_EXPOSED', 'score': .95}]))
    assert media.scan_frame(detector, gradient()).category == 'nsfw'
    detector.detect.assert_called_once()


def test_borderline_result_is_review_not_automatic_punishment():
    detector = NS(detect=Mock(return_value=[{'class': 'ANUS_EXPOSED', 'score': .72}]))
    result = media.scan_frame(detector, gradient())
    assert result.status == 'unchecked' and not result.category


def test_ordinary_body_parts_do_not_become_nudity_through_variants():
    detector = NS(detect=Mock(return_value=[{'class': 'MALE_BREAST_EXPOSED', 'score': .99}]))
    result = media.scan_frame(detector, gradient())
    assert result.status == 'checked' and not result.category
    assert detector.detect.call_count == 3


def test_photo_exif_orientation_matches_what_user_sees(tmp_path):
    path = tmp_path / 'rotated.jpg'
    image = Image.new('RGB', (60, 30), 'navy')
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    assert next(media._pillow_frames(path)).size == (30, 60)


@pytest.mark.asyncio
@pytest.mark.parametrize('photo_count', [0, 1, 2])
async def test_news_payload_keeps_forum_destination_after_settings_reload(tmp_path, monkeypatch, photo_count):
    path = tmp_path / 'settings.json'
    settings = bot.BotSettings(path)
    settings.publish_mode = 'both'
    settings.require_image = False
    settings.video_enabled = False
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(path))
    assert bot.settings.thread_mode and bot.settings.channel_autopost
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -1009876)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 4321)
    monkeypatch.setattr(bot, 'pending_posts', None)
    monkeypatch.setattr(bot, 'format_news_text_long', lambda news: 'Новая аниме-новость.')
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs_async', AsyncMock(return_value={}))
    monkeypatch.setattr(bot, '_resolve_photos_for_album', AsyncMock(side_effect=lambda photos, *args: photos))
    result = NS(message_id=5, chat_id=-1009876)
    tg = NS(send_message=AsyncMock(return_value=result), send_photo=AsyncMock(return_value=result),
            send_media_group=AsyncMock(return_value=[result, result]))
    news = {'title': 'News', 'source': 'Test', 'images': [f'https://img.example/{i}.jpg' for i in range(photo_count)]}
    assert await bot._send_post_thread_split(tg, news, None)
    calls = tg.send_message.call_args_list + tg.send_photo.call_args_list + tg.send_media_group.call_args_list
    assert calls
    assert all(c.kwargs['chat_id'] == -1009876 and c.kwargs['message_thread_id'] == 4321 for c in calls)
