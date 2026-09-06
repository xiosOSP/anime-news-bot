"""Тесты фильтра свежести."""
from datetime import datetime, timedelta

import pytest

import anime_news_bot
from anime_news_bot import _is_too_old


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    class FakeSettings:
        post_max_age_hours = 36
    monkeypatch.setattr(anime_news_bot, 'settings', FakeSettings())


def utc_struct_hours_ago(hours):
    """Возвращает struct_time для момента N часов назад в UTC."""
    return (datetime.utcnow() - timedelta(hours=hours)).timetuple()


class TestIsTooOld:
    def test_fresh_post_passes(self):
        assert _is_too_old(utc_struct_hours_ago(10)) is False

    def test_old_post_blocked(self):
        assert _is_too_old(utc_struct_hours_ago(48)) is True

    def test_just_under_limit_passes(self):
        # 35 часов — должен пройти при дефолтном 36ч
        assert _is_too_old(utc_struct_hours_ago(35)) is False

    def test_just_over_limit_blocked(self):
        assert _is_too_old(utc_struct_hours_ago(37)) is True

    def test_no_date_passes(self):
        # Без даты — считаем свежим (не блокируем)
        assert _is_too_old(None) is False

    def test_custom_max_age(self):
        # 2 часа > 1 час — старый
        assert _is_too_old(utc_struct_hours_ago(2), max_age_hours=1) is True
        # 0.5 часа < 1 час — свежий
        assert _is_too_old(utc_struct_hours_ago(0), max_age_hours=1) is False

    def test_uses_settings_when_no_arg(self, monkeypatch):
        # Подменяем настройки на 72 часа
        class FS:
            post_max_age_hours = 72
        monkeypatch.setattr(anime_news_bot, 'settings', FS())
        # 50 часов было бы старым при 36, но при 72 — свежее
        assert _is_too_old(utc_struct_hours_ago(50)) is False

    def test_invalid_struct_returns_false(self):
        # Кривая структура — fail-safe
        assert _is_too_old('not a tuple') is False
