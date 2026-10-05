"""Ночная тишина канала: с 22:40 до 08:00 автопостинг не публикует.

Ночью подписчики спят: пост уходит вниз ленты без реакций, а уведомление
будит. Готовые посты при этом не теряются — ждут в очереди до утра.
"""
import asyncio
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('raw, window', [
    ('22:00-06:00', (22 * 60, 6 * 60)),
    ('22-6', (22 * 60, 6 * 60)),
    ('23:30 – 07:15', (23 * 60 + 30, 7 * 60 + 15)),
    ('off', None),
    ('', None),
    ('25-6', None),
    ('22:60-06:00', None),
    ('6-6', None),          # пустое окно — не «тишина круглые сутки»
])
def test_window_parsing(raw, window):
    assert bot._quiet_window(raw) == window


@pytest.mark.parametrize('hhmm, quiet', [
    ('22:39', False), ('22:40', True), ('23:45', True), ('00:00', True),
    ('03:00', True), ('07:59', True), ('08:00', False), ('14:00', False),
])
def test_default_night_window_crosses_midnight(monkeypatch, hhmm, quiet):
    # Окно по умолчанию берём из кода бота (conftest выключает тишину для
    # остальных тестов): владелец просил 22:40–08:00.
    default = re.search(r"_env\('CHANNEL_QUIET_HOURS', '([^']*)'\)",
                        Path(bot.__file__).read_text(encoding='utf-8')).group(1)
    assert bot._quiet_window(default) == (22 * 60 + 40, 8 * 60)
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', default)
    now = datetime.strptime(f'2026-10-05 {hhmm}', '%Y-%m-%d %H:%M')
    assert bot._channel_quiet_now(now) is quiet


def test_daytime_window_and_off(monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', '13-14')
    assert bot._channel_quiet_now(datetime(2026, 10, 5, 13, 30)) is True
    # Конец окна не входит в тишину: в 14:00 посты уже идут.
    assert bot._channel_quiet_now(datetime(2026, 10, 5, 14, 0)) is False
    assert bot._channel_quiet_now(datetime(2026, 10, 5, 23, 0)) is False
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', 'off')
    assert bot._channel_quiet_now(datetime(2026, 10, 5, 23, 0)) is False


class _UntouchableQueue:
    """Очередь, к которой нельзя прикасаться: ночью пост должен остаться в ней."""
    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)


def test_queue_waits_until_morning(monkeypatch):
    queue = _UntouchableQueue()
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda now_local=None: True)
    assert asyncio.run(bot._publish_one_from_queue_locked(MagicMock())) == (None, None)
    assert queue.touched == []


@pytest.mark.parametrize('quiet', [True, False])
def test_thread_to_channel_autopost_waits_too(monkeypatch, quiet):
    pending = MagicMock()
    pending.next_for_autopost.return_value = None
    monkeypatch.setattr(bot, 'pending_posts', pending)
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda now_local=None: quiet)

    async def due(now=None):
        return True
    monkeypatch.setattr(bot, '_channel_autopost_due', due)
    assert asyncio.run(bot._autopost_one_from_thread(MagicMock())) is None
    assert pending.next_for_autopost.called is (not quiet)


def test_status_explains_the_night_pause(monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', '22:00-06:00')
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda now_local=None: True)
    assert 'Ночная тишина канала: 22:00–06:00 (сейчас тихо' in bot._quiet_status_line()
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', 'off')
    assert bot._quiet_status_line() == ''
