"""Regression tests for PR #60 media moderation v2."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import anime_news_bot as bot
import moderation_media as media


def _message(**kwargs):
    values = dict(
        photo=None, video=None, video_note=None, animation=None,
        sticker=None, document=None,
    )
    values.update(kwargs)
    return NS(**values)


def test_large_video_can_use_small_telegram_thumbnail():
    thumb = NS(file_id='thumb-id', file_size=1200)
    video = NS(
        file_id='video-id', file_size=media.MAX_BYTES + 1,
        thumbnail=thumb, duration=12, width=1280, height=720,
    )
    item, kind = media.media_preview_attachment(_message(video=video))
    assert item is thumb
    assert kind == 'image'


@pytest.mark.asyncio
async def test_large_media_without_thumbnail_is_review_not_download():
    telegram = NS(get_file=AsyncMock())
    scanner = media.MediaScanner()
    video = NS(
        file_id='large', file_size=media.MAX_BYTES + 1,
        thumbnail=None, duration=12, width=1280, height=720,
    )
    result = await scanner.check(telegram, _message(video=video))
    assert result.status == 'unchecked'
    assert '20 МБ' in result.reason
    assert 'превью нет' in result.reason
    telegram.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_media_positive_thumbnail_is_only_manual_evidence(monkeypatch):
    scanner = media.MediaScanner()
    thumb = NS(file_id='thumb-id', file_unique_id='thumb-u', file_size=1000)
    video = NS(
        file_id='large', file_size=media.MAX_BYTES + 1,
        thumbnail=thumb, duration=12, width=1280, height=720,
    )
    probe = AsyncMock(return_value=media.Scan(
        'checked', 'spoiler_16', 'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED',
        1, .97,
    ))
    monkeypatch.setattr(scanner, '_check_downloadable', probe)

    telegram = NS()
    result = await scanner.check(telegram, _message(video=video))

    assert result.status == 'unchecked'
    assert result.category == 'spoiler_16'
    assert result.score == .97
    assert 'Оригинал больше 20 МБ' in result.reason
    probe.assert_awaited_once_with(telegram, thumb, 'image')


def test_one_borderline_suggestive_frame_requires_review(tmp_path, monkeypatch):
    path = tmp_path / 'frame.png'
    Image.new('RGB', (16, 16), 'gray').save(path)
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    monkeypatch.setattr(media, '_pillow_frames',
                        lambda _path: [Image.new('RGB', (16, 16), 'gray')])
    monkeypatch.setattr(
        media, 'scan_frame',
        lambda *_args, **_kwargs: media.Scan(
            'checked', 'spoiler_16',
            'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED',
            score=.85,
        ),
    )

    result = media.scan_file(path, 'image', .80, .85)

    assert result.status == 'unchecked'
    assert result.category == ''
    assert result.score == .85
    assert 'Один пограничный кадр 16+' in result.reason


def test_repeated_suggestive_frames_confirm_16_plus(tmp_path, monkeypatch):
    path = tmp_path / 'frame.png'
    Image.new('RGB', (16, 16), 'gray').save(path)
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    monkeypatch.setattr(
        media, '_pillow_frames',
        lambda _path: [
            Image.new('RGB', (16, 16), 'gray'),
            Image.new('RGB', (16, 16), 'gray'),
        ],
    )
    monkeypatch.setattr(
        media, 'scan_frame',
        lambda *_args, **_kwargs: media.Scan(
            'checked', 'spoiler_16',
            'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED',
            score=.86,
        ),
    )

    result = media.scan_file(path, 'image', .80, .85)

    assert result.status == 'checked'
    assert result.category == 'spoiler_16'
    assert result.frames == 2


def test_manual_review_does_not_trip_media_breaker():
    scanner = media.MediaScanner()
    scanner._failures = scanner.failure_limit - 1

    scanner._note_worker_result(media.Scan(
        'unchecked',
        reason='Один пограничный кадр 16+ (0.85); нужна ручная проверка',
    ))

    assert scanner._failures == scanner.failure_limit - 1
    assert scanner.paused_for() == 0


def test_real_detector_failures_still_trip_media_breaker():
    scanner = media.MediaScanner()
    for _ in range(scanner.failure_limit):
        scanner._note_worker_result(media.Scan(
            'unchecked', reason='Ошибка декодирования или детектора: ValueError'
        ))
    assert scanner._failures == scanner.failure_limit
    assert scanner.paused_for() > 0


def test_suggestive_media_never_escalates_to_mute_from_old_warns():
    for warns in (0, 1, 2, 20):
        decision = bot._mod_decide('spoiler_16', severity=2, warns=warns)
        assert decision['action'] == 'warn'
        assert decision['delete'] is True
    assert bot._mod_decide('nsfw', severity=2, warns=0)['action'] == 'mute'
