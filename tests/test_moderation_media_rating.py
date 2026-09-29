"""Второе мнение для рисованного: классификаторы deepghs/anime_rating.

Живые случаи: аниме-видео с грудью в бикини крупным планом — NudeNet не нашёл
ничего; фото девушки в бикини — только «грудь в одежде», которую бот не
учитывал; гифка в стрингах — пограничные 0.80. Классификаторы рисунка на
ординарных картинках ошибаются (~0.7% даже вдвоём), поэтому санкция — только
когда они подтверждают находку NudeNet, иначе — ручная оценка.
"""
import hashlib
import io
import os
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import numpy as np
import pytest
from PIL import Image

import anime_news_bot as bot
import moderation_media as media

PATHS = ('big.onnx', 'small.onnx')
FRAMES = [Image.new('RGB', (8, 8))] * 3


def opinion(monkeypatch, result, flag, covered=0, frames=FRAMES, review=None):
    calls = []

    def fake_rate(pool, paths):
        calls.append(len(pool))
        if review is not None:
            return (.45, .9, 3, 0, review)
        return (.999, .9, 3, 3, 3) if flag else (.4, .2, 3, 0, 0)
    monkeypatch.setattr(media, 'rate_frames', fake_rate)
    return media.second_opinion(result, frames, covered, paths=PATHS), calls


def test_detector_finding_confirmed_by_the_classifiers_is_automatic(monkeypatch):
    scan, _ = opinion(monkeypatch, media.Scan('checked', 'spoiler_16', 'x', 16, .86, hits=1), True)
    assert scan.agreed and media.media_confidence(scan) == media.MEDIA_REPEAT_CONFIDENCE
    state = bot._mod_decision_state('spoiler_16', bot.MODERATION_MEDIA_SOURCE,
                                    confidence=media.media_confidence(scan))
    assert state[0] == 'auto'


def test_borderline_nudenet_plus_classifiers_becomes_a_verdict(monkeypatch):
    borderline = media.Scan('unchecked', reason='Пограничная оценка наготы', frames=16,
                            score=.80, borderline=True)
    scan, _ = opinion(monkeypatch, borderline, True)
    assert (scan.status, scan.category, scan.agreed, scan.borderline) == (
        'checked', 'spoiler_16', True, False)
    assert 'подтверждена' in scan.reason and '1.00/0.90' in scan.reason
    # Без классификаторов пограничная находка так и остаётся ручной оценкой.
    scan, _ = opinion(monkeypatch, borderline, False)
    assert scan.status == 'unchecked' and scan.borderline and not scan.agreed


def test_covered_close_up_needs_the_classifiers_and_repeats_in_animation(monkeypatch):
    photo = media.Scan('checked', frames=1)
    scan, _ = opinion(monkeypatch, photo, True, covered=1)
    assert scan.category == 'spoiler_16' and scan.agreed and scan.score == .70
    assert 'крупным планом' in scan.reason
    scan, _ = opinion(monkeypatch, photo, False, covered=1)
    assert scan.category == '' and scan.status == 'checked'
    # В гифке одного такого кадра мало: это уже ручная оценка, а не санкция.
    scan, _ = opinion(monkeypatch, media.Scan('checked', frames=16), True, covered=1)
    assert scan.status == 'unchecked' and scan.borderline
    scan, _ = opinion(monkeypatch, media.Scan('checked', frames=16), True, covered=2)
    assert scan.agreed


def test_classifiers_alone_only_ask_a_human(monkeypatch):
    scan, _ = opinion(monkeypatch, media.Scan('checked', frames=1), True)
    assert (scan.status, scan.category, scan.borderline, scan.near_explicit) == (
        'unchecked', '', True, 0)
    assert scan.score == .999 and 'нужна ручная проверка' in scan.reason
    # В обработчике это отчёт на ручную оценку, а не санкция.
    assert bot._mod_decision_state('spoiler_16', bot.MODERATION_MEDIA_SOURCE,
                                   confidence=scan.score, needs_review=True)[0] == 'review'
    assert 'классификатор рисунка: 16+ на 3 из 3 кадров' in media.media_evidence(scan)


def test_clean_media_keeps_its_verdict_and_records_the_rating(monkeypatch):
    scan, calls = opinion(monkeypatch, media.Scan('checked', frames=1), False)
    assert (scan.status, scan.category, scan.rated, scan.rating) == ('checked', '', 3, .4)
    assert calls == [3]


def test_explicit_nudity_and_missing_models_skip_the_classifiers(monkeypatch):
    scan, calls = opinion(monkeypatch, media.Scan('checked', 'nsfw', 'x', 1, .9), True)
    assert scan.category == 'nsfw' and calls == [] and not scan.agreed
    monkeypatch.setattr(media, 'rating_model_paths', lambda: ())
    monkeypatch.setattr(media, 'rate_frames', lambda *a: pytest.fail('модели нет'))
    scan = media.second_opinion(media.Scan('checked', frames=1), FRAMES, 1)
    assert scan.covered == 1 and scan.rated == 0


@pytest.mark.parametrize('rated, hits, flag', [(3, 2, True), (3, 1, False), (1, 1, True),
                                               (2, 1, True), (0, 0, False)])
def test_half_of_rated_frames_must_agree(rated, hits, flag):
    assert media.rating_flag(rated, hits) is flag


class Session:
    """Заглушка onnxruntime: отдаёт заранее заданный ответ, запоминает вход."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.inputs = []

    def get_inputs(self):
        return [NS(name='input')]

    def run(self, _names, feed):
        self.inputs.append(feed['input'])
        return [np.asarray([self.outputs.pop(0)], dtype=np.float32)]


def test_rate_frame_preprocessing_and_probabilities():
    session = Session([[.02, .9, .08], [0.0, 5.0, 0.0]])
    assert media.rate_frame(session, Image.new('RGB', (50, 30), 'white')) == pytest.approx(.98)
    data = session.inputs[0]
    assert data.shape == (1, 3, 384, 384) and data.dtype == np.float32
    assert data.max() == pytest.approx(1.0)  # белый → +1 после (x/255 − .5)/.5
    # Логиты тоже понимаем: softmax(0, 5, 0) — «safe» почти ноль.
    assert media.rate_frame(session, Image.new('RGB', (8, 8))) == pytest.approx(
        1 - 1 / (2 + np.exp(5)), abs=1e-4)
    assert session.inputs[1].min() == pytest.approx(-1.0)  # чёрный → −1


def test_rate_frames_counts_frames_where_both_models_agree(monkeypatch):
    big = Session([[.005, .9, .095], [.011, .9, .089], [.009, .5, .491]])
    small = Session([[.25, .7, .05], [.1, .9, 0], [.26, .7, .04]])
    sessions = iter([big, small])
    monkeypatch.setattr(media, '_rating_session', lambda path: next(sessions))
    rating, small_rating, rated, hits, review_hits = media.rate_frames(
        [Image.new('RGB', (8, 8))] * 10, PATHS)
    assert rated == 3 and hits == 1  # .995/.75 да; .989 мало; .991/.74 мало
    # Для ручной оценки: .989/.9 (малая уверена) — да; .991/.74 — большая
    # от .99 при малой от .4 — тоже да.
    assert review_hits == 3
    assert rating == pytest.approx(.995) and small_rating == pytest.approx(.9)
    assert media.rate_frames([], PATHS) == (0.0, 0.0, 0, 0, 0)


def detector_with(*items):
    return NS(detect=lambda _image: list(items))


@pytest.mark.parametrize('score, box, covered', [
    (.72, [0, 0, 50, 40], 1),   # 2000 / 10000 = ровно пятая часть кадра
    (.72, [0, 0, 49, 40], 0),
    (.69, [0, 0, 100, 100], 0),
])
def test_covered_close_up_signal(score, box, covered):
    frame = Image.new('RGB', (100, 100))
    result = media.scan_frame(detector_with(
        {'class': 'FEMALE_BREAST_COVERED', 'score': score, 'box': box}), frame)
    assert result.covered == covered and result.category == ''


def test_scan_file_passes_frames_and_covered_count(tmp_path, monkeypatch):
    path = tmp_path / 'a.gif'
    Image.new('RGB', (8, 8)).save(path)
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    pattern = Image.effect_mandelbrot((40, 20), (-2, -1, 1, 1), 60).convert('RGB')
    monkeypatch.setattr(media, '_pillow_frames', lambda _p: [pattern] * 4)
    marks = iter([1, 0, 1, 0])
    monkeypatch.setattr(media, 'scan_frame', lambda *a: media.Scan('checked', covered=next(marks)))
    seen = {}

    def fake_opinion(result, frames, covered):
        seen.update(frames=len(frames), size=frames[0].size, covered=covered,
                    bilinear=frames[0].tobytes() == pattern.resize((384, 384), Image.BILINEAR).tobytes())
        return result
    monkeypatch.setattr(media, 'second_opinion', fake_opinion)
    media.scan_file(path, 'animation', .8, .85)
    # Уменьшение — как при обучении моделей (BILINEAR), иначе оценки плывут.
    assert seen == dict(frames=4, size=(384, 384), covered=2, bilinear=True)


def test_model_paths_come_from_the_worker_environment(tmp_path, monkeypatch):
    big, small = tmp_path / 'b.onnx', tmp_path / 's.onnx'
    big.write_bytes(b'x')
    small.write_bytes(b'y')
    monkeypatch.setenv(media.RATING_ENV, f'{big}{os.pathsep}{small}')
    assert media.rating_model_paths() == (big, small)
    small.unlink()
    assert media.rating_model_paths() == ()
    monkeypatch.delenv(media.RATING_ENV)
    assert media.rating_model_paths() == ()


def test_worker_gets_model_paths_only_when_loaded(monkeypatch):
    seen = []
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: seen.append(k['env'])
                        or subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setenv(media.RATING_ENV, 'чужое')
    media._invoke_worker('x', 'image', 5, .8, .85, 2048, ('a', 'b'))
    media._invoke_worker('x', 'image', 5, .8, .85, 2048)
    assert seen[0][media.RATING_ENV] == f'a{os.pathsep}b'
    assert media.RATING_ENV not in seen[1]


def fake_models(monkeypatch, content=b'model-bytes'):
    monkeypatch.setattr(media, 'RATING_MODELS', {
        'one': dict(url='https://example.invalid/one', sha256=hashlib.sha256(content).hexdigest(),
                    size=len(content), file='one.onnx')})


def test_models_download_once_and_are_verified(tmp_path, monkeypatch):
    fake_models(monkeypatch)
    opened = []

    def opener(request, timeout):
        opened.append(request.full_url)
        return io.BytesIO(b'model-bytes')
    paths = media.ensure_rating_models(tmp_path, opener=opener)
    assert paths == (tmp_path / 'one.onnx',) and paths[0].read_bytes() == b'model-bytes'
    assert media.ensure_rating_models(tmp_path, opener=opener) == paths
    assert len(opened) == 1


@pytest.mark.parametrize('payload', [b'model-bytez', b'model', b'model-bytes-and-more'])
def test_tampered_or_truncated_model_is_rejected(tmp_path, monkeypatch, payload):
    fake_models(monkeypatch)
    result = media.ensure_rating_models(tmp_path, opener=lambda r, timeout: io.BytesIO(payload))
    assert isinstance(result, str) and result.startswith('one:')
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_bot_job_enables_the_models_or_reports_why_not(monkeypatch):
    scanner = media.MediaScanner()
    monkeypatch.setattr(bot, '_moderation_media_scanner', scanner)
    monkeypatch.setattr(bot, 'ensure_rating_models', lambda directory: 'сеть недоступна')
    await bot.media_rating_models_job(NS())
    assert scanner.rating_paths == () and 'сеть недоступна' in scanner.rating_status
    monkeypatch.setattr(bot, 'ensure_rating_models', lambda directory: ('a', 'b'))
    await bot.media_rating_models_job(NS())
    assert scanner.rating_paths == ('a', 'b') and scanner.rating_status == 'включены'
    monkeypatch.setattr(bot, 'ensure_rating_models', lambda directory: pytest.fail('уже есть'))
    await bot.media_rating_models_job(NS())


@pytest.mark.asyncio
async def test_scanner_hands_model_paths_to_the_worker(monkeypatch, tmp_path):
    scanner = media.MediaScanner()
    scanner.rating_paths = ('a', 'b')
    captured = {}

    def fake_run_worker(*args):
        captured['args'] = args
        return media.Scan('checked')
    monkeypatch.setattr(media, 'run_worker', fake_run_worker)

    class File:
        file_size = 10

        async def download_to_drive(self, custom_path, **_):
            custom_path.write_bytes(b'x' * 10)
    telegram = NS(get_file=AsyncMock(return_value=File()))
    await scanner._check_downloadable(telegram, NS(file_unique_id='u', file_id='f'), 'image')
    assert captured['args'][-1] == ('a', 'b')


def test_media_commands_stay_admin_only():
    # Новая функция однажды встала между @admin_only и /mediaping — команда
    # осталась бы открытой всем. Проверяем обёртку, а не порядок строк.
    for command in (bot.mediaping_command, bot.modunblock_command, bot.modmiss_command):
        assert getattr(command, '__wrapped__', None) is not None, command.__name__
    assert not hasattr(bot.media_rating_models_job, '__wrapped__')


@pytest.mark.asyncio
async def test_mediaping_probes_with_the_models_and_shows_their_state(monkeypatch):
    scanner = media.MediaScanner()
    scanner.rating_paths, scanner.rating_status = ('a', 'b'), 'включены'
    monkeypatch.setattr(bot, '_moderation_media_scanner', scanner)
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    probed = []
    monkeypatch.setattr(bot, 'media_probe', lambda timeout, memory_mb, rating_paths: probed.append(
        rating_paths) or (media.Scan('checked'), '', 1.0))
    edit = AsyncMock()
    message = NS(reply_text=AsyncMock(return_value=NS(edit_text=edit)))
    await bot.mediaping_command.__wrapped__(NS(message=message), NS(args=[]))
    assert probed == [('a', 'b')]
    assert 'Классификаторы рисунка (второе мнение для аниме): включены' in edit.await_args.args[0]



@pytest.mark.parametrize('big, small, hit', [
    (.999, .76, True),    # обе уверены
    (.48, .851, True),    # стикер из чата: малая уверена, большая сомневается
    (.34, .93, True),     # тот же стикер при другом сжатии
    (.29, .95, False),    # большая уверена, что безопасно
    (.997, .44, True),    # косплей в белье: большая уверена, малая сомневается
    (.997, .39, False),
    (.98, .79, False),    # ни одна не уверена
])
def test_review_hit_needs_one_confident_model(big, small, hit):
    assert media.review_hit(big, small) is hit


def test_one_confident_model_asks_a_human_but_never_punishes(monkeypatch):
    scan, _ = opinion(monkeypatch, media.Scan('checked', frames=1), False, review=2)
    assert (scan.status, scan.category, scan.borderline, scan.agreed) == ('unchecked', '', True, False)
    assert 'одной из моделей' in scan.reason and scan.rating_review_hits == 2
    # Вердикт NudeNet при этом не трогаем: он решает по своим правилам.
    nudenet = media.Scan('checked', 'spoiler_16', 'x', 16, .86, hits=1)
    scan, _ = opinion(monkeypatch, nudenet, False, review=3)
    assert scan.category == 'spoiler_16' and not scan.agreed and scan.status == 'checked'
    # Меньше половины кадров — не повод.
    scan, _ = opinion(monkeypatch, media.Scan('checked', frames=16), False, review=1)
    assert scan.status == 'checked'
