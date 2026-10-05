"""Находки стороннего аудита: отзыв прав, картинка разных событий, язык модели,
разные загрузки видео, честный журнал антифлуда.
"""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot
import llm_protocol as llm


# ------------------------------------------------- отзыв прав переживает рестарт

def _break_disk(monkeypatch):
    def boom(*_a, **_k):
        raise OSError('диск переполнен')
    monkeypatch.setattr(bot, '_atomic_write_json', boom)


def test_revoking_admin_is_not_reported_when_the_disk_write_fails(tmp_path, monkeypatch):
    path = tmp_path / 's.json'
    settings = bot.BotSettings(path)
    settings.add_admin(111)
    _break_disk(monkeypatch)
    with pytest.raises(OSError):
        settings.remove_admin(111)
    # Память и диск не расходятся: админ остался и там, и там.
    assert settings.extra_admins == [111]
    assert bot.BotSettings(path).extra_admins == [111]


def test_granting_admin_is_rolled_back_when_the_disk_write_fails(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 's.json')
    _break_disk(monkeypatch)
    with pytest.raises(OSError):
        settings.add_admin(222)
    assert settings.extra_admins == []


def test_save_reports_failure_and_warns_the_owner(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 's.json')
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    assert settings.save() is True
    _break_disk(monkeypatch)
    assert settings.save() is False
    assert any('Не удалось сохранить настройки' in a for a in bot._pending_admin_alerts)


@pytest.mark.asyncio
async def test_deladmin_tells_the_owner_when_rights_were_not_removed(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 's.json')
    settings.add_admin(111)
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, '_resolve_user', AsyncMock(return_value=(111, 'Вася', '')))
    _break_disk(monkeypatch)
    reply = AsyncMock()
    update = NS(effective_user=NS(id=bot.ADMIN_ID), message=NS(reply_text=reply))
    await bot.deladmin_command(update, NS(args=[]))
    text = reply.await_args.args[0]
    assert 'НЕ сняты' in text and 'больше не админ' not in text


# ------------------------------------------ один постер, разные события

def _store(tmp_path):
    return bot.ImageHashes(tmp_path / 'h.json')


FP = 'd:' + '0' * 16


def test_same_poster_for_a_different_event_is_not_a_duplicate(tmp_path):
    store = _store(tmp_path)
    store.add(FP, 'Anime season 2 announced')
    assert store.find_duplicate(FP, 'Anime season 2 premiere date revealed') is None
    assert store.find_duplicate(FP, 'Anime season 2 trailer released') is None


def test_same_poster_for_the_same_event_is_still_a_duplicate(tmp_path):
    store = _store(tmp_path)
    store.add(FP, 'Anime season 2 announced')
    assert store.find_duplicate(FP, 'Second season of Anime announced') is not None


def test_extra_event_in_the_new_headline_is_still_another_event(tmp_path):
    store = _store(tmp_path)
    store.add(FP, 'Anime trailer released')
    assert store.find_duplicate(FP, 'Anime trailer and cast revealed') is None


def test_poster_match_without_a_named_event_still_decides(tmp_path):
    # Событие не названо ни там, ни там — решает картинка, как раньше.
    store = _store(tmp_path)
    store.add(FP, 'Новости про аниме')
    assert store.find_duplicate(FP, 'Ещё новости про аниме') is not None
    assert store.find_duplicate(FP) is not None


@pytest.mark.asyncio
async def test_reservation_in_flight_also_respects_the_event(tmp_path):
    store = _store(tmp_path)
    assert store.reserve(FP, 'Anime cast revealed') is None
    assert store.reserve(FP, 'Anime cast revealed') is not None
    assert store.reserve(FP, 'Anime delayed to 2027') is None


# ------------------------------------------ язык ответа модели

def test_english_reply_of_the_model_is_rejected():
    source = 'Anime season two has been announced for next year.'
    reason = llm._editorial_rejection(
        source, 'Anime season two announced for next year',
        'The studio revealed a new key visual and the staff for the second season today.')
    assert reason == 'not_russian'


def test_russian_text_with_latin_titles_passes():
    source = 'Attack on Titan: The Final Chapters was announced by MAPPA.'
    assert llm._editorial_rejection(
        source, 'Attack on Titan: The Final Chapters анонсировали',
        'Студия MAPPA показала новый ключевой визуал и назвала дату выхода финала.') == ''


def test_latin_titles_do_not_make_a_russian_post_foreign():
    # Доля кириллицы у такого поста около 0.4: строже 0.2 нельзя.
    text = ('Frieren: Beyond Journey End и Mushoku Tensei: Jobless Reincarnation\n'
            'Студия объявила Season 3 для Sousou no Frieren и дала дату выхода.')
    assert not llm._not_russian(text)


def test_very_short_texts_are_not_judged_by_language():
    assert not llm._not_russian('Bleach: TYBW')


# ------------------------------------------ загрузка видео

class _FakeYDL:
    templates: list = []

    def __init__(self, opts):
        _FakeYDL.templates.append(opts['outtmpl'])

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def extract_info(self, *_a, **_k):
        raise RuntimeError('стоп')


def _templates(monkeypatch, urls):
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)
    monkeypatch.setattr(bot, 'yt_dlp', MagicMock(YoutubeDL=_FakeYDL))
    _FakeYDL.templates = []
    for url in urls:
        bot.download_video(url, [])
    return list(_FakeYDL.templates)


def test_different_links_with_the_same_tail_get_different_files(monkeypatch):
    tail = '&list=' + 'x' * 90
    a, b = _templates(monkeypatch, [
        'https://www.youtube.com/watch?v=AAAAAAAAAAA' + tail,
        'https://www.youtube.com/watch?v=BBBBBBBBBBB' + tail])
    assert a != b


def test_the_same_link_downloaded_twice_gets_separate_files(monkeypatch):
    # Публикация и диагностика одного ролика одновременно не делят путь.
    url = 'https://www.youtube.com/watch?v=AAAAAAAAAAA'
    a, b = _templates(monkeypatch, [url, url])
    assert a != b


# ------------------------------------------ антифлуд: честный журнал

@pytest.mark.asyncio
async def test_inflight_delete_refused_is_not_logged_as_deleted(tmp_path, monkeypatch):
    from telegram.error import BadRequest
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, '_mod_admin_exempt', AsyncMock(return_value=False))
    tg = NS(delete_message=AsyncMock(side_effect=BadRequest('Message can\'t be deleted')))
    message = NS(chat_id=-100, message_id=5)
    result = await bot._mod_rate_pause(tg, message, 51, 'Участник', 'текст', state='inflight')
    assert result.startswith('не удалено')
    assert store.recent_log(1)[0]['action'].startswith('не удалено')

    tg = NS(delete_message=AsyncMock(return_value=True))
    result = await bot._mod_rate_pause(tg, message, 51, 'Участник', 'текст', state='inflight')
    assert result == 'удалено: тот же залп'
    assert json.dumps(store.recent_log(1)[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_cycle_send_waits_for_the_publisher(monkeypatch):
    # Обе дороги в очередь идут под одним замком отправки.
    entered = []

    async def locked(_bot):
        entered.append(1)
        return 'sent', {}
    monkeypatch.setattr(bot, '_publish_one_from_queue_locked', locked)
    async with bot._queue_send_lock:
        task = asyncio.ensure_future(bot._publish_one_from_queue(None))
        await asyncio.sleep(0.05)
        assert entered == []
    await task
    assert entered == [1]
