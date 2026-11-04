"""Экономный режим по памяти (ноябрь 2026).

Пересказ: /lifecycle показал «Память контейнера: 193 МБ, лимит 256 МБ» и 35
перезапусков подряд. Бот в покое ~190 МБ, процесс проверки медиа в чате ~220
МБ, yt-dlp и ffmpeg — по 100–200 МБ: любая гифка или ролик убивали бота.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import moderation_media as media
import test_moderation_16_allowed as _chat_tests

chat = _chat_tests.chat      # та же фикстура чата, что в тестах модерации


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value, limit_mb=None):
        monkeypatch.setattr(bot, 'LOW_MEMORY_MODE', value)
        monkeypatch.setattr(bot, '_low_memory_cache', {})
        monkeypatch.setattr(bot, '_cgroup_memory_mb',
                            lambda *a: {'max': limit_mb} if limit_mb else {})
    return set_mode


@pytest.mark.parametrize('value, limit, expected', [
    ('auto', 256, True), ('auto', 1024, False), ('auto', None, False),
    ('on', 4096, True), ('off', 256, False),
])
def test_mode_follows_the_container_limit(mode, value, limit, expected):
    mode(value, limit)
    assert bot._low_memory_mode() is expected


def test_no_videos_in_low_memory(mode, tmp_path):
    settings = bot.BotSettings(tmp_path / 's.json')
    settings.video_enabled = True
    mode('auto', 256)
    assert settings.video_enabled is False
    mode('auto', 1024)
    assert settings.video_enabled is True


def test_video_download_is_not_started(mode, tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 's.json')
    settings.video_enabled = True
    monkeypatch.setattr(bot, 'settings', settings)
    download = AsyncMock()
    monkeypatch.setattr(bot, '_download_video_bounded', download)
    mode('on')
    news = {'title': 'Трейлер', 'video': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}
    assert asyncio.run(bot._prepare_video_file(news)) is None
    download.assert_not_called()


@pytest.mark.asyncio
async def test_chat_media_is_not_sent_to_the_detector(chat, mode):
    mode('on')
    scan = media.Scan('checked', 'nsfw', 'Обнаружена явная нагота', 4, .97)
    tg = await chat.run(scan)
    assert bot._moderation_media_scanner.check.await_count == 0
    tg.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_detector_still_works_with_enough_memory(chat, mode):
    mode('off')
    tg = await chat.run(media.Scan('checked', 'nsfw', 'Обнаружена явная нагота', 4, .97))
    tg.delete_message.assert_awaited_once_with(-100, 1)


def test_lifecycle_names_the_mode(mode, monkeypatch):
    monkeypatch.setattr(bot, '_cgroup_memory_mb', lambda *a: {'current': 193, 'max': 256})
    monkeypatch.setattr(bot, 'LOW_MEMORY_MODE', 'auto')
    monkeypatch.setattr(bot, '_low_memory_cache', {})
    assert 'Экономный режим' in bot._lifecycle_container_memory_line({})


def test_memory_is_returned_after_each_cycle(monkeypatch, tmp_path):
    trims = []
    monkeypatch.setattr(bot, '_malloc_trim', lambda: trims.append(1))
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', tmp_path / 'life.json')
    bot._mark_auto_cycle_finished('ok')
    assert trims == [1]


def test_malloc_is_tuned_at_start():
    import inspect
    assert '_tune_malloc()' in inspect.getsource(bot.main)
