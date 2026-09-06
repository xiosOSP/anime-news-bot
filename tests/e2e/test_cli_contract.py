"""Process-boundary contracts that survive internal module refactors."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
