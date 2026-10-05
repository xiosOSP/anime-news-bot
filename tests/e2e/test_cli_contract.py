"""Process-boundary contracts that survive internal module refactors."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_cli_fails_closed_without_bot_token(tmp_path):
    env = os.environ.copy()
    env['DATA_DIR'] = str(tmp_path)
    env.pop('BOT_TOKEN', None)
    env['ADMIN_ID'] = '1'
    env['CHANNEL_ID'] = '-1001234567890'
    env['FEATURE_SOURCE_DISCOVERY'] = 'false'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'anime_news_bot.py')],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert 'Токен бота не задан' in combined


def _hosting_smoke():
    spec = importlib.util.spec_from_file_location(
        'hosting_smoke_contract', ROOT / 'tools' / 'hosting_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_host_reaches_polling(tmp_path):
    # Хостинг показывает «Ошибка» при любом падении до опроса Telegram, а
    # импорт модуля и тесты отдельных функций такого падения не видят.
    started = time.monotonic()
    assert _hosting_smoke().bot_starts(tmp_path, timeout=60).startswith('дошёл до polling')
    # Проверка заканчивается на строке опроса, а не ждёт весь срок: бот без
    # Telegram сам не завершится, и каждая задача CI теряла бы минуту.
    assert time.monotonic() - started < 30


def test_startup_crash_is_reported_with_its_cause(tmp_path):
    with pytest.raises(RuntimeError, match='не дошёл до polling') as failure:
        _hosting_smoke().bot_starts(tmp_path, timeout=60,
                                    extra_env={'CHANNEL_ID': 'канал с пробелами'})
    # Причина из лога попадает в сообщение — в CI сразу видно, что чинить.
    assert 'CHANNEL_ID' in str(failure.value)


def test_traceback_before_polling_fails_the_check(tmp_path):
    # Шаг запуска упал, но бот пошёл дальше: на хостинге он работает без
    # части функций, и это тоже поломка.
    (tmp_path / 'sitecustomize.py').write_text(
        'import traceback\n'
        'try:\n'
        '    raise ValueError("сбой при старте")\n'
        'except ValueError:\n'
        '    traceback.print_exc()\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='traceback до запуска polling'):
        _hosting_smoke().bot_starts(tmp_path, timeout=60,
                                    extra_env={'PYTHONPATH': str(tmp_path)})


def test_telegram_errors_after_polling_do_not_fail_the_check():
    # Без сети или с фальшивым токеном ошибки идут уже от Telegram — образ
    # хостинга при этом исправен.
    lines = ['Создаю Application...', '✅ Бот запущен, начинаю polling...',
             'Traceback (most recent call last):', 'telegram.error.NetworkError: нет сети']
    assert _hosting_smoke().startup_verdict(lines, 1.0).startswith('дошёл до polling')
