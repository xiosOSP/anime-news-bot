"""Память контейнера на видео (внутренний аудит, октябрь 2026).

Бота убивали по памяти сразу после загрузки ролика: лимит в 1 ГБ делят бот,
ffmpeg и воркер проверки медиа. ffmpeg по умолчанию берёт поток на ядро.
"""
import subprocess
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot


@pytest.fixture
def ffmpeg_calls(monkeypatch, tmp_path):
    calls = []

    def run(cmd, **_kw):
        calls.append(list(cmd))
        return NS(returncode=1, stdout=b'', stderr=b'')
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _k: True)
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'\0' * 64)
    return NS(calls=calls, video=video, monkeypatch=monkeypatch)


def _option_before_input(cmd, option, value):
    i = cmd.index('-i')
    return any(cmd[j] == option and cmd[j + 1] == value for j in range(i))


def test_thumbnail_decodes_in_one_thread(ffmpeg_calls):
    ffmpeg_calls.monkeypatch.setattr(bot, '_probe_video_file', lambda _p: None)
    bot._video_thumbnail_cache.clear()
    bot._generate_video_thumbnail(ffmpeg_calls.video)
    assert ffmpeg_calls.calls and all(_option_before_input(c, '-threads', '1') for c in ffmpeg_calls.calls)


def test_normalize_encodes_in_two_threads(ffmpeg_calls):
    info = {'container': 'matroska', 'video_codec': 'vp9', 'width': 1920, 'pix_fmt': 'yuv420p'}
    bot._normalize_video_file(ffmpeg_calls.video, info)
    cmd = ffmpeg_calls.calls[-1]
    assert cmd[cmd.index('libx264') + 1:cmd.index('libx264') + 3] == ['-threads', '2']


def test_shrink_encodes_in_two_threads(ffmpeg_calls, monkeypatch):
    big = ffmpeg_calls.video
    big.write_bytes(b'\0' * (bot.VIDEO_MAX_FILE_SIZE_MB * 1024 * 1024 + 1))
    monkeypatch.setattr(bot, '_probe_video_file', lambda _p: {'duration': 60.0})
    bot._shrink_video_to_limit(big)
    cmd = ffmpeg_calls.calls[-1]
    assert cmd[cmd.index('libx264') + 1:cmd.index('libx264') + 3] == ['-threads', '2']


def test_worker_decoder_is_single_threaded():
    import moderation_media as media
    seen = {}

    class Done(Exception):
        pass

    def popen(*_a, env=None, **_k):
        seen.update(env or {})
        raise Done
    import pytest as _pt
    with _pt.MonkeyPatch.context() as m:
        m.setattr(media.subprocess, 'run', popen, raising=False)
        m.setattr(media.subprocess, 'Popen', popen, raising=False)
        try:
            media._invoke_worker('x.mp4', 'video', 1, .8, .85)
        except Done:
            pass
        except Exception:
            pass
    assert 'threads;1' in seen.get('OPENCV_FFMPEG_CAPTURE_OPTIONS', '')
    assert 'protocol_whitelist;file' in seen.get('OPENCV_FFMPEG_CAPTURE_OPTIONS', '')
