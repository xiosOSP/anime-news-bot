#!/usr/bin/env python3
"""Проверка образа «как на хостинге»: всё ли, что поставил requirements.txt, работает.

CI запускает её в чистом python:3.x-slim после ``pip install -r
requirements.txt``, выполненного в папке, где кроме requirements.txt ничего
нет (так собирает образ Bothost). Ubuntu-раннер CI богаче slim-образа
системными библиотеками: opencv, onnxruntime или rlottie могут поставиться и
там, и там, а загрузиться — только на раннере. Поэтому здесь каждый тяжёлый
модуль не просто импортируется, а делает настоящую работу.

Запуск из корня репозитория::

    python tools/hosting_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(title: str, fn) -> bool:
    try:
        detail = fn()
    except Exception as e:  # смысл проверки — увидеть любую поломку
        print(f'❌ {title}: {type(e).__name__}: {e}')
        return False
    print(f'✅ {title}' + (f': {detail}' if detail else ''))
    return True


def nudenet_detects(workdir: Path) -> str:
    from PIL import Image
    from nudenet import NudeDetector
    path = workdir / 'blank.jpg'
    Image.new('RGB', (320, 240), (200, 150, 120)).save(path)
    found = NudeDetector().detect(str(path))
    return f'детектор отработал, находок: {len(found)}'


def ffmpeg_runs() -> str:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([exe, '-hide_banner', '-version'], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    return out.splitlines()[0][:60]


def deno_runs() -> str:
    exe = Path(sysconfig.get_path('scripts')) / 'deno'
    out = subprocess.run([str(exe), '--version'], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return out.splitlines()[0]


def rlottie_renders() -> str:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(
        '{"v":"5.5.2","fr":30,"ip":0,"op":30,"w":64,"h":64,"layers":[]}')
    width, height = anim.render_pillow_frame(0).size
    return f'кадр {width}×{height}'


def bot_imports(workdir: Path) -> str:
    os.environ.setdefault('BOT_TOKEN', '123:hosting-smoke')
    os.environ.setdefault('DATA_DIR', str(workdir / 'data'))
    sys.path.insert(0, str(ROOT))
    import anime_news_bot
    return anime_news_bot._runtime_versions_line()


def main() -> int:
    print(f'Python {sys.version.split()[0]}')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        results = [
            check('numpy, opencv, onnxruntime, Pillow', lambda: __import__('cv2').__version__),
            check('NudeNet (фильтр 18+)', lambda: nudenet_detects(work)),
            check('rlottie (анимированные стикеры)', rlottie_renders),
            check('ffmpeg из imageio-ffmpeg', ffmpeg_runs),
            check('deno для yt-dlp', deno_runs),
            check('бот импортируется', lambda: bot_imports(work)),
        ]
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
