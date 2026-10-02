"""Загрузка ролика в отдельном процессе: общий срок, уборка, чужой путь.

Вместо настоящего yt-dlp в дочерний процесс через PYTHONPATH подкладывается
поддельный модуль: поведение задаёт сам URL.
"""
import asyncio
import os
import textwrap
import time

import pytest

import anime_news_bot as bot
import video_download

FAKE_YT_DLP = textwrap.dedent('''
    import os, subprocess, sys, time

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if not download:
                return {'duration': 10}
            out = self.opts['outtmpl'].replace('%(ext)s', 'mp4')
            if 'noisy' in url:
                print('[download] 100% прогресс в stdout')
                os.system('echo ffmpeg тоже пишет в stdout')
            if 'slow' in url:
                open(out + '.part', 'wb').write(b'half')
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                open(os.environ['FAKE_PID_FILE'], 'w').write(str(child.pid))
                time.sleep(60)
            if 'outside' in url:
                self.path = os.environ['FAKE_OUTSIDE']
                return {}
            open(out, 'wb').write(b'video')
            self.path = out
            return {}

        def prepare_filename(self, info):
            return self.path
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    lib = tmp_path / 'lib'
    lib.mkdir()
    (lib / 'yt_dlp.py').write_text(FAKE_YT_DLP, encoding='utf-8')
    monkeypatch.setenv('PYTHONPATH', str(lib))
    monkeypatch.setenv('FAKE_PID_FILE', str(tmp_path / 'pid'))
    outside = tmp_path / 'secret.mp4'
    outside.write_bytes(b'not yours')
    monkeypatch.setenv('FAKE_OUTSIDE', str(outside))
    out = tmp_path / 'videos'
    out.mkdir()
    return out


def alive(pid: int) -> bool:
    """Жив ли процесс. Убитый «внук» может остаться зомби, пока его не
    подберёт init (в контейнере — не всегда), поэтому смотрим состояние."""
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii') as f:
            state = f.read().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return state not in ('Z', 'X')


def run(out, url, timeout=20.0):
    return video_download.download_isolated(
        url, out, fmt='b', max_mb=48, max_duration=0, extensions=('.mp4',), timeout=timeout)


@pytest.mark.asyncio
async def test_successful_download_returns_the_file(fake):
    path, note = await run(fake, 'https://youtube.com/watch?v=ok')
    assert path is not None and path.parent == fake.resolve() and path.read_bytes() == b'video'
    assert note.startswith('скачано через yt-dlp')


@pytest.mark.asyncio
async def test_stray_output_does_not_break_the_answer(fake):
    path, _note = await run(fake, 'https://youtube.com/watch?v=noisy')
    assert path is not None and path.is_file()


@pytest.mark.asyncio
async def test_parallel_downloads_of_one_link_get_separate_files(fake):
    # Публикация и диагностика одного ролика одновременно не делят файл.
    (a, _), (b, _) = await asyncio.gather(run(fake, 'https://youtube.com/watch?v=ok'),
                                          run(fake, 'https://youtube.com/watch?v=ok'))
    assert a and b and a != b and a.is_file() and b.is_file()


@pytest.mark.asyncio
async def test_deadline_kills_the_whole_process_group_and_cleans_up(fake, tmp_path):
    started = time.monotonic()
    path, note = await run(fake, 'https://youtube.com/watch?v=slow', timeout=2.0)
    assert time.monotonic() - started < 10
    assert path is None and 'прервана' in note
    assert list(fake.iterdir()) == []                 # .part убран
    pid = int((tmp_path / 'pid').read_text())
    await asyncio.sleep(0.2)
    assert not alive(pid)                             # ffmpeg-«внук» тоже убит


@pytest.mark.asyncio
async def test_cancelled_publication_does_not_leave_the_download_running(fake, tmp_path):
    task = asyncio.ensure_future(run(fake, 'https://youtube.com/watch?v=slow', timeout=60))
    for _ in range(100):
        if (tmp_path / 'pid').exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not alive(int((tmp_path / 'pid').read_text()))
    assert list(fake.iterdir()) == []


@pytest.mark.asyncio
async def test_path_outside_the_download_dir_is_refused(fake):
    path, note = await run(fake, 'https://youtube.com/watch?v=outside')
    assert path is None and 'чужой путь' in note


# --------------------------------------------------- места для загрузки

@pytest.mark.asyncio
async def test_busy_slots_do_not_hold_the_publication_past_the_deadline(monkeypatch):
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)
    monkeypatch.setattr(bot, 'VIDEO_DOWNLOAD_TIMEOUT_SEC', 0.3)
    monkeypatch.setattr(bot, '_video_download_slots', asyncio.Semaphore(1))
    gate = asyncio.Event()

    async def slow(url, *_a, **_k):
        await gate.wait()
        return None, 'стоп'
    monkeypatch.setattr(bot.video_download, 'download_isolated', slow)
    first = asyncio.ensure_future(bot._download_video_bounded('https://youtube.com/a'))
    await asyncio.sleep(0)
    note = []
    started = time.monotonic()
    assert await bot._download_video_bounded('https://youtube.com/b', note) is None
    assert time.monotonic() - started < 2
    assert 'заняты' in note[0]
    gate.set()
    await first
    # Место освобождается и после неудачи: следующая загрузка идёт сразу.
    assert bot._video_download_slots._value == 1


@pytest.mark.asyncio
async def test_deadline_includes_the_wait_for_a_slot(monkeypatch):
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)
    monkeypatch.setattr(bot, 'VIDEO_DOWNLOAD_TIMEOUT_SEC', 10)
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(bot, '_video_download_slots', slots)
    seen = {}

    async def record(url, *_a, timeout, **_k):
        seen['timeout'] = timeout
        return None, 'стоп'
    monkeypatch.setattr(bot.video_download, 'download_isolated', record)
    await slots.acquire()                       # место занято чужой загрузкой
    asyncio.get_running_loop().call_later(1.0, slots.release)
    await bot._download_video_bounded('https://youtube.com/a')
    assert 8.5 < seen['timeout'] < 9.5
