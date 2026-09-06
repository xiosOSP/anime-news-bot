import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image, ImageDraw

import anime_news_bot as bot


def _jpeg(size=(1280, 720), *, variant=0, quality=88):
    im = Image.new('RGB', size, (30 + variant * 5, 50, 90))
    d = ImageDraw.Draw(im)
    d.rectangle([size[0] * .12, size[1] * .15, size[0] * .88, size[1] * .82],
                fill=(210, 170 + variant, 80))
    d.ellipse([size[0] * .3, size[1] * .25, size[0] * .7, size[1] * .75],
              fill=(40, 100, 210))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=quality)
    return out.getvalue()


@pytest.fixture(autouse=True)
def restore_flags():
    old = dict(bot.FEATURE_FLAGS)
    yield
    bot.FEATURE_FLAGS.clear()
    bot.FEATURE_FLAGS.update(old)
    bot._image_bytes_cache.clear()
    bot._video_thumbnail_cache.clear()


def test_perceptual_flag_can_fall_back_to_exact_md5():
    data = _jpeg((800, 450))
    bot.FEATURE_FLAGS['perceptual_media_dedup'] = False
    assert bot._image_fingerprint(data).startswith('m:')


def test_quality_prefers_large_landscape_over_thumbnail():
    big = bot._image_quality_info(_jpeg((1280, 720)), 'https://cdn/x_full.jpg')
    small = bot._image_quality_info(_jpeg((220, 124)), 'https://cdn/x_thumb.jpg')
    assert big['score'] > small['score'] + 30
    assert big['width'] == 1280 and big['height'] == 720


def test_quality_flags_extreme_aspect():
    info = bot._image_quality_info(_jpeg((1200, 240)), 'https://cdn/wide.jpg')
    assert bot._media_candidate_warning(info).startswith('extreme-aspect:')


def test_optimize_reorders_best_image_first(monkeypatch):
    blobs = {
        'https://cdn/small.jpg': _jpeg((240, 135), variant=1),
        'https://cdn/key.jpg': _jpeg((1280, 720), variant=2),
    }
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: blobs[url])
    news = {'images': list(blobs), 'link': '', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert news['images'][0] == 'https://cdn/key.jpg'
    assert news['_media_primary_score'] >= 70
    assert news['image'] == news['images'][0]


def test_optimize_drops_perceptual_resize_duplicate(monkeypatch):
    original = _jpeg((1280, 720), variant=1)
    with Image.open(io.BytesIO(original)) as im:
        out = io.BytesIO()
        im.resize((640, 360), Image.LANCZOS).save(out, 'JPEG', quality=70)
        resized = out.getvalue()
    other_im = Image.new('RGB', (1280, 720), (210, 40, 60))
    other_draw = ImageDraw.Draw(other_im)
    other_draw.polygon([(0, 0), (1280, 720), (0, 720)], fill=(20, 220, 170))
    other_buf = io.BytesIO(); other_im.save(other_buf, 'JPEG', quality=88)
    blobs = {
        'https://a/full.jpg': original,
        'https://b/recompressed.jpg': resized,
        'https://c/other.jpg': other_buf.getvalue(),
    }
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: blobs[url])
    news = {'images': list(blobs), 'link': '', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert len(news['images']) == 2
    assert 'https://c/other.jpg' in news['images']


def test_optimize_never_drops_only_low_quality_candidate(monkeypatch):
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda _url: _jpeg((180, 100)))
    news = {'images': ['https://cdn/tiny.jpg'], 'link': '', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert news['images'] == ['https://cdn/tiny.jpg']


def test_optimize_fetches_og_when_primary_is_weak(monkeypatch):
    blobs = {
        'https://cdn/tiny.jpg': _jpeg((180, 100)),
        'https://site/key.jpg': _jpeg((1400, 788), variant=4),
    }
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: blobs[url])
    monkeypatch.setattr(bot, 'fetch_og_image', lambda _url: 'https://site/key.jpg')
    news = {'images': ['https://cdn/tiny.jpg'], 'link': 'https://site/article', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert news['images'][0] == 'https://site/key.jpg'


def test_optimize_accepts_legacy_single_image_field(monkeypatch):
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda _url: _jpeg((1000, 563)))
    news = {'image': 'https://cdn/one.jpg', 'images': [], 'link': '', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert news['images'] == ['https://cdn/one.jpg']
    assert news['image'] == 'https://cdn/one.jpg'


def test_media_quality_feature_flag_can_restore_legacy_order(monkeypatch):
    bot.FEATURE_FLAGS['media_quality'] = False
    news = {'images': ['small', 'large'], 'link': '', 'title': 'X'}
    asyncio.run(bot._optimize_news_media(news))
    assert news['images'] == ['small', 'large']


def test_story_cluster_keeps_media_candidates_from_confirmations(monkeypatch):
    monkeypatch.setattr(bot, '_source_reputation_score', lambda source: 0.9 if source == 'Official' else 0.5)
    items = [
        {'title': 'Blue Lock anime gets final trailer', 'source': 'Official', 'link': 'https://official/a',
         'summary': 'Official announcement details', 'images': ['https://official/thumb.jpg']},
        {'title': 'Blue Lock anime gets final trailer', 'source': 'News', 'link': 'https://news/a',
         'summary': 'Coverage', 'images': ['https://news/key-1280x720.jpg']},
    ]
    out = bot._cluster_news(items)
    assert len(out) == 1
    assert out[0]['source'] == 'Official'
    assert 'https://official/thumb.jpg' in out[0]['images']
    assert 'https://news/key-1280x720.jpg' in out[0]['images']


def test_probe_video_file_parses_ffprobe(monkeypatch, tmp_path):
    p = tmp_path / 'v.mp4'; p.write_bytes(b'x' * 123)
    payload = {'format': {'format_name': 'mov,mp4', 'duration': '3.25'}, 'streams': [
        {'codec_type': 'video', 'codec_name': 'h264', 'width': 1280, 'height': 720, 'pix_fmt': 'yuv420p'},
        {'codec_type': 'audio', 'codec_name': 'aac'},
    ]}
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(bot.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    info = bot._probe_video_file(p)
    assert info['video_codec'] == 'h264'
    assert info['audio_codec'] == 'aac'
    assert info['duration'] == 3.25
    assert info['size'] == 123


def test_normalize_reasons_detect_non_telegram_friendly(tmp_path):
    p = tmp_path / 'v.webm'; p.write_bytes(b'x')
    reasons = bot._video_normalize_reasons(p, {
        'container': 'webm', 'video_codec': 'vp9', 'audio_codec': 'opus',
        'has_audio': True, 'width': 1920, 'pix_fmt': 'yuv444p',
    })
    assert {'container', 'video-codec', 'audio-codec', 'width', 'pixel-format'} <= set(reasons)


def test_normalize_is_opt_in(tmp_path):
    p = tmp_path / 'v.avi'; p.write_bytes(b'original')
    bot.FEATURE_FLAGS['video_normalize'] = False
    assert bot._normalize_video_file(p, {'container': 'avi', 'video_codec': 'mpeg4'}) == p
    assert p.read_bytes() == b'original'


def test_normalize_success_replaces_temp_file(monkeypatch, tmp_path):
    p = tmp_path / 'v.avi'; p.write_bytes(b'original' * 100)
    bot.FEATURE_FLAGS['video_normalize'] = True
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/ffmpeg' if name == 'ffmpeg' else None)
    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b'mp4' * 100)
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    monkeypatch.setattr(bot.subprocess, 'run', fake_run)
    out = bot._normalize_video_file(p, {
        'container': 'avi', 'video_codec': 'mpeg4', 'audio_codec': 'mp3',
        'has_audio': True, 'width': 1920, 'pix_fmt': 'yuv420p',
    })
    assert out.suffix == '.mp4'
    assert out.exists()
    assert not p.exists()


def test_video_thumbnail_is_bounded_and_cached(monkeypatch, tmp_path):
    p = tmp_path / 'v.mp4'; p.write_bytes(b'video')
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(bot, '_probe_video_file', lambda _p: {'duration': 4.0})
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b'\xff\xd8' + b'j' * 500)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bot.subprocess, 'run', fake_run)
    first = bot._generate_video_thumbnail(p)
    second = bot._generate_video_thumbnail(p)
    assert first == second and first.startswith(b'\xff\xd8')
    assert len(calls) == 1
    assert 'scale=320:320:force_original_aspect_ratio=decrease' in calls[0]


def test_prepare_video_records_probe_and_normalized_path(monkeypatch, tmp_path):
    src = tmp_path / 'download.webm'; src.write_bytes(b'x')
    dst = tmp_path / 'download.telegram.mp4'; dst.write_bytes(b'y')
    monkeypatch.setattr(bot, 'settings', MagicMock(video_enabled=True))
    monkeypatch.setattr(bot, 'YT_DLP_AVAILABLE', True)
    monkeypatch.setattr(bot, 'download_video', lambda *_: src)
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: {
        'container': p.suffix, 'video_codec': 'h264', 'audio_codec': 'aac',
        'width': 640, 'height': 360, 'pix_fmt': 'yuv420p', 'duration': 2.0,
        'size': p.stat().st_size, 'has_audio': True,
    })
    monkeypatch.setattr(bot, '_normalize_video_file', lambda *_: dst)
    news = {'video': 'https://youtube.com/watch?v=x'}
    got = asyncio.run(bot._prepare_video_file(news))
    assert got == dst
    assert news['_video_meta']['duration'] == 2.0


def test_send_local_video_passes_generated_thumbnail(monkeypatch, tmp_path):
    p = tmp_path / 'v.mp4'; p.write_bytes(b'video')
    monkeypatch.setattr(bot, 'settings', MagicMock(video_enabled=True, require_image=True))
    monkeypatch.setattr(bot, 'format_news_post', lambda _n: 'Caption')
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs', lambda _p: {'thumbnail': b'jpg'})
    tg = MagicMock()
    tg.send_video = AsyncMock()
    news = {'video': 'https://youtube.com/watch?v=x', 'images': [], 'source': 'X', 'title': 'T'}
    ok = asyncio.run(bot._send_post(tg, news, 1, p))
    assert ok is True
    assert tg.send_video.await_args.kwargs['thumbnail'] == b'jpg'


def test_media_command_reports_flags(monkeypatch):
    msg = MagicMock(); msg.reply_text = AsyncMock()
    update = MagicMock(message=msg, effective_user=MagicMock(id=bot.ADMIN_ID))
    context = MagicMock()
    asyncio.run(bot.media_command(update, context))
    text = msg.reply_text.await_args.args[0]
    assert 'Media Quality' in text
    assert 'Video normalize' in text
