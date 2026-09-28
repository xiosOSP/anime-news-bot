"""Offline nudity checks in a disposable, time-limited decoder process.

No media is sent to an external classifier. A negative sampled result is not
proof that every video frame is safe. Errors are explicitly 'unchecked'.
"""
import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
import gzip
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import timedelta

MAX_BYTES = 20 * 1024 * 1024
# Потолок АДРЕСНОГО ПРОСТРАНСТВА воркера, а не памяти: резидентно детектор занимает
# около 110 МБ на любой машине, а вот адресного пространства просит по-разному —
# замерено 515 МБ на одной четырёхъядерной машине и больше 1536 МБ на другой.
# Зависит от числа ядер, версии onnxruntime и аллокатора, поэтому единственный
# надёжный способ узнать своё число — измерить: это делает /mediaping.
#
# Значение по умолчанию с запасом. Слишком тесный лимит превращает КАЖДУЮ
# проверку в отказ — это хуже, чем страховка, которая не сработала: от
# зависшего воркера всё равно спасает таймаут, а от выключенной проверки
# не спасает ничто.
WORKER_MEMORY_MB_MIN = 1024
WORKER_MEMORY_MB_DEFAULT = 2048
WORKER_MEMORY_ENV = 'MEDIA_WORKER_MEMORY_MB'
MAX_PIXELS = 16_000_000
MAX_FRAMES = 16
MAX_DURATION = 180
EXPLICIT = {'FEMALE_GENITALIA_EXPOSED', 'MALE_GENITALIA_EXPOSED',
            'ANUS_EXPOSED', 'FEMALE_BREAST_EXPOSED'}
SUGGESTIVE = {'BUTTOCKS_EXPOSED', 'FEMALE_GENITALIA_COVERED', 'ANUS_COVERED'}


@dataclass(frozen=True)
class Scan:
    status: str  # checked, unchecked
    category: str = ''  # nsfw / spoiler_16 / no detected nudity
    reason: str = ''
    frames: int = 0
    score: float = 0.0
    # Сколько адресного пространства понадобилось воркеру. Нужен не ради
    # любопытства: на разных машинах аппетит разный, и подбирать лимит вслепую
    # значит менять число наугад после каждого отказа.
    address_space_mb: int = 0
    # Звать ли человека. Чистое превью длинного или тяжёлого видео — «не
    # проверено целиком», но смотреть его руками админу не на что: письмо
    # приходило на каждый такой ролик в чате. Запись в журнал остаётся.
    review: bool = True
    # Виноват сам файл, а не детектор: не медиа, битый, пустой. Такой исход
    # не должен выключать проверку. Раньше три файла субтитров .srt подряд
    # считались тремя отказами детектора и ставили его на паузу на 10 минут —
    # всё, что присылали в это время, оставалось непроверенным.
    bad_input: bool = False
    # Сколько проверенных кадров показали 16+, и на скольких рядом был
    # откровенный признак чуть ниже порога 18+. Оценка детектора — это
    # максимум по одному кадру, и по ней гифка, где ягодицы видны в каждом
    # кадре, выглядела так же, как случайный кадр на пороге: 0.85 при
    # автопороге 0.92, санкции нет. Повторяемость — другое свидетельство,
    # и без этих счётчиков оно терялось.
    hits: int = 0
    near_explicit: int = 0
    # Пограничная находка — не сбой детектора, а вопрос к человеку: такой
    # результат идёт админу отчётом с кнопками, а не техническим письмом,
    # которое прячется на 15 минут после первого.
    borderline: bool = False
    # Отпечатки проверенных кадров (frame_hash). По ним чёрный список узнаёт
    # ту же гифку, пересжатую или загруженную заново под другим file_id.
    hashes: tuple = ()
    # Второе мнение — классификаторы рисунка (safe / r15 / r18). NudeNet
    # обучен на фото людей: рисованную грудь в бикини он не видит вовсе
    # (ноль находок на живом примере из чата). rating — наибольшая доля «не
    # safe» у большой модели по проверенным кадрам, rating_small — у малой;
    # rated — сколько кадров оценено, rating_hits — сколько из них обе модели
    # сочли 16+. agreed — находку NudeNet подтвердил классификатор рисунка.
    rating: float = 0.0
    rating_small: float = 0.0
    rated: int = 0
    rating_hits: int = 0
    agreed: bool = False
    # Кадры, где крупным планом грудь в одежде (FEMALE_BREAST_COVERED, рамка
    # от пятой части кадра): так выглядит фото «декольте на весь экран».
    covered: int = 0


def _address_space_peak_mb():
    """Пик адресного пространства процесса. Ноль — если платформа не говорит."""
    try:
        with open('/proc/self/status', encoding='utf-8') as status:
            for line in status:
                if line.startswith('VmPeak:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def classify_detections(detections, explicit_threshold=.80, suggestive_threshold=.85):
    """Bare feet, bellies, male chests and ordinary swimwear are not 18+."""
    result = Scan('checked')
    for detection in detections:
        label = str(detection.get('class', ''))
        try:
            score = float(detection.get('score', 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score):
            continue
        if label in EXPLICIT and score >= explicit_threshold:
            if result.category != 'nsfw' or score > result.score:
                result = Scan('checked', 'nsfw', 'Обнаружена явная нагота: ' + label, score=score)
        elif label in SUGGESTIVE and score >= suggestive_threshold and result.category != 'nsfw':
            result = Scan('checked', 'spoiler_16', 'Откровенный контент требует спойлера: ' + label, score=score)
    return result


# Уверенность вердикта 16+ по гифке или видео. Автопорог для 16+ — 0.92,
# а NudeNet почти никогда не даёт ягодицам больше 0.9: по одному кадру
# категория фактически не доходила до санкции никогда. Ложные срабатывания,
# из-за которых порог подняли, были одиночными кадрами на самом пороге. Когда
# та же находка держится на четверти кадров и не меньше чем на трёх, или
# дважды подкреплена откровенным признаком чуть ниже порога 18+, — это уже
# не случайный кадр.
MEDIA_REPEAT_MIN_FRAMES = 3
MEDIA_REPEAT_SHARE = .25
MEDIA_REPEAT_CONFIDENCE = .93


def media_confidence(scan) -> float:
    """Уверенность вердикта: оценка лучшего кадра, поднятая повторяемостью."""
    # getattr: в тестах и старых воркерах результат может прийти без счётчиков.
    try:
        score = float(getattr(scan, 'score', 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0
    if getattr(scan, 'category', '') != 'spoiler_16' or getattr(scan, 'status', '') != 'checked':
        return score
    if getattr(scan, 'agreed', False):
        # Два независимых детектора — NudeNet и классификатор рисунка —
        # согласны: это уже не случайный кадр на пороге.
        return max(score, MEDIA_REPEAT_CONFIDENCE)
    hits = int(getattr(scan, 'hits', 0) or 0)
    repeated = hits >= max(MEDIA_REPEAT_MIN_FRAMES,
                           math.ceil(int(getattr(scan, 'frames', 0) or 0) * MEDIA_REPEAT_SHARE))
    backed = hits >= 2 and int(getattr(scan, 'near_explicit', 0) or 0) >= 2
    return max(score, MEDIA_REPEAT_CONFIDENCE) if repeated or backed else score


def media_evidence(scan) -> str:
    """Сколько кадров показали находку — чтобы админ видел, на чём вердикт."""
    frames = int(getattr(scan, 'frames', 0) or 0)
    hits = int(getattr(scan, 'hits', 0) or 0)
    parts = []
    if frames >= 2 and hits:
        text = f'16+ на {hits} из {frames} кадров'
        near = int(getattr(scan, 'near_explicit', 0) or 0)
        if near:
            text += f', признаки 18+ чуть ниже порога на {near}'
        parts.append(text)
    rated = int(getattr(scan, 'rated', 0) or 0)
    if rated:
        parts.append(f'классификатор рисунка: 16+ на {int(getattr(scan, "rating_hits", 0) or 0)} '
                     f'из {rated} кадров (оценки {float(getattr(scan, "rating", 0) or 0):.2f}/'
                     f'{float(getattr(scan, "rating_small", 0) or 0):.2f})')
    return '; '.join(parts)


# Отпечаток кадра — разностный хэш 16×16 (256 бит). Замерено на кадре живой
# гифки из чата: пересжатие JPEG q30, уменьшение вчетверо, размытие и +15%
# яркости дают расстояние 1–3 бита; разные картинки — от 40 и выше, кроме
# почти одинаковых скриншотов интерфейса Telegram (8–30). Порог 20 ловит
# копии и не ловит соседние кадры других сцен. Обрезку хэш не переживает —
# это сознательно: иначе растёт риск удалить чужую безобидную картинку.
HASH_SIZE = 16
HASH_MATCH_BITS = 20


def frame_hash(frame):
    """Отпечаток кадра или None, если в кадре нет рисунка.

    Чёрный кадр затемнения, заливка или плавный градиент дают почти
    одинаковый хэш у совершенно разных гифок — по ним «совпадало» бы всё.
    """
    from PIL import Image
    gray = frame.convert('L').resize((HASH_SIZE + 1, HASH_SIZE), Image.BILINEAR)
    px = gray.tobytes()
    mean = sum(px) / len(px)
    spread = (sum((v - mean) ** 2 for v in px) / len(px)) ** .5
    bits = 0
    for row in range(HASH_SIZE):
        base = row * (HASH_SIZE + 1)
        for col in range(HASH_SIZE):
            bits = (bits << 1) | (px[base + col] > px[base + col + 1])
    ones = bits.bit_count()
    total = HASH_SIZE * HASH_SIZE
    if spread < 12 or not total * .15 <= ones <= total * .85:
        return None
    return f'{bits:0{total // 4}x}'


def parse_hashes(values):
    """Отпечатки числами. Уже разобранные (int) проходят как есть."""
    if isinstance(values, str):
        values = (values,)
    parsed = []
    for value in values or ():
        if isinstance(value, int):
            parsed.append(value)
            continue
        try:
            parsed.append(int(str(value), 16))
        except ValueError:
            continue
    return parsed


def hashes_match(candidate, known) -> bool:
    """Та же картинка или гифка: достаточно кадров близки к известным.

    Картинка с картинкой — по одному кадру. Всё, где есть гифка, — только по
    двум близким кадрам: один общий кадр бывает и у разных роликов (заставка,
    титр), а кадр безобидной сцены из нарезки не должен делать запрещённой
    любую картинку с этой сценой.
    """
    ours, theirs = parse_hashes(candidate), parse_hashes(known)
    if not ours or not theirs:
        return False
    need = 1 if len(ours) == 1 and len(theirs) == 1 else 2
    close = 0
    for value in ours:
        if any((value ^ other).bit_count() <= HASH_MATCH_BITS for other in theirs):
            close += 1
            if close >= need:
                return True
    return False


# Классификаторы рисунка deepghs/anime_rating (MIT, бесплатные, ONNX): три
# класса safe / r15 / r18. Проверено на 687 обычных картинках из аниме-каналов
# (постеры, кадры, новости): у большой модели «не safe» от 0.99 — у 24 картинок,
# и среди них голый торс мужчины, одетый персонаж, съёмочная площадка. Даже
# вместе с малой моделью (от 0.75) ошибок около 0.7%. Поэтому одни
# классификаторы санкцию не дают — только отчёт на ручную оценку. Санкция —
# когда они подтверждают находку NudeNet: такой пары на 687 картинках не
# было ни разу, а все три живых примера из чата она ловит.
RATING_MODELS = {
    'caformer': dict(
        url='https://huggingface.co/deepghs/anime_rating/resolve/'
            '46be80bfe01a415efa5aa7c025528cac82a8b88a/caformer_s36_plus/model.onnx',
        sha256='fd5fdea9a8b610aa26a513df7e2e588a3fa0f641ba3408e5c67501663f501287',
        size=149582900, file='anime_rating_caformer_s36_plus.onnx'),
    'mobilenet': dict(
        url='https://huggingface.co/deepghs/anime_rating/resolve/'
            '46be80bfe01a415efa5aa7c025528cac82a8b88a/mobilenetv3_v1_pruned_ls0.1/model.onnx',
        sha256='76e5c44704421e2b6431a2dbb395f8943e205e59f415a5614b16f0601b55544d',
        size=16827558, file='anime_rating_mobilenetv3.onnx'),
}
RATING_ENV = 'MEDIA_RATING_MODELS'
RATING_SIZE = 384
RATING_FRAMES = 3
RATING_MIN = .99        # большая модель: доля «не safe»
RATING_SMALL_MIN = .75  # малая модель
COVERED_CLOSEUP_SCORE = .70
COVERED_CLOSEUP_SHARE = .20


def ensure_rating_models(directory, *, timeout=600, opener=None):
    """Скачать классификаторы рисунка один раз; вернуть пути или текст ошибки.

    Файлы фиксированы по ревизии и SHA-256: подменённая или недокачанная
    модель не запустится — проверка работает без второго мнения, как раньше.
    """
    import hashlib
    import urllib.request
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f'папка моделей недоступна: {exc}'
    opener = opener or urllib.request.urlopen
    paths = []
    for name, spec in RATING_MODELS.items():
        target = directory / spec['file']
        if target.is_file() and target.stat().st_size == spec['size']:
            paths.append(target)
            continue
        partial = target.with_name(target.name + '.part')
        digest = hashlib.sha256()
        received = 0
        try:
            request = urllib.request.Request(spec['url'], headers={'User-Agent': 'anime-news-bot'})
            with opener(request, timeout=60) as response, open(partial, 'wb') as out:
                started = time.monotonic()
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > spec['size'] or time.monotonic() - started > timeout:
                        raise ValueError('размер или время загрузки вне ожидаемого')
                    digest.update(chunk)
                    out.write(chunk)
            if received != spec['size'] or digest.hexdigest() != spec['sha256']:
                raise ValueError('контрольная сумма не совпала')
            partial.replace(target)
        except Exception as exc:  # сеть, диск, подмена — второе мнение просто не включится
            try:
                partial.unlink()
            except OSError:
                pass
            return f'{name}: {type(exc).__name__}: {exc}'[:200]
        paths.append(target)
    return tuple(paths)


def rating_model_paths():
    """Пути к моделям из окружения воркера; пусто — второго мнения нет."""
    raw = os.environ.get(RATING_ENV, '')
    paths = [Path(part) for part in raw.split(os.pathsep) if part]
    if len(paths) != 2 or not all(path.is_file() for path in paths):
        return ()
    return tuple(paths)


def _rating_session(path):
    import onnxruntime
    options = onnxruntime.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return onnxruntime.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])


def rate_frame(session, frame) -> float:
    """Доля «не safe» (r15 + r18) у классификатора рисунка."""
    import numpy as np
    from PIL import Image
    image = frame.convert('RGB').resize((RATING_SIZE, RATING_SIZE), Image.BILINEAR)
    data = ((np.asarray(image, dtype=np.float32).transpose(2, 0, 1) / 255.0 - .5) / .5)[None]
    output = np.asarray(session.run(None, {session.get_inputs()[0].name: data})[0][0],
                        dtype=np.float64)
    if not (output.min() >= 0 and abs(output.sum() - 1) < 1e-3):
        output = np.exp(output - output.max())
        output /= output.sum()
    return float(max(0.0, min(1.0, 1.0 - output[0])))


def rate_frames(frames, paths):
    """(rating, rating_small, rated, hits) по нескольким кадрам из выборки."""
    if not frames or not paths:
        return 0.0, 0.0, 0, 0
    picked = [frames[index] for index in sample_indices(len(frames), RATING_FRAMES)]
    big, small = (_rating_session(path) for path in paths)
    best = best_small = 0.0
    hits = 0
    for frame in picked:
        value, value_small = rate_frame(big, frame), rate_frame(small, frame)
        best, best_small = max(best, value), max(best_small, value_small)
        hits += int(value >= RATING_MIN and value_small >= RATING_SMALL_MIN)
    return best, best_small, len(picked), hits


def rating_flag(rated: int, hits: int) -> bool:
    """Классификаторы сочли 16+ хотя бы половину оценённых кадров."""
    return rated > 0 and hits * 2 >= rated


# Документы, которые Telegram не показывает картинкой: субтитры, тексты,
# архивы. Проверять в них нечего, а письмо админу на каждый .srt — шум.
NON_MEDIA_SUFFIXES = frozenset({
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.txt', '.md', '.pdf', '.doc', '.docx',
    '.xls', '.xlsx', '.csv', '.json', '.xml', '.zip', '.rar', '.7z', '.tar', '.gz',
    '.torrent', '.epub', '.fb2', '.apk', '.exe', '.py', '.log',
})


def _non_media_document(item) -> bool:
    """Вызывается после проверок на картинку и видео — их MIME сюда не доходит."""
    mime = str(getattr(item, 'mime_type', '') or '').lower()
    suffix = Path(str(getattr(item, 'file_name', '') or '')).suffix.lower()
    return mime.startswith('text/') or suffix in NON_MEDIA_SUFFIXES


def media_attachment(message):
    """Choose original content, never judge a video by its thumbnail."""
    for attr in ('sticker', 'animation', 'video', 'video_note', 'photo', 'document'):
        item = getattr(message, attr, None)
        if not item:
            continue
        if attr == 'photo':
            return item[-1], 'image'
        if attr == 'sticker':
            return item, ('tgs' if getattr(item, 'is_animated', False) else
                          'video' if getattr(item, 'is_video', False) else 'image')
        if attr in ('video', 'video_note'):
            return item, 'video'
        if attr == 'animation':
            return item, 'animation'
        mime = str(getattr(item, 'mime_type', '') or '').lower()
        suffix = Path(str(getattr(item, 'file_name', '') or '')).suffix.lower()
        if mime.startswith('image/') or suffix in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
            return item, 'image'
        if mime.startswith('video/') or suffix in ('.mp4', '.webm', '.mov', '.mkv'):
            return item, 'video'
        if suffix == '.tgs' or mime == 'application/x-tgsticker':
            return item, 'tgs'
        if _non_media_document(item):
            return None
        # Documents can hide media under a false name/MIME; sniff in worker.
        return item, 'unknown'
    return None


def media_preview_attachment(message):
    """Return a small Telegram thumbnail when the original cannot be downloaded.

    A clean thumbnail never proves that the whole video/document is safe, but
    a positive detection is still useful evidence for manual review. Telegram's
    public Bot API cannot download many originals above ~20 MB, while their
    thumbnails remain available.
    """
    attachment = media_attachment(message)
    if attachment is None:
        return None
    item, _kind = attachment
    for attr in ('thumbnail', 'thumb'):
        preview = getattr(item, attr, None)
        if preview is not None and getattr(preview, 'file_id', None):
            return preview, 'image'
    return None


def sample_indices(count, limit=MAX_FRAMES):
    count = int(count)
    if count <= 0:
        raise ValueError('empty animation')
    return sorted({round(i * (count - 1) / max(1, min(count, limit) - 1))
                   for i in range(min(count, limit))})


def _pillow_frames(path):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(path) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('image dimensions exceed limit')
        count = getattr(image, 'n_frames', 1)
        if count > 6000:
            raise ValueError('animation too long')
        for index in sample_indices(count):
            image.seek(index)
            frame = ImageOps.exif_transpose(image).convert('RGBA')
            frame.thumbnail((1280, 1280))
            background = Image.new('RGBA', frame.size, 'white')
            background.alpha_composite(frame)
            yield background.convert('RGB')


def _tgs_frames(path):
    from rlottie_python import LottieAnimation
    with gzip.open(path, 'rb') as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('TGS expanded size exceeds limit')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('invalid TGS')
    assets = data.get('assets', [])
    if not isinstance(assets, list) or len(assets) > 500 or any(
            not isinstance(asset, dict) or 'p' in asset or 'u' in asset for asset in assets):
        # Vector precompositions are allowed; file/URL image assets are not.
        raise ValueError('TGS external/image assets are unsupported')
    if data.get('fonts') or data.get('chars'):
        raise ValueError('TGS fonts are unsupported')
    with LottieAnimation.from_data(json.dumps(data), resource_path='') as animation:
        count = animation.lottie_animation_get_totalframe()
        width, height = animation.lottie_animation_get_size()
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS or count > 6000:
            raise ValueError('TGS dimensions/duration exceed limit')
        for index in sample_indices(count):
            frame = animation.render_pillow_frame(frame_num=index, width=512, height=512).convert('RGBA')
            from PIL import Image
            background = Image.new('RGBA', frame.size, 'white')
            background.alpha_composite(frame)
            yield background.convert('RGB')


def _video_frames(path):
    import cv2
    from PIL import Image
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError('unsupported or damaged media')
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not all(math.isfinite(v) and v > 0 for v in (count, fps, width, height)):
            raise ValueError('invalid video metadata')
        if count / fps > MAX_DURATION or width * height > MAX_PIXELS:
            raise ValueError('video dimensions/duration exceed limit')
        for index in sample_indices(count):
            success, frame = False, None
            # Some Telegram MP4/WebM files decode fine but report that random
            # frame seeking is unsupported. Try timestamp seeking before
            # declaring the whole file broken.
            if capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                success, frame = capture.read()
            if not success:
                fallback = cv2.VideoCapture(str(path))
                try:
                    if fallback.isOpened():
                        fallback.set(cv2.CAP_PROP_POS_MSEC, 1000.0 * float(index) / float(fps))
                        success, frame = fallback.read()
                finally:
                    fallback.release()
            if not success:
                raise ValueError(f'video frame decoding failed at frame {int(index)}')
            yield Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        capture.release()


def detection_variants(frame):
    """Three bounded views reduce sensitivity to tint and contrast changes."""
    from PIL import ImageOps
    frame = frame.convert('RGB')
    yield frame
    yield ImageOps.autocontrast(ImageOps.grayscale(frame), cutoff=1).convert('RGB')
    yield ImageOps.autocontrast(frame, cutoff=1)


def scan_frame(detector, frame, explicit_threshold=.80, suggestive_threshold=.85):
    import numpy as np
    best = Scan('checked')
    suspicious = 0.0
    near_explicit = False
    covered = False
    area = max(1, frame.width * frame.height)
    for variant in detection_variants(frame):
        # NudeNet expects OpenCV BGR, Pillow yields RGB. Keep the thresholds:
        # transforming an image does not make the detector infallible.
        detections = detector.detect(np.asarray(variant)[:, :, ::-1].copy())
        result = classify_detections(detections, explicit_threshold, suggestive_threshold)
        if result.category == 'nsfw':
            return result
        if result.category and (not best.category or result.score > best.score):
            best = result
        for item in detections:
            try:
                score = float(item.get('score', 0))
            except (ValueError, TypeError):
                continue
            label = item.get('class')
            if not math.isfinite(score):
                continue
            if label in EXPLICIT and score >= explicit_threshold - .15:
                suspicious, near_explicit = max(suspicious, score), True
            elif label in SUGGESTIVE and score >= suggestive_threshold - .15:
                suspicious = max(suspicious, score)
            elif label == 'FEMALE_BREAST_COVERED' and score >= COVERED_CLOSEUP_SCORE:
                box = item.get('box') or ()
                try:
                    share = float(box[2]) * float(box[3]) / area
                except (IndexError, TypeError, ValueError):
                    share = 0.0
                covered = covered or share >= COVERED_CLOSEUP_SHARE
    if not best.category and suspicious:
        return Scan('unchecked', reason='Пограничная оценка наготы; нужна ручная проверка',
                    score=suspicious, near_explicit=int(near_explicit), borderline=True,
                    covered=int(covered))
    if near_explicit or covered:
        return replace(best, near_explicit=int(near_explicit), covered=int(covered))
    return best


def build_detector():
    """Детектор с предсказуемым аппетитом к адресному пространству.

    nudenet поднимает onnxruntime со своими настройками: арена памяти включена,
    потоков столько, сколько ядер у машины. На хостинге с четырьмя ядрами это
    отнимало больше адресного пространства, чем разрешено процессу, и проверка
    падала — SIGABRT, std::bad_alloc, ONNXRuntimeError: Fail, — хотя реально
    детектору нужно около 110 МБ.

    Замерено: 1.34 ГБ адресного пространства по умолчанию против 1.05 ГБ с
    одним потоком и без арены; резидентная память в обоих случаях ~108 МБ.

    Настройки сессии nudenet передать не даёт — он создаёт её сам, — поэтому
    подменяем конструктор на время вызова и сразу возвращаем на место.
    """
    import onnxruntime
    from nudenet import NudeDetector
    options = onnxruntime.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    original = onnxruntime.InferenceSession
    onnxruntime.InferenceSession = lambda *args, **kwargs: original(
        *args, **{**kwargs, 'sess_options': options})
    try:
        return NudeDetector()
    finally:
        onnxruntime.InferenceSession = original


def scan_file(path, kind, explicit_threshold=.80, suggestive_threshold=.85):
    from PIL import Image, UnidentifiedImageError
    if not 0 < Path(path).stat().st_size <= MAX_BYTES:
        return Scan('unchecked', reason='Размер файла вне допустимого диапазона', bad_input=True)
    if kind == 'tgs':
        frames = _tgs_frames(path)
    else:
        try:
            with Image.open(path):
                pass
            frames = _pillow_frames(path)
        except UnidentifiedImageError:
            # Only open formats with known local file signatures. This avoids
            # playlists referencing URLs or local files through FFmpeg.
            with open(path, 'rb') as stream:
                magic = stream.read(16)
            if not (magic[4:8] == b'ftyp' or magic[:4] == b'\x1aE\xdf\xa3' or
                    (magic[:4] == b'RIFF' and magic[8:12] == b'AVI ')):
                # Не картинка и не видео: Telegram покажет это файлом, а не
                # медиа. Смотреть админу нечего.
                return Scan('unchecked', reason='Неподдерживаемый формат файла',
                            bad_input=True, review=False)
            frames = _video_frames(path)
    detector = build_detector()  # 320n.onnx is included in the pinned wheel.
    best, count = Scan('checked'), 0
    uncertain = None
    suggestive_frames = 0
    near_explicit_frames = 0
    covered_frames = 0
    hashes = []
    rating_pool = []
    frame_iter = iter(frames)
    while True:
        # Ошибка разбора кадра — это битый файл, его может прислать кто угодно;
        # ошибка самого детектора (scan_frame) по-прежнему роняет воркер и
        # считается отказом. Нехватка памяти — тоже отказ, а не вина файла.
        try:
            frame = next(frame_iter)
        except StopIteration:
            break
        except MemoryError:
            raise
        except Exception as exc:
            return Scan('unchecked', reason=f'Файл не декодируется: {type(exc).__name__}',
                        frames=count, bad_input=True, hashes=tuple(hashes))
        fingerprint = frame_hash(frame)
        if fingerprint:
            hashes.append(fingerprint)
        rating_pool.append(frame.convert('RGB').resize((RATING_SIZE, RATING_SIZE)))
        detection = scan_frame(detector, frame, explicit_threshold, suggestive_threshold)
        count += 1
        near_explicit_frames += int(bool(detection.near_explicit))
        covered_frames += int(bool(detection.covered))
        if detection.category == 'nsfw':
            return Scan('checked', detection.category, detection.reason, count, detection.score,
                        hits=1, hashes=tuple(hashes))
        if detection.category == 'spoiler_16':
            suggestive_frames += 1
            if not best.category or detection.score > best.score:
                best = detection
        elif detection.category and (not best.category or detection.score > best.score):
            best = detection
        if detection.status == 'unchecked' and (uncertain is None or detection.score > uncertain.score):
            uncertain = detection
    if not count:
        return Scan('unchecked', reason='Нет декодированных кадров', bad_input=True)
    if best.category == 'spoiler_16' and suggestive_frames < 2 and best.score < min(
            .99, suggestive_threshold + .10):
        # A single frame exactly on the NudeNet threshold caused real false
        # positives (e.g. BUTTOCKS_EXPOSED=0.85). Require either repeated
        # evidence across sampled frames or one clearly stronger detection.
        result = Scan(
            'unchecked',
            reason=(f'Один пограничный кадр 16+ ({best.score:.2f}); '
                    'нужна ручная проверка'),
            frames=count, score=best.score, hits=suggestive_frames,
            near_explicit=near_explicit_frames, borderline=True, hashes=tuple(hashes))
    elif not best.category and uncertain is not None:
        result = Scan('unchecked', reason=uncertain.reason, frames=count, score=uncertain.score,
                      near_explicit=near_explicit_frames, borderline=uncertain.borderline,
                      hashes=tuple(hashes))
    else:
        result = Scan('checked', best.category, best.reason, count, best.score,
                      hits=suggestive_frames if best.category == 'spoiler_16' else 0,
                      near_explicit=near_explicit_frames, hashes=tuple(hashes))
    return second_opinion(result, rating_pool, covered_frames)


def second_opinion(result, frames, covered_frames=0, paths=None):
    """Проверка классификаторами рисунка поверх ответа NudeNet.

    Санкция — только когда оба детектора согласны: находку NudeNet (16+,
    пограничную или «грудь в одежде крупным планом») подтвердили оба
    классификатора. Одни классификаторы — отчёт на ручную оценку.
    """
    result = replace(result, covered=covered_frames)
    paths = rating_model_paths() if paths is None else paths
    if not paths or not frames or result.category == 'nsfw':
        return result
    rating, small, rated, hits = rate_frames(frames, paths)
    result = replace(result, rating=rating, rating_small=small, rated=rated, rating_hits=hits)
    if not rating_flag(rated, hits):
        return result
    if result.status == 'checked' and result.category == 'spoiler_16':
        return replace(result, agreed=True)
    note = f'классификатор рисунка: 16+ ({rating:.2f}/{small:.2f})'
    if result.borderline:
        return replace(result, status='checked', category='spoiler_16', borderline=False,
                       agreed=True,
                       reason=f'Пограничная находка NudeNet ({result.score:.2f}) подтверждена; {note}')
    if covered_frames and (result.frames <= 1 or covered_frames >= 2):
        return replace(result, status='checked', category='spoiler_16', agreed=True,
                       score=max(result.score, COVERED_CLOSEUP_SCORE),
                       reason=f'Грудь в одежде крупным планом; {note}')
    return replace(result, status='unchecked', category='', borderline=True, score=rating,
                   reason=f'Похоже на 16+ ({note}); нужна ручная проверка')


def clamp_worker_memory(memory_mb):
    """Приводит запрошенный лимит к тому, на чём детектор действительно живёт."""
    try:
        value = int(memory_mb)
    except (TypeError, ValueError):
        return WORKER_MEMORY_MB_DEFAULT
    return max(WORKER_MEMORY_MB_MIN, min(8192, value))


def _invoke_worker(path, kind, timeout, explicit_threshold, suggestive_threshold,
                   memory_mb=WORKER_MEMORY_MB_DEFAULT, rating_paths=()):
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               MKL_NUM_THREADS='1', OPENCV_FFMPEG_CAPTURE_OPTIONS='protocol_whitelist;file',
               # glibc заводит под каждый поток свою арену — до 8 на ядро, по 64 МБ
               # адресного пространства каждая. На четырёх ядрах это 2 ГБ, которые
               # никто не использует, но лимит адресного пространства съедают
               # целиком. Реальной памяти арены не занимают: она выделяется по
               # мере записи. Двух хватает.
               MALLOC_ARENA_MAX='2',
               # OpenCV поднимает пул потоков по числу ядер — и каждый поток
               # тянет за собой свою арену.
               OPENCV_NUM_THREADS='1')
    env[WORKER_MEMORY_ENV] = str(clamp_worker_memory(memory_mb))
    env.pop(RATING_ENV, None)
    if rating_paths:
        env[RATING_ENV] = os.pathsep.join(str(item) for item in rating_paths)
    # Classifier never needs bot tokens, provider keys or cloud credentials.
    for name in list(env):
        if any(part in name.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD', 'KEY', 'CREDENTIAL')):
            env.pop(name, None)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(path), kind,
         str(explicit_threshold), str(suggestive_threshold)],
        capture_output=True, text=True, encoding='utf-8', timeout=timeout,
        env=env, creationflags=flags, check=False)


def worker_error_tail(stderr, limit=160):
    """Последняя содержательная строка вывода воркера."""
    lines = [line.strip() for line in str(stderr or '').splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ''


def worker_failure_reason(returncode, stderr):
    """Почему воркер не отработал — словами, по которым можно чинить.

    Раньше на любой сбой отчёт говорил «недоступен или завершился с ошибкой»,
    а stderr выбрасывался. По такому тексту не отличить не установленную
    библиотеку от нехватки памяти, и владелец чинит наугад то, что не сломано.
    """
    tail = worker_error_tail(stderr)
    if returncode < 0:
        # Убит сигналом. На маленьком хостинге это почти всегда OOM-killer:
        # детектор поднимает onnxruntime во втором процессе.
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f'сигнал {-returncode}'
        # SIGKILL есть не на всякой платформе, а падать диагностика не вправе.
        hint = (' — вероятно, не хватило памяти'
                if -returncode == getattr(signal, 'SIGKILL', None) else '')
        return f'Детектор убит ({name}){hint}'
    reason = f'Детектор завершился с кодом {returncode}'
    return f'{reason}: {tail}' if tail else reason


def run_worker(path, kind, timeout, explicit_threshold, suggestive_threshold,
               memory_mb=WORKER_MEMORY_MB_DEFAULT, rating_paths=()):
    try:
        result = _invoke_worker(path, kind, timeout, explicit_threshold,
                                suggestive_threshold, memory_mb, rating_paths)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker before returning.
        return Scan('unchecked', reason='Превышено время локальной проверки')
    except (OSError, ValueError, TypeError) as exc:
        return Scan('unchecked', reason=f'Не удалось запустить детектор: {type(exc).__name__}')
    if result.returncode:
        return Scan('unchecked', reason=worker_failure_reason(result.returncode, result.stderr))
    try:
        data = json.loads(result.stdout)
        raw_hashes = data.get('hashes') or ()
        data['hashes'] = tuple(str(value) for value in (
            raw_hashes if isinstance(raw_hashes, (list, tuple)) else ())[:64])
        return Scan(**data)
    except (ValueError, TypeError, AttributeError):
        tail = worker_error_tail(result.stderr) or worker_error_tail(result.stdout)
        return Scan('unchecked', reason='Детектор ответил неразборчиво'
                                        + (f': {tail}' if tail else ''))


def _measure_address_space(directory, timeout, rating_paths=()):
    """Сколько адресного пространства детектор берёт без тесного потолка.

    Число, а не догадка: подбирать лимит перезапусками — это часы на то, что
    машина может сказать сама.
    """
    path = Path(directory) / 'probe.png'
    if not path.exists():
        return 0
    try:
        result = _invoke_worker(path, 'image', timeout, .80, .85, 8192, rating_paths)
        if result.returncode:
            return 0
        return int(Scan(**json.loads(result.stdout)).address_space_mb or 0)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return 0


def probe(timeout=60, memory_mb=WORKER_MEMORY_MB_DEFAULT, rating_paths=()):
    """Самопроверка детектора на сгенерированной картинке.

    Отчёт из чата говорит только, что медиа не проверено. Чинить хостинг по
    такому сообщению нельзя: непонятно, чего не хватает. Здесь проверка
    запускается по требованию и отдаёт вывод воркера целиком.

    Если не хватило памяти, проба повторяется с поднятым потолком — иначе
    владелец подбирает лимит наугад после каждого отказа. Аппетит к адресному
    пространству у разных машин разный (число ядер, версия onnxruntime, аллокатор),
    и единственный способ узнать нужное число — измерить его на этой машине.
    """
    started = time.monotonic()
    try:
        from PIL import Image
    except ImportError as exc:
        return Scan('unchecked', reason=f'Нет Pillow: {exc}'), '', 0.0
    with tempfile.TemporaryDirectory(prefix='media-probe-') as directory:
        path = Path(directory) / 'probe.png'
        try:
            Image.new('RGB', (64, 64), 'slategray').save(path)
            result = _invoke_worker(path, 'image', timeout, .80, .85, memory_mb, rating_paths)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return (Scan('unchecked', reason=f'{type(exc).__name__}: {exc}'), '',
                    time.monotonic() - started)
        spent = time.monotonic() - started
        details = (result.stderr or '').strip()[-600:]
        if result.returncode:
            return Scan('unchecked',
                        reason=worker_failure_reason(result.returncode, result.stderr)), details, spent
        try:
            scan = Scan(**json.loads(result.stdout))
        except (ValueError, TypeError):
            return Scan('unchecked', reason='Детектор ответил неразборчиво'), details or str(result.stdout)[:600], spent
        # Мерить есть смысл ровно в одном случае — когда отказ именно по
        # памяти. Успешная проверка говорит о памяти только цену секунд, а
        # сломанной установке число не поможет и собьёт с толку.
        if 'памяти' not in scan.reason:
            return scan, details, spent
        needed = _measure_address_space(directory, timeout, rating_paths)
        if needed:
            scan = Scan(scan.status,
                        reason=f'{scan.reason}. На этой машине детектору нужно '
                               f'{needed} МБ: поставьте MODERATION_MEDIA_MEMORY_MB='
                               f'{needed + 256}',
                        address_space_mb=needed)
        return scan, details, spent


class MediaScanner:
    def __init__(self, timeout=25, explicit_threshold=.80, suggestive_threshold=.85,
                 max_waiting=4, memory_mb=WORKER_MEMORY_MB_DEFAULT):
        self.timeout = max(5, min(60, timeout))
        self.explicit_threshold = max(.65, min(.99, explicit_threshold))
        self.suggestive_threshold = max(.70, min(.99, suggestive_threshold))
        # Детектор в чате один, и это правильно: второй такой процесс хостинг
        # не потянет. Но «занято» раньше означало «не проверяем вовсе», и в
        # оживлённом чате всё, что прилетало во время чужой проверки, уходило
        # непроверенным — то есть 18+ проходил ровно тогда, когда медиа много.
        self.max_waiting = max(0, min(32, max_waiting))
        self.memory_mb = clamp_worker_memory(memory_mb)
        # Сломанный детектор ломается одинаково для всех: очередь при этом
        # копилась и переполнялась, а отчёт говорил про очередь, а не про
        # поломку. После нескольких отказов подряд берём паузу и говорим прямо.
        self.failure_limit = 3
        self.pause_sec = 600
        self._failures = 0
        self._paused_until = 0.0
        self._gate = None
        self._gate_loop = None
        self._in_flight = 0
        self._cache = OrderedDict()
        # Классификаторы рисунка: пути появляются, когда модели скачаны.
        self.rating_paths = ()
        self.rating_status = 'не загружены'

    def _slot(self):
        """Замок детектора, привязанный к текущему циклу событий.

        Сканер создаётся при импорте, когда цикла ещё нет, а примитив asyncio
        привязывается к первому же циклу, который его тронул. В прогоне тестов
        цикл у каждого теста свой: один замок на всех давал бы падения
        «bound to a different event loop», зависящие от порядка файлов.
        """
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Semaphore(1)
            self._gate_loop = loop
            self._in_flight = 0
        return self._gate

    def queue_depth(self):
        """Сколько проверок сейчас ждут очереди — для диагностики."""
        return max(0, self._in_flight - 1)

    def paused_for(self):
        """Сколько секунд детектор ещё на паузе после серии отказов."""
        return max(0.0, self._paused_until - time.monotonic())

    def _note_worker_result(self, result):
        """Count only technical detector failures, never moderation uncertainty.

        A borderline frame is a valid detector answer that requires a human.
        Treating three such answers as crashes paused NudeNet for ten minutes
        even though the worker was healthy.
        """
        if result.status == 'checked':
            self._failures = 0
            self._paused_until = 0.0
            return
        reason = str(result.reason or '').casefold()
        if ('ручн' in reason or 'пограничн' in reason or getattr(result, 'bad_input', False)
                or getattr(result, 'borderline', False)):
            return
        self._failures += 1
        if self._failures >= self.failure_limit:
            self._paused_until = time.monotonic() + self.pause_sec

    async def _check_downloadable(self, bot, item, kind):
        """Download one bounded attachment and run the disposable detector."""
        key = str(getattr(item, 'file_unique_id', '') or '')
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        paused = self.paused_for()
        if paused > 0:
            return Scan('unchecked', reason=f'Детектор не отвечает {self._failures} раз подряд; '
                                            f'пауза ещё {paused / 60:.0f} мин (/mediaping)')
        gate = self._slot()
        if self._in_flight > self.max_waiting:
            return Scan('unchecked', reason='Очередь локальной проверки переполнена')
        self._in_flight += 1
        try:
            try:
                await asyncio.wait_for(gate.acquire(), timeout=self.timeout * 2 + 20)
            except asyncio.TimeoutError:
                return Scan('unchecked', reason='Локальная проверка не дождалась очереди')
            try:
                try:
                    file = await asyncio.wait_for(bot.get_file(item.file_id), timeout=15)
                    remote_size = getattr(file, 'file_size', None)
                    if remote_size is None or not 0 < remote_size <= MAX_BYTES:
                        return Scan('unchecked', reason='Неизвестный или недопустимый размер файла')
                    with tempfile.TemporaryDirectory(prefix='chat-moderation-') as directory:
                        path = Path(directory) / 'content'
                        await asyncio.wait_for(
                            file.download_to_drive(custom_path=path, read_timeout=15, connect_timeout=5),
                            timeout=20)
                        task = asyncio.create_task(asyncio.to_thread(
                            run_worker, path, kind, self.timeout,
                            self.explicit_threshold, self.suggestive_threshold,
                            self.memory_mb, self.rating_paths))
                        try:
                            result = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await task
                            raise
                except Exception:
                    return Scan('unchecked', reason='Не удалось скачать или проверить медиа')
                self._note_worker_result(result)
                if key and result.status == 'checked':
                    self._cache[key] = (time.monotonic() + 86400, result)
                    while len(self._cache) > 512:
                        self._cache.popitem(last=False)
                return result
            finally:
                gate.release()
        finally:
            self._in_flight -= 1

    async def _check_preview_only(self, bot, message, limit: str, found_prefix: str):
        """Проверка по превью для файла, который целиком не проверить.

        Чистое превью не доказывает, что чист весь ролик, — но и открывать
        каждый такой ролик руками админ не станет: письмо приходило на любое
        видео больше 20 МБ, а это половина роликов в живом чате. Поэтому
        чистое превью — запись в журнал без письма; находка на превью или
        превью, которое не удалось проверить, — письмо, как и раньше.
        """
        preview = media_preview_attachment(message)
        if preview is None:
            return Scan('unchecked', reason=f'{limit}; превью нет')
        preview_item, preview_kind = preview
        preview_scan = await self._check_downloadable(bot, preview_item, preview_kind)
        if preview_scan.status != 'checked':
            return Scan('unchecked', reason=f'{limit}; превью тоже не проверено: {preview_scan.reason}',
                        frames=preview_scan.frames, score=preview_scan.score)
        if preview_scan.category:
            return Scan('unchecked', category=preview_scan.category,
                        reason=f'{found_prefix}; на превью: {preview_scan.reason}',
                        frames=preview_scan.frames, score=preview_scan.score,
                        hashes=tuple(preview_scan.hashes or ()))
        return Scan('unchecked', reason=f'{limit}; превью чистое, весь файл не проверен',
                    frames=preview_scan.frames, review=False,
                    hashes=tuple(preview_scan.hashes or ()))

    async def check(self, bot, message):
        attachment = media_attachment(message)
        if attachment is None:
            return None
        item, kind = attachment
        size = getattr(item, 'file_size', None)
        if size is not None and size <= 0:
            return Scan('unchecked', reason='Некорректный размер файла')
        # Файл, который целиком не проверить: Bot API не отдаёт больше 20 МБ,
        # а длинное или огромное видео детектор не переварит. Смотрим превью —
        # маленькую картинку, которую Telegram почти всегда прикладывает.
        if size is not None and size > MAX_BYTES:
            return await self._check_preview_only(bot, message, 'Файл превышает лимит загрузки Bot API 20 МБ',
                                                  'Оригинал больше 20 МБ и недоступен Bot API')
        duration = getattr(item, 'duration', 0) or 0
        if isinstance(duration, timedelta):
            duration = duration.total_seconds()
        if duration > MAX_DURATION:
            return await self._check_preview_only(bot, message, 'Видео длиннее 3 минут',
                                                  'Видео длиннее 3 минут')
        if (getattr(item, 'width', 0) or 0) * (getattr(item, 'height', 0) or 0) > MAX_PIXELS:
            return await self._check_preview_only(bot, message, 'Слишком большое разрешение',
                                                  'Слишком большое разрешение')
        return await self._check_downloadable(bot, item, kind)

if __name__ == '__main__':
    # Keep decoder memory bounded on production Linux; Windows also has input
    # limits and the supervising timeout, but no resource module.
    try:
        import resource
    except ImportError:
        resource = None
    memory_mb = clamp_worker_memory(os.environ.get(WORKER_MEMORY_ENV, WORKER_MEMORY_MB_DEFAULT))
    if resource is not None:
        for limit, wanted in ((resource.RLIMIT_AS, memory_mb * 1024**2), (resource.RLIMIT_CPU, 50)):
            try:
                soft, hard = resource.getrlimit(limit)
                # Жёсткий лимит хоста можно понизить, но не поднять: попытка
                # выставить 3 ГБ там, где потолок ниже, роняет воркер прямо на
                # старте, и каждая проверка превращается в «детектор упал».
                # Хвост-хард оставляем как есть — сужаем только мягкий.
                if hard != resource.RLIM_INFINITY:
                    wanted = min(wanted, hard)
                if soft == resource.RLIM_INFINITY or wanted < soft:
                    resource.setrlimit(limit, (wanted, hard))
            except (ValueError, OSError):
                # Лимит — страховка, а не условие работы: без неё проверка
                # всё равно ограничена таймаутом надзирающего процесса.
                pass
    try:
        scan = scan_file(sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
        scan = Scan(**{**asdict(scan), 'address_space_mb': _address_space_peak_mb()})
    except Exception as exc:
        # Трассировка уходит в stderr: родитель её читает, и «медиа не проверено»
        # перестаёт быть тупиком — /mediaping показывает, чего не хватило.
        traceback.print_exc(file=sys.stderr)
        text = f'{type(exc).__name__}: {exc}'.lower()
        if isinstance(exc, MemoryError) or any(
                mark in text for mark in ('bad_alloc', 'out of memory', 'cannot allocate')):
            # Нехватку памяти и сломанную установку чинят по-разному, а раньше
            # обе выглядели одинаково. onnxruntime говорит про память
            # std::bad_alloc, а не MemoryError, поэтому смотрим и на текст.
            scan = Scan('unchecked',
                        reason=f'Детектору не хватило памяти (лимит {memory_mb} МБ)',
                        address_space_mb=_address_space_peak_mb())
        else:
            detail = ' '.join(str(exc).split())[:120]
            suffix = f': {detail}' if detail else ''
            scan = Scan('unchecked',
                        reason=(f'Ошибка декодирования или детектора: '
                                f'{type(exc).__name__}{suffix}'),
                        address_space_mb=_address_space_peak_mb())
    print(json.dumps(asdict(scan), ensure_ascii=True))
