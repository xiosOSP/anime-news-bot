"""Письма админу «нужна ручная оценка»: только там, где человеку есть на что смотреть.

Скриншоты админа 23.09.2026: пока модель модерации недоступна, бот писал
после каждой ссылки в чате («короткое сообщение со ссылкой; нужен контекст»)
и после каждого видео больше 20 МБ («превью чистое, но весь оригинал не
проверен»). Пауза в 15 минут прятала часть писем, но не саму проблему: они
шли весь вечер, и человеку в них не на что было смотреть.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import moderation_media as media


@pytest.fixture
def chat(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, '_moderation_action_lock', asyncio.Lock())
    monkeypatch.setattr(bot, '_moderation_update_lock', asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_windows',
                 '_moderation_recent', '_moderation_unjudged_reports', '_moderation_media_reports'):
        monkeypatch.setattr(bot, name, {})
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)     # модели нет
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [1])
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    return store


def _telegram():
    return NS(send_message=AsyncMock(), get_chat_member=AsyncMock(return_value=NS(status='member')),
              delete_message=AsyncMock(), restrict_chat_member=AsyncMock())


def _message(number, text=None, **kwargs):
    values = dict(chat_id=-100, message_id=number, from_user=NS(id=7, full_name='User', is_bot=False),
                  text=text, caption=None, sender_chat=None, media_group_id=None,
                  reply_to_message=None, link=f'https://t.me/c/100/{number}')
    values.update(kwargs)
    return NS(**values)


async def _handle(tg, msg):
    await bot.moderation_message_handler(
        NS(effective_message=msg, effective_user=msg.from_user, effective_chat=NS(id=-100),
           edited_message=None), NS(bot=tg))


def _admin_letters(tg):
    return [call for call in tg.send_message.await_args_list if call.args and call.args[0] == 1]


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['https://t.me/some_channel/12', 'глянь https://kinogo-free.example/1'])
async def test_a_link_without_the_model_is_logged_not_mailed(chat, text):
    tg = _telegram()
    await _handle(tg, _message(1, text=text))
    assert _admin_letters(tg) == []
    assert chat.recent_log(5)[-1]['category'] == 'spam'      # в журнале осталось


@pytest.mark.asyncio
async def test_a_named_suspicion_still_calls_a_human(chat):
    """Группа рядом с оскорбительным словом — повод позвать человека, как и раньше."""
    tg = _telegram()
    await _handle(tg, _message(2, text='все негры тупые'))
    assert len(_admin_letters(tg)) == 1


@pytest.mark.asyncio
async def test_an_invite_without_promo_still_calls_a_human(chat):
    tg = _telegram()
    await _handle(tg, _message(3, text='https://t.me/+AbCdEfGhIj'))
    assert len(_admin_letters(tg)) == 1


# ---------- медиа, которое целиком не проверить ----------

def _media_message(**kwargs):
    values = dict(photo=None, video=None, video_note=None, animation=None, sticker=None, document=None)
    values.update(kwargs)
    return NS(**values)


THUMB = NS(file_id='thumb', file_unique_id='thumb-u', file_size=900)
CLEAN = media.Scan('checked', frames=1)
FOUND = media.Scan('checked', 'nsfw', 'Откровенный контент: FEMALE_BREAST_EXPOSED', 1, .98)


@pytest.mark.asyncio
@pytest.mark.parametrize('video', [
    NS(file_id='big', file_size=media.MAX_BYTES + 1, thumbnail=THUMB, duration=40, width=1280, height=720),
    NS(file_id='long', file_size=5_000_000, thumbnail=THUMB, duration=media.MAX_DURATION + 60,
       width=1280, height=720),
    NS(file_id='huge', file_size=5_000_000, thumbnail=THUMB, duration=20, width=10_000, height=10_000),
])
async def test_clean_preview_of_uncheckable_video_needs_no_letter(monkeypatch, video):
    scanner = media.MediaScanner()
    probe = AsyncMock(return_value=CLEAN)
    monkeypatch.setattr(scanner, '_check_downloadable', probe)
    result = await scanner.check(NS(), _media_message(video=video))
    assert result.status == 'unchecked' and result.review is False
    assert 'превью чистое' in result.reason
    probe.assert_awaited_once()          # превью проверено, а не пропущено


@pytest.mark.asyncio
async def test_long_video_is_checked_by_its_preview_now():
    """Раньше ролик длиннее 3 минут не проверялся вовсе — даже по превью."""
    scanner = media.MediaScanner()
    scanner._check_downloadable = AsyncMock(return_value=FOUND)
    video = NS(file_id='long', file_size=5_000_000, thumbnail=THUMB,
               duration=media.MAX_DURATION + 60, width=1280, height=720)
    result = await scanner.check(NS(), _media_message(video=video))
    assert result.category == 'nsfw' and result.review is True
    assert 'Видео длиннее 3 минут' in result.reason


@pytest.mark.asyncio
async def test_no_preview_still_calls_a_human():
    scanner = media.MediaScanner()
    video = NS(file_id='big', file_size=media.MAX_BYTES + 1, thumbnail=None, duration=40,
               width=1280, height=720)
    result = await scanner.check(NS(), _media_message(video=video))
    assert result.review is True and 'превью нет' in result.reason


@pytest.mark.asyncio
async def test_preview_that_failed_still_calls_a_human(monkeypatch):
    scanner = media.MediaScanner()
    monkeypatch.setattr(scanner, '_check_downloadable',
                        AsyncMock(return_value=media.Scan('unchecked', reason='Детектор не отвечает')))
    video = NS(file_id='big', file_size=media.MAX_BYTES + 1, thumbnail=THUMB, duration=40,
               width=1280, height=720)
    result = await scanner.check(NS(), _media_message(video=video))
    assert result.review is True and 'превью тоже не проверено' in result.reason


@pytest.mark.asyncio
async def test_handler_logs_a_clean_big_video_without_a_letter(chat, monkeypatch):
    tg = _telegram()
    quiet = media.Scan('unchecked', reason='Файл превышает лимит загрузки Bot API 20 МБ; превью чистое',
                       review=False)
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=quiet)))
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    video = NS(file_id='big', file_unique_id='big-u', file_size=media.MAX_BYTES + 1)
    await _handle(tg, _message(4, video=video, photo=None, animation=None, video_note=None,
                               sticker=None, document=None, voice=None, audio=None))
    assert _admin_letters(tg) == []
    assert chat.recent_log(5)[-1]['category'] == 'media'


@pytest.mark.asyncio
async def test_handler_still_mails_a_broken_detector(chat, monkeypatch):
    tg = _telegram()
    broken = media.Scan('unchecked', reason='Детектор не отвечает 3 раза подряд')
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=broken)))
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    video = NS(file_id='v', file_unique_id='v-u', file_size=1000)
    await _handle(tg, _message(5, video=video, photo=None, animation=None, video_note=None,
                               sticker=None, document=None, voice=None, audio=None))
    assert len(_admin_letters(tg)) == 1
