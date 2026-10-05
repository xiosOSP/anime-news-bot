"""Качество роликов на хостинге без системного ffmpeg/ffprobe.

В образе хостинга нет ffmpeg: YouTube отдавал только 360p, Bilibili и
Niconico (только раздельные дорожки) не скачивались вовсе, а ролики
приходили в Telegram квадратом без длительности и превью — ширину, высоту и
длительность бот не передавал. Теперь ffmpeg едет пакетом imageio-ffmpeg,
размеры берутся из yt-dlp или из «ffmpeg -i».
"""
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import video_download

# Пересказ вывода «ffmpeg -i» для mp4 (h264 + aac) и webm без звука.
FFMPEG_MP4 = textwrap.dedent('''\
    Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'trailer.mp4':
      Duration: 00:01:32.48, start: 0.000000, bitrate: 2190 kb/s
      Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 2000 kb/s, 23.98 fps
      Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 128 kb/s (default)
    At least one output file must be specified
''')
FFMPEG_WEBM = textwrap.dedent('''\
    Input #0, matroska,webm, from 'clip.webm':
      Duration: 00:00:07.00, start: 0.000000, bitrate: 90 kb/s
      Stream #0:0: Video: vp9 (Profile 0), yuv420p(tv, progressive), 640x360, SAR 1:1 DAR 16:9, 30 fps
''')


def test_bundled_ffmpeg_is_used_when_system_one_is_missing(monkeypatch):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: None)
    monkeypatch.setattr(bot, '_bundled_ffmpeg', lambda: '/pkg/ffmpeg-linux-x86_64-v7')
    assert bot._media_tool('ffmpeg') == '/pkg/ffmpeg-linux-x86_64-v7'
    # ffprobe в пакете нет — его подменять бинарником ffmpeg нельзя.
    assert bot._media_tool('ffprobe') is None


def test_system_ffmpeg_wins_over_bundled(monkeypatch):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(bot, '_bundled_ffmpeg', lambda: '/pkg/ffmpeg')
    assert bot._media_tool('ffmpeg') == '/usr/bin/ffmpeg'


def test_format_with_ffmpeg_prefers_h264_aac_within_telegram_limit(monkeypatch):
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/pkg/ffmpeg')
    fmt = bot._video_format()
    first = fmt.split('/')[0]
    assert '[vcodec^=avc1]' in first and '[acodec^=mp4a]' in first
    assert '[filesize<38M]' in first and '[height<=1080]' in first
    # Последняя ступень — «что угодно», иначе ролик без известного размера терялся.
    assert fmt.endswith('/b')


def test_probe_reads_size_and_codecs_from_ffmpeg_output(monkeypatch, tmp_path):
    path = tmp_path / 'trailer.mp4'
    path.write_bytes(b'x' * 10)
    monkeypatch.setattr(bot.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr=FFMPEG_MP4))
    info = bot._probe_with_ffmpeg(path, '/pkg/ffmpeg')
    assert (info['width'], info['height']) == (1920, 1080)
    assert info['duration'] == 92.48
    assert (info['video_codec'], info['audio_codec'], info['pix_fmt']) == ('h264', 'aac', 'yuv420p')
    assert info['container'].startswith('mov,mp4') and info['has_audio']


def test_probe_without_audio_and_without_video(monkeypatch, tmp_path):
    path = tmp_path / 'clip.webm'
    path.write_bytes(b'x')
    monkeypatch.setattr(bot.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr=FFMPEG_WEBM))
    info = bot._probe_with_ffmpeg(path, '/pkg/ffmpeg')
    assert (info['width'], info['height'], info['has_audio']) == (640, 360, False)
    monkeypatch.setattr(bot.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr='Invalid data found'))
    assert bot._probe_with_ffmpeg(path, '/pkg/ffmpeg') is None


def test_probe_falls_back_to_ffmpeg_when_ffprobe_is_missing(monkeypatch, tmp_path):
    path = tmp_path / 'trailer.mp4'
    path.write_bytes(b'x')
    bot.FEATURE_FLAGS['video_probe'] = True
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/pkg/ffmpeg' if name == 'ffmpeg' else None)
    monkeypatch.setattr(bot.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr=FFMPEG_MP4))
    assert bot._probe_video_file(path)['height'] == 1080


def test_dims_kwargs_only_for_sane_values():
    assert bot._video_dims_kwargs({'width': 1280, 'height': 720, 'duration': 91.6}) == {
        'width': 1280, 'height': 720, 'duration': 92}
    assert bot._video_dims_kwargs({'width': 0, 'height': 720, 'duration': 'долго'}) == {}
    assert bot._video_dims_kwargs({'width': 99999, 'height': 99999, 'duration': -1}) == {}
    assert bot._video_dims_kwargs(None) == {}


def test_video_meta_from_ytdlp_drops_garbage():
    assert video_download.video_meta({'width': 1920, 'height': 1080, 'duration': 95}) == {
        'width': 1920, 'height': 1080, 'duration': 95.0}
    assert video_download.video_meta({'width': True, 'height': -5, 'duration': '9'}) == {}
    assert video_download.video_meta('не словарь') == {}


FAKE_YT_DLP = textwrap.dedent('''
    import json, os

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts
            with open(os.environ['FAKE_OPTS_FILE'], 'w') as f:
                json.dump({k: opts.get(k) for k in ('ffmpeg_location', 'merge_output_format')}, f)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if not download:
                return {'duration': 95}
            self.path = self.opts['outtmpl'].replace('%(ext)s', 'mp4')
            open(self.path, 'wb').write(b'video')
            return {'width': 1280, 'height': 720, 'duration': 95.4}

        def prepare_filename(self, info):
            return self.path
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    lib = tmp_path / 'lib'
    lib.mkdir()
    (lib / 'yt_dlp.py').write_text(FAKE_YT_DLP, encoding='utf-8')
    monkeypatch.setenv('PYTHONPATH', str(lib))
    monkeypatch.setenv('FAKE_OPTS_FILE', str(tmp_path / 'opts.json'))
    out = tmp_path / 'videos'
    out.mkdir()
    return out


@pytest.mark.asyncio
async def test_child_merges_with_given_ffmpeg_and_reports_size(fake, tmp_path):
    meta: dict = {}
    path, note = await video_download.download_isolated(
        'https://youtube.com/watch?v=ok', fake, fmt='b', max_mb=48, max_duration=0,
        extensions=('.mp4',), timeout=20.0, ffmpeg='/pkg/ffmpeg-linux', meta=meta)
    assert path is not None and path.is_file()
    assert meta == {'width': 1280, 'height': 720, 'duration': 95.4}
    assert '720p' in note
    opts = json.loads((tmp_path / 'opts.json').read_text())
    assert opts == {'ffmpeg_location': '/pkg/ffmpeg-linux', 'merge_output_format': 'mp4/mkv'}


@pytest.mark.asyncio
async def test_child_without_ffmpeg_does_not_ask_for_merge(fake, tmp_path):
    path, _note = await video_download.download_isolated(
        'https://youtube.com/watch?v=ok', fake, fmt='b', max_mb=48, max_duration=0,
        extensions=('.mp4',), timeout=20.0)
    assert path is not None
    assert json.loads((tmp_path / 'opts.json').read_text()) == {
        'ffmpeg_location': None, 'merge_output_format': None}


@pytest.mark.asyncio
async def test_prepare_uses_ytdlp_size_when_probe_is_impossible(monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True))
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)

    async def download(url, note, meta):
        meta.update(width=1280, height=720, duration=61.0)
        return clip

    monkeypatch.setattr(bot, '_download_video_bounded', download)
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: None)
    monkeypatch.setattr(bot, '_normalize_video_file', lambda p, info: p)
    news = {'video': 'https://youtube.com/watch?v=x'}
    assert await bot._prepare_video_file(news) == clip
    assert bot._video_dims_kwargs(news['_video_meta']) == {
        'width': 1280, 'height': 720, 'duration': 61}


@pytest.mark.asyncio
async def test_telegram_video_bytes_are_sent_with_size(monkeypatch):
    """Ролик из Telegram-канала качается байтами — и тоже уходит с размерами."""
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True, require_image=False))
    monkeypatch.setattr(bot, 'format_news_post', lambda news: 'Вышел трейлер второго сезона.')
    monkeypatch.setattr(bot, '_resolve_video', AsyncMock(return_value=b'\x00\x00video-bytes'))
    monkeypatch.setattr(bot, '_generate_video_thumbnail', lambda path: b'\xff\xd8jpeg')
    seen = []

    def probe(path):
        seen.append(Path(path).read_bytes())
        return {'width': 720, 'height': 1280, 'duration': 14.2}

    monkeypatch.setattr(bot, '_probe_video_file', probe)
    tg = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    news = {'title': 'Трейлер', 'source': 'TG', 'images': [],
            'video': 'https://cdn4.cdn-telegram.org/file/clip.mp4'}
    assert await bot._send_post(tg, news, -100123, None)
    kwargs = tg.send_video.await_args.kwargs
    assert (kwargs['width'], kwargs['height'], kwargs['duration']) == (720, 1280, 14)
    assert kwargs['thumbnail'] == b'\xff\xd8jpeg'
    assert seen == [b'\x00\x00video-bytes']   # мерили именно этот ролик


@pytest.mark.asyncio
async def test_temporary_copy_of_bytes_is_removed(monkeypatch):
    monkeypatch.setattr(bot, '_generate_video_thumbnail', lambda path: None)
    paths = []
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: paths.append(Path(p)) or None)
    assert await bot._video_thumbnail_kwargs_async(None, {}, b'clip') == {}
    assert paths and not paths[0].exists()
