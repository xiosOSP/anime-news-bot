"""Качество фото в постах.

На живом канале 6% фото бота были меньше 700 px (у админов — 1%): превью
ролика из Telegram-канала 320×180, когда сам ролик не доехал, и og:image-
миниатюры 600×315. Плюс «апгрейд» ссылки до оригинала иногда давал битый
адрес, и картинка пропадала из поста целиком.
"""
import io
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import anime_news_bot as bot

FRAME = bot.VIDEO_FRAME_SUFFIX
TG_VIDEO = 'https://cdn4.cdn-telegram.org/file/clip.mp4'


def jpeg(width, height, noisy=True) -> bytes:
    im = Image.effect_noise((width, height), 90).convert('RGB') if noisy \
        else Image.new('RGB', (width, height), (0, 0, 0))
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=90)
    return out.getvalue()


@pytest.fixture(autouse=True)
def clean_caches():
    bot._image_bytes_cache.clear()
    bot._IMAGE_URL_FALLBACKS.clear()
    yield
    bot._image_bytes_cache.clear()
    bot._IMAGE_URL_FALLBACKS.clear()


# ---------------------------------------------------------------- адреса

def test_ann_thumbnail_becomes_original_image():
    url = 'https://www.animenewsnetwork.com/thumbnails/crop600x315gIG/cms/news.6/219191/kv.jpg'
    assert bot.upgrade_image_url(url) == \
        'https://www.animenewsnetwork.com/images/cms/news.6/219191/kv.jpg'


def test_generic_thumbnail_rule_keeps_size_segment_urls():
    # /thumbnails/300x200/… без сегмента-размера не существует — не трогаем.
    assert bot.upgrade_image_url('https://site.example/thumbnails/300x200/pic.jpg') == \
        'https://site.example/thumbnails/300x200/pic.jpg'
    assert bot.upgrade_image_url('https://site.example/thumbs/pic.jpg') == \
        'https://site.example/pic.jpg'


def test_youtube_preview_becomes_maxres():
    assert bot.upgrade_image_url('https://i.ytimg.com/vi/AbCdEfGhIjK/hqdefault.jpg') == \
        'https://i.ytimg.com/vi/AbCdEfGhIjK/maxresdefault.jpg'


def test_twitter_media_gets_large_variant():
    assert bot.upgrade_image_url('https://pbs.twimg.com/media/Gx1.jpg?format=jpg&name=small') == \
        'https://pbs.twimg.com/media/Gx1.jpg?format=jpg&name=large'
    assert bot.upgrade_image_url('https://pbs.twimg.com/media/Gx1.jpg:small') == \
        'https://pbs.twimg.com/media/Gx1.jpg?name=large'


@pytest.mark.asyncio
async def test_broken_upgrade_falls_back_to_working_original(monkeypatch):
    original = 'https://i.ytimg.com/vi/AbCdEfGhIjK/hqdefault.jpg'
    upgraded = bot._upgraded_image(original)
    assert upgraded.endswith('maxresdefault.jpg')
    data = jpeg(480, 360)
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: data if url == original else None)
    assert await bot._probe_image_candidate(upgraded) == (original, data)


# ------------------------------------------------------------ отбор картинок

@pytest.mark.asyncio
async def test_dead_candidate_never_beats_a_real_picture(monkeypatch):
    real = 'https://news.example/small.jpg'
    dead = 'https://news.example/gone.jpg'
    data = jpeg(600, 315)
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: data if url == real else None)
    monkeypatch.setattr(bot, 'fetch_og_image', lambda link: None)
    news = {'images': [dead, real], 'link': 'https://news.example/story'}
    await bot._optimize_news_media(news)
    assert news['images'] == [real]


@pytest.mark.asyncio
async def test_full_size_frame_replaces_blurry_preview(monkeypatch):
    preview = 'https://cdn4.cdn-telegram.org/file/preview.jpg'
    pictures = {TG_VIDEO + FRAME: jpeg(1280, 720), preview: jpeg(320, 180)}
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: pictures.get(url))
    monkeypatch.setattr(bot, '_video_frame_bytes', lambda url: pictures[url + FRAME])
    monkeypatch.setattr(bot, 'fetch_og_image', lambda link: None)
    news = {'images': [TG_VIDEO + FRAME, preview], '_video_thumb': preview,
            'link': 'https://t.me/ch/1'}
    await bot._optimize_news_media(news)
    assert news['images'] == [TG_VIDEO + FRAME]


@pytest.mark.asyncio
async def test_failed_frame_is_not_sent_as_a_link(monkeypatch):
    monkeypatch.setattr(bot, '_video_frame_bytes', lambda url: None)
    assert await bot._resolve_photos_for_album([TG_VIDEO + FRAME]) == []


def test_cache_builds_frame_instead_of_downloading_the_video(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, '_video_frame_bytes', lambda url: calls.append(url) or b'jpeg')
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: pytest.fail('качать ролик нельзя'))
    assert bot._cached_image_bytes(TG_VIDEO + FRAME) == b'jpeg'
    assert calls == [TG_VIDEO]


# --------------------------------------------------- кадр из начала ролика

def ffmpeg():
    exe = bot._media_tool('ffmpeg')
    if not exe:
        pytest.skip('ffmpeg недоступен')
    return exe


def make_clip(tmp_path, black_seconds=0.0) -> bytes:
    """Ролик: сначала чёрный экран, потом картинка — как у многих трейлеров."""
    path = tmp_path / 'clip.mp4'
    video = ('color=c=black:size=1280x720:rate=24:duration=%s[a];'
             'testsrc=size=1280x720:rate=24:duration=10[b];[a][b]concat=n=2:v=1:a=0'
             % (black_seconds or 0.01))
    subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-filter_complex', video,
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    str(path)], check=True, timeout=120)
    return path.read_bytes()


def test_frame_is_full_size_from_the_head_of_the_file(monkeypatch, tmp_path):
    clip = make_clip(tmp_path)
    asked = []

    def head(url, limit):
        asked.append(limit)
        return clip[:limit]

    monkeypatch.setattr(bot, '_download_head_bytes', head)
    data = bot._video_frame_bytes(TG_VIDEO)
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (1280, 720)
    assert asked == [bot.VIDEO_FRAME_HEAD_BYTES]


def test_black_opening_is_skipped(monkeypatch, tmp_path):
    clip = make_clip(tmp_path, black_seconds=3)
    monkeypatch.setattr(bot, '_download_head_bytes', lambda url, limit: clip[:limit])
    data = bot._video_frame_bytes(TG_VIDEO)
    assert bot._image_quality_info(data)['entropy'] >= 4.0


def test_frame_only_from_telegram_mp4(monkeypatch):
    ffmpeg()
    monkeypatch.setattr(bot, '_download_head_bytes',
                        lambda url, limit: b'#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n')
    monkeypatch.setattr(bot.subprocess, 'run', lambda *a, **k: pytest.fail('ffmpeg на не-mp4'))
    # Плейлист под видом ролика до ffmpeg даже не доходит.
    assert bot._video_frame_bytes(TG_VIDEO) is None
    monkeypatch.setattr(bot, '_download_head_bytes', lambda url, limit: pytest.fail('чужой хост'))
    assert bot._video_frame_bytes('https://evil.example/clip.mp4') is None


def test_ffmpeg_is_told_the_input_is_mp4(monkeypatch, tmp_path):
    # Явный формат: даже файл с подписью mp4 не разберётся как плейлист.
    clip = make_clip(tmp_path)
    monkeypatch.setattr(bot, '_download_head_bytes', lambda url, limit: clip[:limit])
    real_run = subprocess.run
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(bot.subprocess, 'run', run)
    assert bot._video_frame_bytes(TG_VIDEO)
    first = commands[0]
    assert first[first.index('-f') + 1] == 'mp4' and first.index('-f') < first.index('-i')


# ------------------------------------------- обложка, если ролик не доехал

@pytest.mark.asyncio
async def test_cover_prefers_frame_and_falls_back_to_preview(monkeypatch):
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/pkg/ffmpeg')
    news = {'video': TG_VIDEO, '_video_thumb': 'https://cdn4.cdn-telegram.org/file/p.jpg'}
    monkeypatch.setattr(bot, '_video_frame_bytes', lambda url: b'frame')
    assert await bot._video_cover(news) == TG_VIDEO + FRAME
    bot._image_bytes_cache.clear()
    monkeypatch.setattr(bot, '_video_frame_bytes', lambda url: None)
    assert await bot._video_cover(news) == news['_video_thumb']


@pytest.mark.asyncio
async def test_youtube_cover_uses_maxres_or_falls_back(monkeypatch):
    pictures = {'https://i.ytimg.com/vi/AbCdEfGhIjK/maxresdefault.jpg': jpeg(1280, 720)}
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: pictures.get(url))
    news = {'video': 'https://www.youtube.com/watch?v=AbCdEfGhIjK', 'images': []}
    await bot._ensure_youtube_cover(news)
    assert news['_video_thumb'].endswith('/maxresdefault.jpg')

    bot._image_bytes_cache.clear()
    pictures.clear()
    pictures['https://i.ytimg.com/vi/AbCdEfGhIjK/hqdefault.jpg'] = jpeg(480, 360)
    news = {'video': 'https://youtu.be/AbCdEfGhIjK', 'images': []}
    await bot._ensure_youtube_cover(news)
    assert news['_video_thumb'].endswith('/hqdefault.jpg')

    # У новости свои картинки — заставка не нужна.
    news = {'video': 'https://youtu.be/AbCdEfGhIjK', 'images': ['https://news.example/kv.jpg']}
    await bot._ensure_youtube_cover(news)
    assert '_video_thumb' not in news


@pytest.mark.asyncio
async def test_downloaded_video_is_not_doubled_by_its_preview(monkeypatch, tmp_path):
    """Ролик скачан — превью того же ролика в альбом не добавляется."""
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True, require_image=False))
    monkeypatch.setattr(bot, 'format_news_post', lambda news: 'Вышел трейлер второго сезона.')
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs_async', AsyncMock(return_value={}))
    tg = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                         send_media_group=AsyncMock(), send_photo=AsyncMock())
    news = {'title': 'Трейлер', 'source': 'YT', 'images': [],
            'video': 'https://youtu.be/AbCdEfGhIjK',
            '_video_thumb': 'https://i.ytimg.com/vi/AbCdEfGhIjK/maxresdefault.jpg'}
    assert await bot._send_post(tg, news, -100123, clip)
    tg.send_video.assert_awaited_once()
    tg.send_media_group.assert_not_awaited()


# ------------------------------------------------- чёрное превью вместо ролика

def dark_card(width, height, bright_box, text=False) -> bytes:
    """Чёрный кадр со светлой областью: надписью (шумная, как буквы) или логотипом."""
    from PIL import ImageDraw
    im = Image.new('RGB', (width, height), (0, 0, 0))
    if text:
        # Тёмный шум кадра (ярче 31 не бывает) и сглаженные края букв дают
        # настоящему титру энтропию около 1,8 — светлее он от этого не становится.
        im = Image.effect_noise((width, height), 64).point(lambda v: v // 8).convert('RGB')
        left, top, right, bottom = bright_box
        letters = Image.effect_noise((right - left, bottom - top), 120).convert('RGB')
        im.paste(letters, (left, top))
    else:
        ImageDraw.Draw(im).rectangle(bright_box, fill=(230, 230, 230))
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=90)
    return out.getvalue()


# Первый кадр трейлера, который Telegram не отдал («Media is too big»): чёрный
# фон и мелкая надпись — примерно 3% площади, как в посте о «Красной Шапочке».
# Надпись не сплошная, поэтому кадр не «однородный» — отсеять его должно
# именно правило обложки ролика.
TITLE_CARD = dark_card(320, 180, (60, 84, 260, 94), text=True)
# Логотип на чёрном фоне: тёмный, но это настоящая иллюстрация (~9% площади).
LOGO = dark_card(800, 800, (100, 330, 700, 420))


async def _optimized(monkeypatch, images, pictures, **news):
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: pictures.get(url))
    monkeypatch.setattr(bot, 'fetch_og_image', lambda link: None)
    news = {'images': images, 'link': 'https://t.me/ch/1', **news}
    await bot._optimize_news_media(news)
    return news


@pytest.mark.asyncio
async def test_black_title_card_of_a_missing_video_is_not_posted(monkeypatch):
    thumb = 'https://cdn4.cdn-telegram.org/file/thumb.jpg'
    news = await _optimized(monkeypatch, [thumb], {thumb: TITLE_CARD},
                            _video_thumb=thumb, _thumb_only=True)
    assert news['images'] == []
    # Иначе отправка вернула бы ту же обложку как «кадр из поста».
    assert news['_video_thumb'] is None


def test_title_card_is_not_merely_uniform():
    info = bot._image_quality_info(TITLE_CARD, 'x')
    assert info['entropy'] >= 1.0 and info['bright_share'] < 0.05


@pytest.mark.asyncio
async def test_same_dark_picture_stays_when_it_is_not_a_video_cover(monkeypatch):
    # Тёмная картинка с мелкой деталью у обычной новости — иллюстрация.
    art = 'https://news.example/night.jpg'
    news = await _optimized(monkeypatch, [art], {art: TITLE_CARD})
    assert news['images'] == [art]


@pytest.mark.asyncio
async def test_dark_logo_picture_stays(monkeypatch):
    logo = 'https://news.example/logo.jpg'
    news = await _optimized(monkeypatch, [logo], {logo: LOGO})
    assert news['images'] == [logo]


@pytest.mark.asyncio
async def test_video_thumb_with_a_real_picture_stays(monkeypatch):
    thumb = 'https://cdn4.cdn-telegram.org/file/thumb.jpg'
    news = await _optimized(monkeypatch, [thumb], {thumb: jpeg(320, 180)}, _video_thumb=thumb)
    assert news['images'] == [thumb] and news['_video_thumb'] == thumb


@pytest.mark.asyncio
async def test_solid_fill_is_never_a_picture(monkeypatch):
    solid = 'https://news.example/placeholder.jpg'
    real = 'https://news.example/art.jpg'
    news = await _optimized(monkeypatch, [solid, real],
                            {solid: jpeg(1280, 720, noisy=False), real: jpeg(600, 315)})
    assert news['images'] == [real]


def test_post_about_an_unavailable_video_yields_to_one_with_the_video():
    base = {'title': 'Трейлер аниме по новелле', 'source': 'TG: Ch', 'images': ['x']}
    with_video = dict(base, video='https://cdn4.cdn-telegram.org/file/clip.mp4')
    cover_only = dict(base, _thumb_only=True)
    plain = dict(base)
    assert bot._news_priority_score(cover_only) == pytest.approx(bot._news_priority_score(plain) - 4.0)
    assert bot._news_priority_score(with_video) > bot._news_priority_score(cover_only)
