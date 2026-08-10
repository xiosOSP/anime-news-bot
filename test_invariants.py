"""Инварианты ревью проверяются обычным прогоном pytest.

Отдельный скрипт `tools/check_invariants.py` удобен глазами, но его легко
забыть запустить. Здесь тот же список подключён к тестовому набору, чтобы
пропажа механизма ломала сборку сама.
"""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

import anime_news_bot as bot

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / 'tools' / 'check_invariants.py'


def _checker():
    spec = importlib.util.spec_from_file_location('check_invariants', CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules['check_invariants'] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def rows():
    if not CHECKER.exists():
        pytest.fail('tools/check_invariants.py пропал вместе с проверками ревью')
    module = _checker()
    tree = ast.parse((ROOT / 'anime_news_bot.py').read_text(encoding='utf-8'))
    return module.checks(bot, tree)


def test_checker_covers_every_review_finding(rows):
    """Список не должен худеть: каждая строка — отдельная найденная проблема."""
    assert len(rows) >= 14


@pytest.mark.parametrize("index", range(14))
def test_invariant_holds(rows, index):
    name, ok, why = rows[index]
    assert ok, f'{name} — {why}'
