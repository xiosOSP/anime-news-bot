"""Загрузка ролика через yt-dlp в отдельном процессе с общим сроком.

Раньше yt-dlp работал в потоке бота (asyncio.to_thread). У него есть таймаут
соединения и повторы, но нет срока на всю операцию: роликов из сотен
фрагментов хватало, чтобы подготовка поста шла десятки минут, и всё это время
стояла публикация. Отменить поток нельзя — отмена ожидания его не остановит.
Отдельный процесс можно убить вместе с ffmpeg, который запускает yt-dlp, и
удалить недокачанные файлы.

Модуль самодостаточен: дочерний процесс не импортирует бота (31 тыс. строк,
настройки, хранилища) — только этот файл и yt-dlp.
"""
import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Optional


def file_stem(url: str) -> str:
    """Имя файла: читаемый хвост URL + случайный суффикс на КАЖДУЮ загрузку.

    Раньше имя было последними 80 символами URL: у двух длинных ссылок с
    одинаковым концом файл общий, а публикация и диагностика одной и той же
    ссылки одновременно читали и удаляли один и тот же путь.
    """
    tail = re.sub(r'[^\w\-]', '_', url)[-60:]
    return f'{tail}_{uuid.uuid4().hex[:10]}'


def video_meta(info) -> dict:
    """Ширина, высота и длительность ролика из ответа yt-dlp.

    Без них Telegram показывает ролик квадратом и без длительности, а ffprobe
    на хостинге нет — поэтому берём то, что yt-dlp уже знает."""
    if not isinstance(info, dict):
        return {}
    out = {}
    for key in ('width', 'height'):
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 8192:
            out[key] = int(value)
    duration = info.get('duration')
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
            and 0 < duration < 24 * 3600:
        out['duration'] = float(duration)
    return out


def download(url: str, out_dir: Path, stem: str, *, fmt: str, max_mb: int,
             max_duration: int, extensions, ydl_module, ffmpeg: Optional[str] = None,
             meta: Optional[dict] = None) -> tuple[Optional[Path], str]:
    """Скачивает ролик. (путь, пояснение) — путь None, если не вышло.

    meta, если передан, заполняется шириной/высотой/длительностью."""
    ydl_opts = {
        'format': fmt,
        'outtmpl': str(Path(out_dir) / f'{stem}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'max_filesize': max_mb * 1024 * 1024,
        'socket_timeout': 30,
        'retries': 2,
        'fragment_retries': 2,
    }
    if ffmpeg:
        # Склейка дорожек: в mp4, если кодеки туда влезают (Telegram играет его
        # в ленте), иначе в mkv — лучше файл, чем никакого ролика.
        ydl_opts['ffmpeg_location'] = ffmpeg
        ydl_opts['merge_output_format'] = 'mp4/mkv'
    try:
        with ydl_module.YoutubeDL(ydl_opts) as ydl:
            # Сначала extract_info без скачивания — проверяем длину
            info = ydl.extract_info(url, download=False)
            duration = info.get('duration', 0)
            if max_duration > 0 and duration and duration > max_duration:
                return None, f'ролик длиннее лимита ({duration}с)'

            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))
            if not file_path.exists():
                # yt-dlp иногда меняет расширение после конвертации
                for candidate in Path(out_dir).glob(f'{file_path.stem}.*'):
                    if candidate.suffix.lower() in extensions:
                        file_path = candidate
                        break
            if not file_path.exists():
                return None, ('файл после скачивания не найден '
                              '(возможно, нужна склейка дорожек и ffmpeg)')
            size_mb = file_path.stat().st_size / (1024 * 1024)
            if size_mb > max_mb:
                file_path.unlink(missing_ok=True)
                return None, f'файл {size_mb:.0f} МБ больше лимита {max_mb} МБ'
            if meta is not None:
                meta.update(video_meta(info))
            height = info.get('height') if isinstance(info, dict) else None
            quality = f', {height}p' if isinstance(height, int) and height > 0 else ''
            return file_path, f'скачано через yt-dlp, {size_mb:.1f} МБ{quality}'
    except Exception as e:
        return None, f'{type(e).__name__}: {str(e)[:120]}'


def remove_partial(out_dir: Path, stem: str) -> None:
    """Убирает всё, что начала эта загрузка: .part, .ytdl, промежуточные дорожки."""
    for leftover in Path(out_dir).glob(f'{stem}*'):
        with contextlib.suppress(OSError):
            leftover.unlink()


def _kill_group(proc) -> None:
    # Своя группа процессов: убиваем и yt-dlp, и запущенный им ffmpeg.
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if hasattr(os, 'killpg'):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()


async def download_isolated(url: str, out_dir: Path, *, fmt: str, max_mb: int,
                            max_duration: int, extensions, timeout: float,
                            ffmpeg: Optional[str] = None, meta: Optional[dict] = None,
                            ) -> tuple[Optional[Path], str]:
    """Загрузка в дочернем процессе; по истечении timeout процесс убивается."""
    stem = file_stem(url)
    job = {'url': url, 'out_dir': str(out_dir), 'stem': stem, 'fmt': fmt,
           'max_mb': max_mb, 'max_duration': max_duration, 'extensions': list(extensions),
           'ffmpeg': ffmpeg}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=hasattr(os, 'killpg'))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        remove_partial(out_dir, stem)
        return None, f'загрузка дольше {int(timeout)} с — прервана'
    except BaseException:
        # Отмена публикации (остановка бота) — процесс не должен пережить её.
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())
        remove_partial(out_dir, stem)
        raise
    try:
        # Ответ — ровно одна строка JSON: посторонний вывод дочерний процесс
        # уводит в stderr, и всё, что сюда попало сверх ответа, — сбой.
        result = json.loads(out.decode('utf-8', 'replace'))
    except ValueError:
        remove_partial(out_dir, stem)
        return None, f'процесс загрузки завершился без ответа (код {proc.returncode})'
    note = str(result.get('note') or '')
    raw = result.get('path')
    if not raw:
        remove_partial(out_dir, stem)
        return None, note
    path = Path(raw).resolve()
    # Ответ процесса — только подсказка: путь обязан быть файлом этой загрузки.
    if path.parent != Path(out_dir).resolve() or not path.name.startswith(stem) or not path.is_file():
        remove_partial(out_dir, stem)
        return None, 'процесс загрузки вернул чужой путь'
    if meta is not None:
        # Сведения о ролике — тоже лишь подсказка: берём только разумные числа.
        meta.update(video_meta(result.get('meta')))
    return path, note


def _child_main() -> None:
    job = json.loads(sys.stdin.read())
    # Весь посторонний вывод (yt-dlp, ffmpeg) — в stderr; stdout только для
    # ответа. Иначе одна строка прогресса ломала бы разбор результата.
    result_fd = os.dup(1)
    os.dup2(2, 1)
    meta: dict = {}
    try:
        import yt_dlp
    except ImportError:
        path, note = None, 'yt-dlp не установлен'
    else:
        path, note = download(job['url'], Path(job['out_dir']), job['stem'], fmt=job['fmt'],
                              max_mb=int(job['max_mb']), max_duration=int(job['max_duration']),
                              extensions=tuple(job['extensions']), ydl_module=yt_dlp,
                              ffmpeg=job.get('ffmpeg'), meta=meta)
    with os.fdopen(result_fd, 'w', encoding='utf-8') as out:
        out.write(json.dumps({'path': str(path) if path else None, 'note': note,
                              'meta': meta}) + '\n')


if __name__ == '__main__':
    _child_main()
