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

ROOT = Path(__file__).resolve().parent
CHECKER = ROOT / 'tools' / 'check_invariants.py'

# Минимум, ниже которого список не имеет права опуститься: каждая строка — это
# отдельная найденная и закрытая проблема, и удаление проверки вместе с
# регрессией должно ронять сборку, а не проходить незамеченным.
MIN_INVARIANTS = 14


def _load_rows():
    if not CHECKER.exists():
        return None
    spec = importlib.util.spec_from_file_location('check_invariants', CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules['check_invariants'] = module
    spec.loader.exec_module(module)
    tree = ast.parse((ROOT / 'anime_news_bot.py').read_text(encoding='utf-8'))
    return module.checks(bot, tree)


# Собираем на импорте: параметризация должна знать реальную длину списка,
# иначе новые инварианты молча остаются без отдельного теста.
_ROWS = _load_rows()


@pytest.fixture(scope='module')
def rows():
    if _ROWS is None:
        pytest.fail('tools/check_invariants.py пропал вместе с проверками ревью')
    return _ROWS


def test_checker_covers_every_review_finding(rows):
    """Список не должен худеть: каждая строка — отдельная найденная проблема."""
    assert len(rows) >= MIN_INVARIANTS


@pytest.mark.parametrize("index", range(len(_ROWS) if _ROWS else MIN_INVARIANTS))
def test_invariant_holds(rows, index):
    name, ok, why = rows[index]
    assert ok, f'{name} — {why}'
