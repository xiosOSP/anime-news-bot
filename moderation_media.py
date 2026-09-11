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
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import timedelta

MAX_BYTES = 20 * 1024 * 1024
# Потолок адресного пространства воркера. Просить больше, чем есть у хостинга,
# бессмысленно: лимит не сработает никогда, и вместо аккуратного отказа одного
# процесса ядро выберет жертву само — может выбрать и самого бота.
# Замерено на настроенной сессии (один поток, без арены): пик адресного
# пространства 1.05 ГБ, резидентная память 108 МБ. Отсюда пол 1024 МБ —
# значение ниже молча превращало бы каждую проверку в отказ — и запас до 1536,
# чтобы на машине с другим числом ядер не упереться в потолок.
WORKER_MEMORY_MB_MIN = 1024
WORKER_MEMORY_MB_DEFAULT = 1536
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
    detector = build_detector()  # 320n.onnx is included in the pinned wheel.
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


def clamp_worker_memory(memory_mb):
    """Приводит запрошенный лимит к тому, на чём детектор действительно живёт."""
    try:
        value = int(memory_mb)
    except (TypeError, ValueError):
        return WORKER_MEMORY_MB_DEFAULT
    return max(WORKER_MEMORY_MB_MIN, min(8192, value))


def _invoke_worker(path, kind, timeout, explicit_threshold, suggestive_threshold,
                   memory_mb=WORKER_MEMORY_MB_DEFAULT):
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               MKL_NUM_THREADS='1', OPENCV_FFMPEG_CAPTURE_OPTIONS='protocol_whitelist;file')
    env[WORKER_MEMORY_ENV] = str(clamp_worker_memory(memory_mb))
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
               memory_mb=WORKER_MEMORY_MB_DEFAULT):
    try:
        result = _invoke_worker(path, kind, timeout, explicit_threshold,
                                suggestive_threshold, memory_mb)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker before returning.
        return Scan('unchecked', reason='Превышено время локальной проверки')
    except (OSError, ValueError, TypeError) as exc:
        return Scan('unchecked', reason=f'Не удалось запустить детектор: {type(exc).__name__}')
    if result.returncode:
        return Scan('unchecked', reason=worker_failure_reason(result.returncode, result.stderr))
    try:
        return Scan(**json.loads(result.stdout))
    except (ValueError, TypeError):
        tail = worker_error_tail(result.stderr) or worker_error_tail(result.stdout)
        return Scan('unchecked', reason='Детектор ответил неразборчиво'
                                        + (f': {tail}' if tail else ''))


def probe(timeout=60, memory_mb=WORKER_MEMORY_MB_DEFAULT):
    """Самопроверка детектора на сгенерированной картинке.

    Отчёт из чата говорит только, что медиа не проверено. Чинить хостинг по
    такому сообщению нельзя: непонятно, чего не хватает. Здесь проверка
    запускается по требованию и отдаёт вывод воркера целиком.
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
            result = _invoke_worker(path, 'image', timeout, .80, .85, memory_mb)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return (Scan('unchecked', reason=f'{type(exc).__name__}: {exc}'), '',
                    time.monotonic() - started)
        spent = time.monotonic() - started
        details = (result.stderr or '').strip()[-600:]
        if result.returncode:
            return Scan('unchecked',
                        reason=worker_failure_reason(result.returncode, result.stderr)), details, spent
        try:
            return Scan(**json.loads(result.stdout)), details, spent
        except (ValueError, TypeError):
            return Scan('unchecked', reason='Детектор ответил неразборчиво'), details or str(result.stdout)[:600], spent


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
        """Считает отказы подряд: только ответы самого детектора.

        Отказы по размеру файла и переполнению очереди сюда не попадают —
        это не поломка детектора, а штатный отказ до его запуска.
        """
        if result.status == 'checked':
            self._failures = 0
            self._paused_until = 0.0
            return
        self._failures += 1
        if self._failures >= self.failure_limit:
            self._paused_until = time.monotonic() + self.pause_sec

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
        paused = self.paused_for()
        if paused > 0:
            # Честный текст вместо «очередь переполнена»: пока детектор не
            # работает, очередь — следствие, а не причина.
            return Scan('unchecked', reason=f'Детектор не отвечает {self._failures} раз подряд; '
                                            f'пауза ещё {paused / 60:.0f} мин (/mediaping)')
        gate = self._slot()
        # Считаем всех, кто уже проверяется или ждёт очереди. По gate.locked()
        # переполнение не увидеть: захват уходит в отдельную задачу и к этому
        # моменту ещё не случился, так что второй проверяющий видел бы
        # свободный замок и потолок не работал бы вовсе.
        if self._in_flight > self.max_waiting:
            # Потолок нарочный: ждущая проверка держит обработчик апдейта, и
            # без него всплеск сообщений утащил бы бота целиком.
            return Scan('unchecked', reason='Очередь локальной проверки переполнена')
        self._in_flight += 1
        try:
            try:
                # Ожидание ограничено сверху: за это время стоящие впереди
                # успевают отработать (скачивание плюс детектор), а если не
                # успевают — честнее сказать человеку, чем ждать бесконечно.
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
                        await asyncio.wait_for(file.download_to_drive(custom_path=path,
                                                                     read_timeout=15, connect_timeout=5), timeout=20)
                        task = asyncio.create_task(asyncio.to_thread(
                            run_worker, path, kind, self.timeout,
                            self.explicit_threshold, self.suggestive_threshold,
                            self.memory_mb))
                        try:
                            result = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            # Keep ownership of the file and slot until worker exits.
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
            scan = Scan('unchecked', reason=f'Детектору не хватило памяти (лимит {memory_mb} МБ)')
        else:
            scan = Scan('unchecked',
                        reason=f'Ошибка декодирования или детектора: {type(exc).__name__}')
    print(json.dumps(asdict(scan), ensure_ascii=True))
