"""Вынесенные из монолита модули — без состояния, и такими обязаны остаться.

Смысл выноса — логику можно проверить на двух строках без токена бота, сети и
диска. Стоит модулю импортировать anime_news_bot, и граница исчезнет: вместе
с ним приедут окружение, хранилища и Telegram. И в боте не должно остаться
второй, расходящейся копии перенесённых функций.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MODULES = {
    'news_stories': ("assert s._story_events_conflict('One Piece Manga Goes on Break', "
                     "'One Piece Anime Goes on Break')",
                     ('_story_similarity', '_story_events_conflict', 'normalize_title')),
    'post_text': ("assert s._extract_sentences('Сериал выйдет... Подробности позже') == ''",
                  ('_extract_sentences', 'smart_truncate', '_tg_title_and_summary')),
}


@pytest.mark.parametrize('module', sorted(MODULES))
def test_the_module_does_not_import_the_bot(module):
    tree = ast.parse((ROOT / f'{module}.py').read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert 'anime_news_bot' not in imported


@pytest.mark.parametrize('module', sorted(MODULES))
def test_it_works_without_bot_environment(module):
    """Без BOT_TOKEN и DATA_DIR: только стандартная библиотека."""
    code = f'import {module} as s; {MODULES[module][0]}'
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT)}
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-500:]


@pytest.mark.parametrize('module', sorted(MODULES))
def test_the_bot_uses_the_module_not_a_copy(module):
    import importlib
    import anime_news_bot as bot
    mod = importlib.import_module(module)
    for name in MODULES[module][1]:
        assert getattr(bot, name) is getattr(mod, name), name
