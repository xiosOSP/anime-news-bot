"""Время в логе должно совпадать с тем, что показывают /health и /lifecycle.

Контейнер живёт по UTC, а бот считает расписание и диагностику по настройке
`timezone_name` (по умолчанию Europe/Moscow). Из-за этого файл лога выглядел
«отставшим на три часа», хотя был свежим, и события приходилось сравнивать
между разными системами координат.
"""
import logging
from unittest.mock import MagicMock


import anime_news_bot as bot


def _record():
    return logging.LogRecord('test', logging.INFO, 'file.py', 1, 'сообщение', None, None)


class TestLogUsesAdminTimezone:
    def test_timestamp_follows_configured_timezone(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(timezone_name='Europe/Moscow', tz_offset=3))
        moscow = bot._log_formatter().format(_record())
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(timezone_name='UTC', tz_offset=0))
        utc = bot._log_formatter().format(_record())
        assert moscow != utc, 'часовой пояс не влияет на время в логе'

    def test_timezone_is_labelled(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(timezone_name='Europe/Moscow', tz_offset=3))
        line = bot._log_formatter().format(_record())
        assert 'MSK' in line or '+03' in line, f'пояс не подписан: {line}'

    def test_works_before_settings_are_loaded(self, monkeypatch):
        """Первые строки лога пишутся до загрузки настроек."""
        monkeypatch.setattr(bot, 'settings', None)
        line = bot._log_formatter().format(_record())
        assert 'сообщение' in line

    def test_broken_timezone_does_not_break_logging(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(timezone_name='Нет/Такого', tz_offset=3))
        line = bot._log_formatter().format(_record())
        assert 'сообщение' in line

    def test_formatter_is_used_by_setup(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(timezone_name='Europe/Moscow', tz_offset=3))
        bot._setup_logging()
        root = logging.getLogger()
        assert root.handlers, 'обработчики лога не настроены'
        assert any(isinstance(h.formatter, bot._AdminTZFormatter) for h in root.handlers)
