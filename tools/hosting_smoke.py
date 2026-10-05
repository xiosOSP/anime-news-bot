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
import threading
import time
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


POLLING_MARK = 'начинаю polling'
# Закрытый порт вместо прокси: проверка не ходит в настоящий Telegram и
# одинаково ведёт себя в CI, у разработчика и без сети.
DEAD_PROXY = 'http://127.0.0.1:9'


def bot_starts(workdir: Path, timeout: float = 90.0, extra_env: dict | None = None) -> str:
    """Полный запуск, как на свежем хостинге: тома данных ещё нет, токен фальшивый.

    Импорт модуля ещё не значит, что бот запустится. До опроса Telegram main()
    берёт блокировку, переносит схемы данных, читает настройки и ищет ffmpeg;
    падение на любом из этих шагов хостинг показывает как «Ошибка», а тесты
    отдельных функций его не видят. Telegram для проверки не нужен: достаточно
    дойти до строки перед опросом без единого traceback.
    """
    data_dir = workdir / 'fresh-data'
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in ('no_proxy', 'all_proxy')}
    env.update({
        'BOT_TOKEN': '123456:hosting-smoke', 'DATA_DIR': str(data_dir),
        'ADMIN_ID': '1', 'CHANNEL_ID': '-1001234567890', 'PYTHONUNBUFFERED': '1',
        'HTTPS_PROXY': DEAD_PROXY, 'HTTP_PROXY': DEAD_PROXY,
        'https_proxy': DEAD_PROXY, 'http_proxy': DEAD_PROXY,
    })
    env.update(extra_env or {})
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(ROOT / 'anime_news_bot.py')], cwd=str(ROOT),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    lines: list[str] = []
    reached = threading.Event()

    def read() -> None:
        for line in proc.stdout:
            lines.append(line.rstrip('\n'))
            if POLLING_MARK in line:
                reached.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = started + timeout
    while not reached.is_set() and proc.poll() is None and time.monotonic() < deadline:
        reached.wait(0.2)
    elapsed = time.monotonic() - started
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    # Дочитываем вывод до конца: упавший процесс мог успеть напечатать причину.
    reader.join(timeout=5)
    return startup_verdict(lines, elapsed)


def startup_verdict(lines: list[str], elapsed: float) -> str:
    """Решение по выводу запуска; при поломке — RuntimeError с хвостом лога.

    Всё, что после строки опроса, — уже разговор с Telegram: без сети или с
    фальшивым токеном там законно появляются traceback'и, и к образу хостинга
    они отношения не имеют.
    """
    before = []
    for line in lines:
        if POLLING_MARK in line:
            break
        before.append(line)
    else:
        raise RuntimeError(f'бот не дошёл до polling за {elapsed:.0f} с: {" | ".join(lines[-15:])}')
    if any('Traceback' in line for line in before):
        raise RuntimeError(f'traceback до запуска polling: {" | ".join(before[-15:])}')
    return f'дошёл до polling за {elapsed:.1f} с'


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
            check('бот запускается на пустом томе данных', lambda: bot_starts(work)),
        ]
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
