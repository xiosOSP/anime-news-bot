"""news_stories.py — отдельный модуль без состояния, и таким обязан остаться.

Первый кусок, вынесенный из монолита. Смысл выноса — логику повторов можно
проверить на двух строках без токена бота, сети и диска. Стоит модулю
импортировать anime_news_bot, и граница исчезнет: вместе с ним приедут
окружение, хранилища и Telegram.
"""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_module_does_not_import_the_bot():
    tree = ast.parse((ROOT / 'news_stories.py').read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert 'anime_news_bot' not in imported


def test_it_works_without_bot_environment():
    """Без BOT_TOKEN и DATA_DIR: только стандартная библиотека."""
    code = ("import news_stories as s; "
            "assert s._story_events_conflict('One Piece Manga Goes on Break', "
            "'One Piece Anime Goes on Break')")
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT)}
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-500:]


def test_the_bot_uses_the_module_not_a_copy():
    """В боте не должно остаться второй, расходящейся копии функций."""
    import anime_news_bot as bot
    import news_stories
    assert bot._story_similarity is news_stories._story_similarity
    assert bot._story_events_conflict is news_stories._story_events_conflict
    assert bot.normalize_title is news_stories.normalize_title
