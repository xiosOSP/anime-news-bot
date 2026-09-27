"""Повторяемость находки 16+ по кадрам гифки поднимает уверенность вердикта.

Живой случай: откровенная гифка, BUTTOCKS_EXPOSED 0.85 на лучшем кадре из 16,
автопорог 16+ — 0.92. Уверенностью была оценка одного кадра, поэтому гифка
ушла на ручную оценку, как случайный пограничный кадр.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import anime_news_bot as bot
import moderation_media as media


def _scan_with_hits(tmp_path, monkeypatch, total, hit_frames, near_frames=()):
    """scan_file над total кадрами; hit_frames дают 16+, near_frames — почти 18+."""
    path = tmp_path / 'anim.gif'
    Image.new('RGB', (8, 8), 'gray').save(path)
    frames = [Image.new('RGB', (8, 8), (i, i, i)) for i in range(total)]
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    monkeypatch.setattr(media, '_pillow_frames', lambda _path: frames)

    def fake_scan(_detector, frame, *_args):
        index = frame.getpixel((0, 0))[0]
        near = int(index in near_frames)
        if index in hit_frames:
            return media.Scan('checked', 'spoiler_16',
                              'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED',
                              score=.85, near_explicit=near)
        return media.Scan('checked', near_explicit=near)

    monkeypatch.setattr(media, 'scan_frame', fake_scan)
    return media.scan_file(path, 'animation', .80, .85)


def _state(scan):
    return bot._mod_decision_state('spoiler_16', 'локальный детектор медиа',
                                   confidence=media.media_confidence(scan))[0]


def test_gif_with_finding_on_many_frames_is_auto(tmp_path, monkeypatch):
    scan = _scan_with_hits(tmp_path, monkeypatch, 16, set(range(0, 16, 3)))
    assert (scan.status, scan.category, scan.frames, scan.hits) == ('checked', 'spoiler_16', 16, 6)
    assert scan.score == .85
    assert media.media_confidence(scan) >= bot.MODERATION_MEDIA_AUTO_THRESHOLDS['spoiler_16']
    assert _state(scan) == 'auto'
    assert media.media_evidence(scan) == '16+ на 6 из 16 кадров'


def test_quarter_of_frames_is_the_bar(tmp_path, monkeypatch):
    # 4 из 16 — ровно четверть: достаточно; 3 из 16 — нет.
    assert _state(_scan_with_hits(tmp_path, monkeypatch, 16, {0, 5, 10, 15})) == 'auto'
    scan = _scan_with_hits(tmp_path, monkeypatch, 16, {0, 7, 15})
    assert scan.hits == 3
    assert media.media_confidence(scan) == .85
    assert _state(scan) == 'review'


def test_two_frames_are_not_enough_even_if_all_hit(tmp_path, monkeypatch):
    # Короткая анимация из двух кадров: оба с находкой — всё ещё не повтор.
    assert _state(_scan_with_hits(tmp_path, monkeypatch, 2, {0, 1})) == 'review'
    assert _state(_scan_with_hits(tmp_path, monkeypatch, 3, {0, 1, 2})) == 'auto'


def test_near_explicit_frames_back_two_hits(tmp_path, monkeypatch):
    scan = _scan_with_hits(tmp_path, monkeypatch, 16, {3, 9}, near_frames={3, 12})
    assert (scan.hits, scan.near_explicit) == (2, 2)
    assert _state(scan) == 'auto'
    assert media.media_evidence(scan) == (
        '16+ на 2 из 16 кадров, признаки 18+ чуть ниже порога на 2')
    single = _scan_with_hits(tmp_path, monkeypatch, 16, {3, 9}, near_frames={12})
    assert single.near_explicit == 1
    assert _state(single) == 'review'


def test_only_16_plus_verdicts_are_boosted():
    for scan in (
            media.Scan('checked', 'nsfw', 'x', frames=16, score=.85, hits=16),
            media.Scan('unchecked', 'spoiler_16', 'x', frames=16, score=.85, hits=16),
            media.Scan('checked', '', '', frames=16, score=.0, hits=16)):
        assert media.media_confidence(scan) == scan.score
    # Результат без счётчиков (старый воркер, заглушка) — просто оценка.
    assert media.media_confidence(NS(status='checked', category='spoiler_16', score=.88)) == .88
    assert media.media_evidence(NS(frames=16)) == ''
    # Одна картинка: «на 1 из 1 кадров» админу ничего не говорит.
    assert media.media_evidence(media.Scan('checked', 'spoiler_16', 'x', frames=1,
                                           score=.97, hits=1)) == ''


class _Detector:
    def __init__(self, detections):
        self.detections = detections

    def detect(self, _image):
        return self.detections


@pytest.mark.parametrize('explicit_score, expected', [(.70, 1), (.65, 1), (.64, 0)])
def test_scan_frame_marks_explicit_near_miss(explicit_score, expected):
    detector = _Detector([
        {'class': 'BUTTOCKS_EXPOSED', 'score': .86},
        {'class': 'FEMALE_BREAST_EXPOSED', 'score': explicit_score},
    ])
    result = media.scan_frame(detector, Image.new('RGB', (8, 8)), .80, .85)
    assert result.category == 'spoiler_16'
    assert result.near_explicit == expected


def test_suggestive_near_miss_is_not_explicit_evidence():
    detector = _Detector([{'class': 'BUTTOCKS_EXPOSED', 'score': .80}])
    result = media.scan_frame(detector, Image.new('RGB', (8, 8)), .80, .85)
    assert result.status == 'unchecked' and result.near_explicit == 0


@pytest.mark.asyncio
async def test_handler_deletes_repeated_16_plus_gif(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports'):
        monkeypatch.setattr(bot, name, {})

    async def run(scan, number):
        tg = NS(get_chat_member=AsyncMock(return_value=NS(status='member')),
                delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
                ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
                send_message=AsyncMock(return_value=NS(message_id=900)),
                edit_message_text=AsyncMock())
        monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=scan)))
        msg = NS(chat_id=-100, message_id=number, text=None, caption=None, sender_chat=None,
                 reply_to_message=None, media_group_id=None, has_media_spoiler=False,
                 from_user=NS(id=50 + number, full_name='Участник', is_bot=False),
                 animation=NS(file_unique_id=f'g{number}', file_id='f', mime_type='video/mp4'))
        await bot.moderation_message_handler(
            NS(effective_message=msg, effective_user=msg.from_user,
               effective_chat=NS(id=-100), edited_message=None), NS(bot=tg))
        return tg

    reason = 'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED'
    tg = await run(media.Scan('checked', 'spoiler_16', reason, 16, .85, hits=8), 1)
    tg.delete_message.assert_awaited_once_with(-100, 1)
    tg.restrict_chat_member.assert_not_awaited()
    tg.ban_chat_member.assert_not_awaited()
    assert store.warn_count(-100, 51) == 1
    report = '\n'.join(str(c.args[1]) for c in tg.send_message.call_args_list)
    assert '16+ на 8 из 16 кадров' in report

    tg = await run(media.Scan('checked', 'spoiler_16', reason, 16, .85, hits=2), 2)
    tg.delete_message.assert_not_awaited()
    assert store.warn_count(-100, 52) == 0


def test_one_hit_is_not_backed_by_near_explicit_frames():
    scan = media.Scan('checked', 'spoiler_16', 'x', frames=16, score=.86, hits=1, near_explicit=3)
    assert media.media_confidence(scan) == .86
