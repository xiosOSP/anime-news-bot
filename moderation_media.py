"""Offline nudity checks in a disposable, time-limited decoder process.

No media is sent to an external classifier. A negative sampled result is not
proof that every video frame is safe. Errors are explicitly 'unchecked'.
"""
import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass
import gzip
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import timedelta

MAX_BYTES = 20 * 1024 * 1024
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
        # Documents can hide media under a false name/MIME; sniff in worker.
        return item, 'unknown'
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
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                raise ValueError('video seek failed')
            success, frame = capture.read()
            if not success:
                raise ValueError('video frame decoding failed')
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
    suspicious = False
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
            if math.isfinite(score) and ((label in EXPLICIT and score >= explicit_threshold - .15)
                                         or (label in SUGGESTIVE and score >= suggestive_threshold - .15)):
                suspicious = True
    if not best.category and suspicious:
        return Scan('unchecked', reason='Пограничная оценка наготы; нужна ручная проверка')
    return best


def scan_file(path, kind, explicit_threshold=.80, suggestive_threshold=.85):
    from nudenet import NudeDetector
    from PIL import Image, UnidentifiedImageError
    if not 0 < Path(path).stat().st_size <= MAX_BYTES:
        return Scan('unchecked', reason='Размер файла вне допустимого диапазона')
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
                return Scan('unchecked', reason='Неподдерживаемый формат файла')
            frames = _video_frames(path)
    detector = NudeDetector()  # 320n.onnx is included in the pinned wheel.
    best, count = Scan('checked'), 0
    uncertain = None
    for frame in frames:
        detection = scan_frame(detector, frame, explicit_threshold, suggestive_threshold)
        count += 1
        if detection.category == 'nsfw':
            return Scan('checked', detection.category, detection.reason, count, detection.score)
        if detection.category:
            best = detection
        if detection.status == 'unchecked':
            uncertain = detection
    if not count:
        return Scan('unchecked', reason='Нет декодированных кадров')
    if not best.category and uncertain is not None:
        return Scan('unchecked', reason=uncertain.reason, frames=count)
    return Scan('checked', best.category, best.reason, count, best.score)


def run_worker(path, kind, timeout, explicit_threshold, suggestive_threshold):
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               MKL_NUM_THREADS='1', OPENCV_FFMPEG_CAPTURE_OPTIONS='protocol_whitelist;file')
    # Classifier never needs bot tokens, provider keys or cloud credentials.
    for name in list(env):
        if any(part in name.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD', 'KEY', 'CREDENTIAL')):
            env.pop(name, None)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(path), kind,
             str(explicit_threshold), str(suggestive_threshold)],
            capture_output=True, text=True, encoding='utf-8', timeout=timeout,
            env=env, creationflags=flags, check=False)
        if result.returncode:
            return Scan('unchecked', reason='Локальный детектор недоступен или завершился с ошибкой')
        return Scan(**json.loads(result.stdout))
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker before returning.
        return Scan('unchecked', reason='Превышено время локальной проверки')
    except (OSError, ValueError, TypeError):
        return Scan('unchecked', reason='Ошибка запуска локального детектора')


class MediaScanner:
    def __init__(self, timeout=25, explicit_threshold=.80, suggestive_threshold=.85):
        self.timeout = max(5, min(60, timeout))
        self.explicit_threshold = max(.65, min(.99, explicit_threshold))
        self.suggestive_threshold = max(.70, min(.99, suggestive_threshold))
        self._busy = False
        self._cache = OrderedDict()

    async def check(self, bot, message):
        attachment = media_attachment(message)
        if attachment is None:
            return None
        item, kind = attachment
        size = getattr(item, 'file_size', None)
        if size is not None and (size <= 0 or size > MAX_BYTES):
            return Scan('unchecked', reason='Файл превышает лимит проверки 20 МБ')
        duration = getattr(item, 'duration', 0) or 0
        if isinstance(duration, timedelta):
            duration = duration.total_seconds()
        if duration > MAX_DURATION:
            return Scan('unchecked', reason='Видео длиннее 3 минут')
        if (getattr(item, 'width', 0) or 0) * (getattr(item, 'height', 0) or 0) > MAX_PIXELS:
            return Scan('unchecked', reason='Слишком большое разрешение')
        key = str(getattr(item, 'file_unique_id', '') or '')
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        if self._busy:
            return Scan('unchecked', reason='Локальная проверка занята')
        self._busy = True
        try:
            try:
                file = await asyncio.wait_for(bot.get_file(item.file_id), timeout=15)
                remote_size = getattr(file, 'file_size', None)
                if remote_size is None or not 0 < remote_size <= MAX_BYTES:
                    return Scan('unchecked', reason='Неизвестный или недопустимый размер файла')
                with tempfile.TemporaryDirectory(prefix='chat-moderation-') as directory:
                    path = Path(directory) / 'content'
                    await asyncio.wait_for(file.download_to_drive(custom_path=path,
                                                                 read_timeout=15, connect_timeout=5), timeout=20)
                    task = asyncio.create_task(asyncio.to_thread(
                        run_worker, path, kind, self.timeout,
                        self.explicit_threshold, self.suggestive_threshold))
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Keep ownership of the file and slot until worker exits.
                        await task
                        raise
            except Exception:
                return Scan('unchecked', reason='Не удалось скачать или проверить медиа')
            if key and result.status == 'checked':
                self._cache[key] = (time.monotonic() + 86400, result)
                while len(self._cache) > 512:
                    self._cache.popitem(last=False)
            return result
        finally:
            self._busy = False


if __name__ == '__main__':
    # Keep decoder memory bounded on production Linux; Windows also has input
    # limits and the supervising timeout, but no resource module.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    except ImportError:
        pass
    try:
        scan = scan_file(sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    except Exception:
        scan = Scan('unchecked', reason='Ошибка декодирования или локального детектора')
    print(json.dumps(asdict(scan), ensure_ascii=True))
