"""Сторож тишины и ночная пауза канала (октябрь 2026).

Тишина канала 22:40–08:00 длиннее порога тревоги (8 ч): каждое утро админ
получал «🔇 Тревога: 8 ч без единой публикации», хотя бот просто ждал утра.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot

MSK = timezone(timedelta(hours=3))


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', '22:40-08:00')
    monkeypatch.setattr(bot, '_admin_tz', lambda: MSK)


def _msk(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(timezone.utc)


def test_night_is_not_counted(quiet):
    # Последний пост в 22:30, утро 08:30: из 10 часов активных — 50 минут.
    hours = bot._quiet_overlap_hours(_msk(21, 22, 30), _msk(22, 8, 30))
    assert hours == pytest.approx(9 + 20 / 60)


def test_day_without_night_is_counted_fully(quiet):
    assert bot._quiet_overlap_hours(_msk(22, 9), _msk(22, 19)) == 0


def test_two_nights_are_both_excluded(quiet):
    hours = bot._quiet_overlap_hours(_msk(20, 12), _msk(22, 12))
    assert hours == pytest.approx(2 * (9 + 20 / 60))


def test_no_quiet_window_means_no_overlap(monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', '')
    assert bot._quiet_overlap_hours(_msk(21, 22), _msk(22, 9)) == 0


def _run_watchdog(monkeypatch, silence_hours, quiet_hours):
    monkeypatch.setattr(bot, 'CHANNEL_QUIET_HOURS', '')
    monkeypatch.setattr(bot, '_silence_hours', lambda: silence_hours)
    monkeypatch.setattr(bot, '_quiet_overlap_hours', lambda *_a: quiet_hours)
    monkeypatch.setattr(bot, 'SOURCES', [])
    monkeypatch.setattr(bot, 'settings', MagicMock(is_source_enabled=lambda n: True))
    monkeypatch.setattr(bot, 'scheduled_posts', None)
    monkeypatch.setattr(bot, 'pending_posts', None)
    monkeypatch.setattr(bot, '_silence_reported', False)
    monkeypatch.setattr(bot, '_silence_retry_at', 0)
    notify = AsyncMock(return_value=1)
    monkeypatch.setattr(bot, 'notify_admin', notify)
    asyncio.run(bot._check_silence(MagicMock()))
    return notify


def test_morning_after_quiet_night_is_not_an_alarm(monkeypatch):
    assert _run_watchdog(monkeypatch, 10.0, 9 + 20 / 60).await_count == 0


def test_real_daytime_silence_still_alarms(monkeypatch):
    notify = _run_watchdog(monkeypatch, 18.0, 9 + 20 / 60)
    assert notify.await_count == 1
    assert '8 ч без единой публикации' in notify.await_args.args[1]


def test_night_that_began_the_day_before(quiet):
    """Пост вышел в 01:00 (ручной, админом), ночь началась ещё вчера в 22:40."""
    assert bot._quiet_overlap_hours(_msk(22, 1), _msk(22, 9)) == pytest.approx(7)
