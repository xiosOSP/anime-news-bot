"""
Аниме-новостной Telegram-бот.
Стиль постов — близкий к каналу Fubuki61: без жирных заголовков,
без ссылок на источник, без эмодзи и хэштегов.
"""

import asyncio
import hashlib
import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import tempfile
import zipfile
import copy
import time
import difflib
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

# Опциональная зависимость: без Pillow дедуп картинок работает по точному
# совпадению файла (md5), с Pillow — по перцептивному хешу.
try:
    from PIL import Image
except ImportError:
    Image = None

# Опциональная зависимость — если yt-dlp нет, скачивание видео отключится
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

# ============== НАСТРОЙКИ ==============
# Чувствительные значения читаются из переменных окружения (env).
# Токен ОБЯЗАТЕЛЬНО задаётся через env (BOT_TOKEN) — в коде его нет (репозиторий публичный).
# Для локального запуска на ПК создайте файл .env рядом с этим скриптом (см. .env.example).
# Файл .env в репозиторий не попадает (он в .gitignore).

def _load_dotenv(path: str = '.env') -> None:
    """Простой загрузчик .env без внешних зависимостей.
    Читает строки вида KEY=VALUE и кладёт в окружение (не перезаписывая уже заданные).
    Если файла нет — молча пропускает (на хостинге переменные задаются в панели)."""
    p = Path(path)
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


# Загружаем .env (для локального запуска). На хостинге файла нет — переменные из панели.
_load_dotenv()


def _env(key: str, default: str) -> str:
    """Читает строковую переменную окружения с fallback на дефолт."""
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    """Читает числовую переменную окружения с fallback на дефолт."""
    val = os.getenv(key)
    if val is None or val.strip() == '':
        return default
    try:
        return int(val)
    except ValueError:
        return default


# Токен бота — ТОЛЬКО из переменной окружения (в коде не хранится).
# Локально: задайте в .env. На хостинге: в панели переменных окружения.
TOKEN = _env('BOT_TOKEN', '') or _env('TELEGRAM_BOT_TOKEN', '')

# Эти значения не секретны (ID публичного канала и т.п.), поэтому fallback допустим.
# При желании их тоже можно переопределить через env.
MAIN_CHANNEL_ID = -1003040322753        # основной канал проекта
_channel_env = (os.getenv('CHANNEL_ID') or '').strip()
CHANNEL_FROM_ENV = bool(_channel_env)   # видно в /health и стартовом отчёте
_channel_raw = _channel_env or str(MAIN_CHANNEL_ID)
# Числовой ID приводим к int: Telegram принимает оба вида, но с числом
# меньше шансов на опечатку вроде лишнего пробела в переменной окружения
CHANNEL_ID = int(_channel_raw) if re.fullmatch(r'-?\d+', _channel_raw) \
    else _channel_raw
ADMIN_ID = _env_int('ADMIN_ID', 5056873937)

# Группа обсуждения и ветка (тема форума) для режима "слать всё в ветку".
# Узнать ID можно командой /chatinfo внутри нужной ветки.
DISCUSSION_CHAT_ID = _env_int('DISCUSSION_CHAT_ID', -1003178917488)   # ID супергруппы обсуждения
DISCUSSION_THREAD_ID = _env_int('DISCUSSION_THREAD_ID', 10138)        # ID темы "бот-новостник"

# DeepL API-ключ (опционально). Если задан — перевод идёт через DeepL (качество выше),
# иначе через Google Translate. Ключ бесплатного тира заканчивается на ':fx'.
# Получить: https://www.deepl.com/pro-api  →  переменная окружения DEEPL_API_KEY.
DEEPL_API_KEY = _env('DEEPL_API_KEY', '')

# --- Фильтрация постов ---
# Whitelist: если задан, пост обязан содержать хотя бы одно из этих слов
KEYWORDS: list[str] = []
# Blacklist: пост скипается если содержит ЛЮБОЕ из этих слов в заголовке или начале summary.
# Это товарка/реклама/розыгрыши — не новости.
BLACKLIST: list[str] = [
    'figure release', 'figurine release', 'pre-order', 'preorder',
    'merchandise', 'merch drop', 'merch line',
    'plushie', 'plush release',
    'keychain', 'acrylic stand', 'badge set',
    'raffle', 'giveaway', 'sweepstakes',
    'scratch lottery', 'ichiban kuji',
    'pop-up shop', 'collab cafe',
    'casino', 'crypto', 'nft',
]

# ============== КОНСТАНТЫ ==============
# Базовая папка для данных бота (JSON-файлы, логи).
# На хостинге с постоянным хранилищем (Bothost Volume и др.) задаётся через env DATA_DIR,
# например '/data' или '/storage'. Локально (без env) — текущая папка.
DATA_DIR = Path(os.getenv('DATA_DIR', '.'))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path('.')

CHECK_INTERVAL_SEC = 1800
SENT_LINKS_FILE = DATA_DIR / 'sent_links.json'
SENT_LINKS_MAX = 5000
SENT_LINKS_TRIM_TO = 3000
HTTP_TIMEOUT = 15
TG_CAPTION_LIMIT = 1024              # жёсткое ограничение Telegram для подписи под фото
TG_TEXT_LIMIT = 4096                 # лимит обычного текстового сообщения
# Внутренний лимит summary для режима КАНАЛА (одно сообщение фото+подпись).
SUMMARY_MAX_CHARS = 950
# Внутренний лимит summary для режима ВЕТКИ (текст отдельным сообщением до 4096).
# Оставляем запас под заголовок и html.escape.
SUMMARY_MAX_CHARS_THREAD = 3500
# Сколько отдаём в Google Translate за раз.
# Для канала хватает 1500, но для ветки нужен длинный текст — берём максимум.
TRANSLATION_INPUT_LIMIT = 1500
TRANSLATION_INPUT_LIMIT_THREAD = 4000
NEWS_PER_SOURCE = 5
PAUSE_BETWEEN_SENDS = 2.0
SOURCE_FETCH_CONCURRENCY = 5          # столько источников качаем одновременно
# APScheduler по умолчанию ставит misfire_grace_time=1с: если тик джоба опоздал
# больше чем на секунду (цикл занят сбором новостей/отправкой), запуск МОЛЧА
# выбрасывается. Для нас это означало «отложка не публикуется». Даём час запаса.
JOB_KWARGS = {'misfire_grace_time': 3600}

# --- AniList API ---
ANILIST_CACHE_FILE = DATA_DIR / 'anilist_cache.json'
ANILIST_API_URL = 'https://graphql.anilist.co'
ANILIST_TIMEOUT = 5                  # секунд на запрос (короткий, чтобы не тормозить пост)
ANILIST_CACHE_TTL_DAYS = 30          # положительные результаты помним месяц
ANILIST_NEGATIVE_TTL_DAYS = 7        # отрицательные («не найдено») — неделю

# --- Логирование ---
LOG_FILE = DATA_DIR / 'bot.log'
LOG_MAX_BYTES = 5 * 1024 * 1024      # 5 МБ на файл
LOG_BACKUP_COUNT = 3                 # храним 3 ротированных файла (~20 МБ всего)
LOG_TAIL_LINES = 50                  # сколько последних строк показывает /logs

# --- HTTP retry ---
HTTP_RETRY_ATTEMPTS = 3              # всего попыток (включая первую)
HTTP_RETRY_BACKOFFS = (1.0, 2.0, 4.0)  # пауза перед попытками 2, 3, 4
HTTP_RETRY_STATUSES = (500, 502, 503, 504, 408, 429)  # коды на которых ретраим

# --- Прокси (опционально). Используется для Reddit, который банит VPS-IP.
# Формат: 'http://user:pass@host:port' или None.
# Заполнить если с сервера Reddit стал отвечать 403.
REDDIT_PROXY: Optional[str] = None

# --- Видео ---
VIDEO_MAX_DURATION_SEC = 0            # 0 = без ограничения по длине, ограничение только по размеру файла
VIDEO_MAX_FILE_SIZE_MB = 48           # запас от лимита Telegram (50 МБ)
def _video_format() -> str:
    """Строка формата для yt-dlp.

    Без ffmpeg склеить отдельные дорожки видео и звука невозможно, поэтому
    берём только «прогрессивные» форматы — где картинка и звук уже в одном
    файле. Раньше формат этого не учитывал, и на хостинге без ffmpeg
    скачивание молча срывалось."""
    if shutil.which('ffmpeg'):
        return ('bv*[height<=720][filesize<45M]+ba/'
                'b[height<=720][filesize<45M]/'
                'b[height<=720]/b')
    progressive = '[vcodec!=none][acodec!=none]'
    return (f'b[ext=mp4]{progressive}[filesize<45M]/'
            f'b{progressive}[filesize<45M]/'
            f'b[ext=mp4]{progressive}/'
            f'b{progressive}')


VIDEO_FORMAT = _video_format()
VIDEO_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / 'anime_news_bot_videos'
VIDEO_DOWNLOAD_DIR.mkdir(exist_ok=True)

# --- Медиа ---
MAX_PHOTOS_PER_POST = 6               # сколько фото максимум собирать в media group
TG_VIDEO_MAX_SECONDS = 300            # видео из TG-каналов: до 5 минут
TG_VIDEO_MAX_MB = 48                  # Bot API не принимает файлы больше ~50 МБ
# Хосты, для которых пробуем yt-dlp
VIDEO_HOSTS = (
    'youtube.com', 'youtu.be', 'm.youtube.com',
    'twitter.com', 'x.com', 'mobile.twitter.com',
    'vimeo.com', 'player.vimeo.com',
    'nicovideo.jp', 'nico.ms',
    'bilibili.com',
    'dailymotion.com',
    'twitch.tv', 'clips.twitch.tv',
)
# Прямые видео-расширения, которые шлём напрямую через sendVideo
DIRECT_VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov', '.m4v')

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
REDDIT_USER_AGENT = 'windows:anime-news-bot:v1.0 (personal use)'

def _setup_logging() -> logging.Logger:
    """Настройка логирования в консоль. Файловый handler добавляется отдельно
    через _setup_file_logging() в main() — чтобы тесты не создавали bot.log."""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S')

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)

    # Заглушаем шумные библиотеки
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    return logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """Добавляет файловый handler с ротацией. Вызывается из main()."""
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    root = logging.getLogger()
    # Проверяем чтобы не было дублирования
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler):
            return
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8',
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root.addHandler(file_handler)
    except Exception as e:
        print(f"Не удалось настроить файловый лог: {e}")


logger = _setup_logging()


# ============== НОРМАЛИЗАЦИЯ ССЫЛОК И ЗАГОЛОВКОВ ==============
_TRACKING_PARAMS = re.compile(
    r'^(utm_|ref$|ref_|fbclid|gclid|yclid|mc_|_ga|share_|igshid|si$)',
    re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    """Приводит URL к каноническому виду для сравнения дубликатов:
    - lowercase scheme и host, убираем www.
    - выкидываем utm_*, fbclid, ref и пр. трекинг
    - убираем trailing slash и фрагмент
    """
    if not url or not url.strip():
        return ''
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parsed = urlparse(url.strip())
        scheme = (parsed.scheme or 'https').lower()
        netloc = parsed.netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
              if not _TRACKING_PARAMS.match(k)]
        query = urlencode(qs)
        path = parsed.path.rstrip('/') or '/'
        return urlunparse((scheme, netloc, path, parsed.params, query, ''))
    except Exception:
        return url.strip()


def normalize_title(title: str) -> str:
    """Нормализует заголовок для сравнения: убираем регистр, пробелы, пунктуацию."""
    if not title:
        return ''
    return re.sub(r'[^\w]+', '', title, flags=re.UNICODE).lower()


# ============== HTTP RETRY HELPER ==============
def http_get_with_retry(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: int = HTTP_TIMEOUT,
    proxies: Optional[dict] = None,
    allow_redirects: bool = True,
) -> Optional[requests.Response]:
    """GET с автоматическим retry на сетевых ошибках и 5xx/429.
    Возвращает Response при успехе или None при провале всех попыток.
    Бэкофф: HTTP_RETRY_BACKOFFS = (1, 2, 4) секунд."""
    last_exc = None
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        try:
            r = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=allow_redirects,
            )
            # Успех — возвращаем сразу
            if r.status_code < 500 and r.status_code not in HTTP_RETRY_STATUSES:
                return r
            # 5xx/429 — стоит повторить
            logger.debug(f"HTTP {r.status_code} для {url}, попытка {attempt + 1}/{HTTP_RETRY_ATTEMPTS}")
            last_exc = None
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.debug(f"Сетевая ошибка ({type(e).__name__}) для {url}, попытка {attempt + 1}/{HTTP_RETRY_ATTEMPTS}")
        except requests.RequestException as e:
            # Другие ошибки requests — не ретраим, выходим
            logger.debug(f"Не-ретрайная ошибка для {url}: {e}")
            return None

        # Это была не последняя попытка — пауза перед следующей
        if attempt < HTTP_RETRY_ATTEMPTS - 1:
            backoff = HTTP_RETRY_BACKOFFS[min(attempt, len(HTTP_RETRY_BACKOFFS) - 1)]
            time.sleep(backoff)

    if last_exc:
        logger.warning(f"HTTP не удался после {HTTP_RETRY_ATTEMPTS} попыток для {url}: {last_exc}")
    else:
        logger.warning(f"HTTP не удался после {HTTP_RETRY_ATTEMPTS} попыток для {url}")
    return None


def http_post_with_retry(
    url: str,
    *,
    json_body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Optional[requests.Response]:
    """POST с retry на 5xx/429 и сетевых ошибках."""
    last_exc = None
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        try:
            r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
            if r.status_code < 500 and r.status_code not in HTTP_RETRY_STATUSES:
                return r
            logger.debug(f"HTTP {r.status_code} для POST {url}, попытка {attempt + 1}")
            last_exc = None
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.debug(f"Сетевая ошибка ({type(e).__name__}) для POST {url}, попытка {attempt + 1}")
        except requests.RequestException as e:
            return None

        if attempt < HTTP_RETRY_ATTEMPTS - 1:
            backoff = HTTP_RETRY_BACKOFFS[min(attempt, len(HTTP_RETRY_BACKOFFS) - 1)]
            time.sleep(backoff)

    if last_exc:
        logger.warning(f"POST не удался после {HTTP_RETRY_ATTEMPTS} попыток для {url}: {last_exc}")
    return None


# ============== ХРАНИЛИЩЕ ССЫЛОК ==============
# Стоп-слова заголовков: для fuzzy-сравнения выкидываем только «воду» (глаголы анонсов,
# служебные). Типы контента (movie/manga/season/trailer/cast...) ОСТАВЛЯЕМ — они
# различают разные новости одной франшизы ("X Movie" vs "X Manga Spinoff").
_TITLE_STOPWORDS = frozenset({
    'the', 'and', 'for', 'with', 'from', 'its', 'this', 'that',
    'gets', 'get', 'new', 'more', 'will', 'has', 'have',
    'reveals', 'reveal', 'revealed', 'announces', 'announced', 'announcement',
    'confirms', 'confirmed', 'launches', 'launch', 'debuts', 'debut',
    'additional', 'coming', 'official',
})


def _title_tokens(title: str) -> frozenset:
    """Значимые токены заголовка для сравнения похожести."""
    words = re.findall(r'[\w]+', (title or '').lower())
    return frozenset(w for w in words if len(w) >= 3 and w not in _TITLE_STOPWORDS)


class SentLinksStore:
    """Хранит нормализованные URL и нормализованные заголовки уже отправленных постов.
    Защищает от дублей трёх видов:
    1) Тот же URL (буквально)
    2) Тот же URL после нормализации (с другим UTM, www. и т.п.)
    3) Тот же заголовок (один и тот же контент опубликован на разных URL/источниках)
    """

    def __init__(self, path: Path):
        self.path = path
        self._urls: list[str] = []          # нормализованные URL (для обрезки старых)
        self._url_set: set[str] = set()      # быстрая проверка
        self._titles: list[str] = []         # заголовки в порядке добавления
        self._title_set: set[str] = set()    # нормализованные заголовки
        # Недавние заголовки для fuzzy-дедупа «одна новость с разных источников»:
        # (timestamp, склейка normalize_title, значимые токены). Не персистентно —
        # окно 48ч копится с запуска, при рестарте начинается заново.
        self._recent_titles: deque = deque(maxlen=500)
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            # Совместимость со старым форматом: просто list[str]
            if isinstance(data, list):
                self._urls = [normalize_url(u) for u in data]
                self._url_set = set(self._urls)
                self._titles = []
                self._title_set = set()
                logger.info(f"Загружена старая история ({len(self._urls)} URL), мигрирую в новый формат")
                self._save()
            elif isinstance(data, dict):
                self._urls = data.get('urls', [])
                self._url_set = set(self._urls)
                # titles хранится списком в порядке добавления — так при обрезке
                # выбрасываются самые старые, а не все подряд
                self._titles = [t for t in data.get('titles', []) if t]
                self._title_set = set(self._titles)
                for item in data.get('recent', []):
                    try:
                        ts, norm, tokens = item
                        self._recent_titles.append((float(ts), str(norm), frozenset(tokens)))
                    except (ValueError, TypeError):
                        continue
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать {self.path}: {e}")

    def _save(self) -> None:
        try:
            with self.path.open('w', encoding='utf-8') as f:
                json.dump({
                    'urls': self._urls,
                    'titles': self._titles,
                    # окно fuzzy-дедупа переживает рестарты
                    'recent': [[ts, norm, sorted(tokens)] for ts, norm, tokens in self._recent_titles],
                }, f, ensure_ascii=False)
        except OSError as e:
            logger.error(f"Не удалось сохранить {self.path}: {e}")

    @property
    def _set(self) -> set[str]:
        """Совместимость со старым кодом (для отображения количества)."""
        return self._url_set

    def __contains__(self, link: str) -> bool:
        return normalize_url(link) in self._url_set

    def has_title(self, title: str) -> bool:
        return normalize_title(title) in self._title_set

    def has_similar_title(self, title: str, window_hours: int = 48) -> bool:
        """Fuzzy-проверка: публиковалась ли недавно ПОХОЖАЯ новость (та же новость
        с другого источника, с иной формулировкой заголовка).

        Дубль, если с одним из недавних заголовков (окно window_hours):
        - Жаккар значимых токенов ≥ 0.6 (почти одинаковые формулировки), ИЛИ
        - общая подстрока склеек ≥ 16 символов (длинное уникальное название тайтла,
          напр. 'mamonotsukainomusume' — ловит разные формулировки одного анонса;
          короткие франшизы типа 'attackontitan' (13) порог не проходят — их разные
          новости не склеиваются ложно)."""
        if not title:
            return False
        norm = normalize_title(title)
        tokens = _title_tokens(title)
        now = time.time()
        for ts, old_norm, old_tokens in self._recent_titles:
            if now - ts > window_hours * 3600:
                continue
            if tokens and old_tokens:
                union = len(tokens | old_tokens)
                if union and len(tokens & old_tokens) / union >= 0.6:
                    return True
            if len(norm) >= 16 and len(old_norm) >= 16:
                m = difflib.SequenceMatcher(None, norm, old_norm).find_longest_match(
                    0, len(norm), 0, len(old_norm))
                if m.size >= 16:
                    return True
        return False

    async def claim(self, link: str, title: str = '') -> bool:
        """Атомарно: если ни URL, ни заголовка ещё не было — записывает и возвращает True.
        Если уже было — возвращает False (это дубликат)."""
        norm_url = normalize_url(link)
        norm_title = normalize_title(title)
        async with self._lock:
            if norm_url in self._url_set:
                return False
            if norm_title and norm_title in self._title_set:
                # Заголовок уже был — это дубликат с другого источника
                logger.info(f"Дубль по заголовку, пропускаю: {title[:60]}")
                return False
            if norm_title:
                self._recent_titles.append((time.time(), norm_title, _title_tokens(title)))
            self._add_unlocked(norm_url, norm_title)
            return True

    async def release(self, link: str, title: str = '') -> None:
        """Откатывает claim, если отправка не удалась."""
        norm_url = normalize_url(link)
        norm_title = normalize_title(title)
        async with self._lock:
            if norm_url in self._url_set:
                self._url_set.discard(norm_url)
                try:
                    self._urls.remove(norm_url)
                except ValueError:
                    pass
            if norm_title:
                self._title_set.discard(norm_title)
                try:
                    self._titles.remove(norm_title)
                except ValueError:
                    pass
            self._save()

    def _add_unlocked(self, norm_url: str, norm_title: str) -> None:
        if norm_url not in self._url_set:
            self._urls.append(norm_url)
            self._url_set.add(norm_url)
        if norm_title and norm_title not in self._title_set:
            self._titles.append(norm_title)
            self._title_set.add(norm_title)
        # Чистка старых записей
        if len(self._urls) > SENT_LINKS_MAX:
            self._urls = self._urls[-SENT_LINKS_TRIM_TO:]
            self._url_set = set(self._urls)
            logger.info(f"История ссылок подрезана до {len(self._urls)}")
        if len(self._titles) > SENT_LINKS_MAX:
            # Раньше здесь стирались ВСЕ заголовки разом — и защита от дублей
            # по названию обнулялась на несколько дней вперёд. Теперь режем
            # так же, как URL: выбрасываем самые старые.
            self._titles = self._titles[-SENT_LINKS_TRIM_TO:]
            self._title_set = set(self._titles)
            logger.info(f"История заголовков подрезана до {len(self._titles)}")
        self._save()


sent_links: Optional['SentLinksStore'] = None
translator: Optional[GoogleTranslator] = None


# ============== ОЧЕРЕДЬ ПОСТОВ ==============
QUEUE_FILE = DATA_DIR / 'post_queue.json'
QUEUE_MAX_SIZE = 30                  # больше — старые вытесняются
QUEUE_POST_TTL_HOURS = 24            # пост старше — выбрасывается без отправки

# Свежесть поста (по дате публикации в источнике).
# Посты старше этого порога вообще не попадают в очередь.
# 72ч = 3 дня — это компромисс между свежестью и редко публикующимися источниками
POST_MAX_AGE_HOURS = 72


class PostQueue:
    """FIFO-очередь постов на диске. Хранит уже подготовленные посты,
    которые ждут своего интервала отправки."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []   # каждая запись = {'news': dict, 'queued_at': iso-str}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                self._items = data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать очередь {self.path}: {e}")
            self._items = []

    def _save(self) -> None:
        try:
            with self.path.open('w', encoding='utf-8') as f:
                json.dump(self._items, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Не удалось сохранить очередь: {e}")

    def _is_expired(self, item: dict) -> bool:
        try:
            queued_at = datetime.fromisoformat(item.get('queued_at', ''))
        except (ValueError, TypeError):
            return False
        return datetime.now() - queued_at > timedelta(hours=QUEUE_POST_TTL_HOURS)

    def _purge_expired_unlocked(self) -> int:
        """Удаляет протухшие посты. Возвращает сколько удалено."""
        before = len(self._items)
        self._items = [i for i in self._items if not self._is_expired(i)]
        removed = before - len(self._items)
        if removed:
            logger.info(f"⏰ Из очереди удалено {removed} протухших постов (старше {QUEUE_POST_TTL_HOURS}ч)")
        return removed

    async def push_many(self, news_list: list[dict]) -> int:
        """Кладёт новости в очередь. Возвращает сколько добавлено.
        Если включён require_image — посты без картинок не попадают в очередь."""
        if not news_list:
            return 0
        async with self._lock:
            self._purge_expired_unlocked()
            now_iso = datetime.now().isoformat()
            existing_links = {i['news']['link'] for i in self._items}
            added = 0
            require_img = settings.require_image
            for news in news_list:
                if news['link'] in existing_links:
                    continue
                # Доп. фильтр: посты без картинок не пускаем в очередь
                if require_img and not news.get('images'):
                    continue
                clean_news = {k: v for k, v in news.items() if k != 'published_parsed'}
                self._items.append({'news': clean_news, 'queued_at': now_iso})
                existing_links.add(news['link'])
                added += 1
            if len(self._items) > QUEUE_MAX_SIZE:
                dropped = len(self._items) - QUEUE_MAX_SIZE
                self._items = self._items[-QUEUE_MAX_SIZE:]
                logger.info(f"📦 Очередь переполнена, выброшено {dropped} старых постов")
            self._save()
            return added

    async def pop_next(self) -> Optional[dict]:
        """Достаёт следующий пост из очереди (FIFO). Возвращает news dict или None.
        Если включён require_image — пропускает (выбрасывает) посты без картинок,
        пока не найдёт подходящий или очередь не закончится."""
        async with self._lock:
            self._purge_expired_unlocked()
            require_img = settings.require_image
            skipped = 0
            while self._items:
                item = self._items.pop(0)
                news = item['news']
                if require_img and not news.get('images'):
                    skipped += 1
                    continue
                if skipped:
                    logger.info(f"⊘ Из очереди выброшено {skipped} постов без картинок")
                self._save()
                return news
            if skipped:
                logger.info(f"⊘ Из очереди выброшено {skipped} постов без картинок")
            self._save()
            return None

    async def peek_size(self) -> int:
        async with self._lock:
            self._purge_expired_unlocked()
            self._save()
            return len(self._items)

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._items)
            self._items.clear()
            self._save()
            return count

    async def list_titles(self, limit: int = 10) -> list[str]:
        """Возвращает заголовки первых N постов в очереди."""
        async with self._lock:
            return [i['news'].get('title', '')[:80] for i in self._items[:limit]]


post_queue: Optional['PostQueue'] = None


# ============== RUNTIME-НАСТРОЙКИ (меняются через UI) ==============
SETTINGS_FILE = DATA_DIR / 'bot_settings.json'


class BotSettings:
    """Настройки, которые админ может менять через интерфейс.
    Сохраняются на диск, загружаются при старте."""

    DEFAULTS = {
        'check_interval_min': 30,
        'video_enabled': True,
        'require_image': True,
        'post_max_age_hours': POST_MAX_AGE_HOURS,
        'disabled_sources': [],
        'thread_mode': False,    # True = слать все новости пачкой в ветку обсуждения
        'translator_engine': 'deepl',  # 'deepl' (если ключ задан, с fallback) или 'google' (принудительно)
        'quiet_mode': True,      # True = уведомлять админа только при ошибках + сводка раз в день
        'last_daily_summary': '',  # дата (YYYY-MM-DD) последней ежедневной сводки
        'extra_admins': [],      # дополнительные Telegram ID с правами админа
        'tz_offset': 3,          # часовой пояс админа относительно UTC (МСК = +3)
        'open_moderation': True, # кнопки под постами в ветке доступны всем участникам
        'auto_disable_sources': True,  # сам ставить на паузу умершие источники
        'image_dedup': True,     # отсеивать посты с уже публиковавшейся картинкой
        'dedup_final_text': True,  # сверять готовый текст поста с недавними
        'daily_backup': True,    # ежедневный бэкап данных в личку админу
        'last_backup_date': '',  # дата последнего бэкапа (YYYY-MM-DD)
        'startup_report': True,  # отчёт админам при запуске бота
        'last_publish_at': '',   # когда последний раз что-то опубликовали
        'deepl_month': '',       # месяц, за который считаем символы DeepL
        'deepl_chars': 0,        # израсходовано символов DeepL за месяц
        'llm_enabled': True,     # использовать языковую модель (если задан ключ)
        'llm_rewrite': True,     # брать у модели перевод и текст поста
        'llm_filter': True,      # отсеивать непрофильные новости
        'llm_tags': True,        # добавлять хэштеги
        'llm_read_article': True,  # читать статью, если в ленте только тизер
        'llm_skip_filler': True,   # отсеивать подборки и авторские колонки
        'llm_dedup_subject': True, # ловить одну новость из разных источников
        'llm_limit_repeats': True, # не больше 3 постов про один тайтл в сутки
        'llm_day': '',           # сутки, за которые считаем вызовы
        'llm_calls_today': 0,    # сколько вызовов модели сделано сегодня
    }

    def __init__(self, path: Path):
        self.path = path
        # deepcopy: в DEFAULTS есть списки (extra_admins, disabled_sources) —
        # поверхностная копия шарила бы их между инстансами (mutable default bug)
        self._data: dict = copy.deepcopy(self.DEFAULTS)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open('r', encoding='utf-8') as f:
                loaded = json.load(f)
            # Мерджим с дефолтами, чтобы новые настройки добавлялись автоматически
            for k, v in loaded.items():
                if k in self.DEFAULTS:
                    self._data[k] = v
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать {self.path}: {e}")

    def save(self) -> None:
        try:
            with self.path.open('w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Не удалось сохранить {self.path}: {e}")

    @property
    def check_interval_sec(self) -> int:
        return self._data['check_interval_min'] * 60

    @property
    def check_interval_min(self) -> int:
        return self._data['check_interval_min']

    @check_interval_min.setter
    def check_interval_min(self, value: int) -> None:
        self._data['check_interval_min'] = max(5, int(value))
        self.save()

    @property
    def video_enabled(self) -> bool:
        return self._data['video_enabled']

    @video_enabled.setter
    def video_enabled(self, value: bool) -> None:
        self._data['video_enabled'] = bool(value)
        self.save()

    @property
    def require_image(self) -> bool:
        return self._data.get('require_image', True)

    @require_image.setter
    def require_image(self, value: bool) -> None:
        self._data['require_image'] = bool(value)
        self.save()

    @property
    def post_max_age_hours(self) -> int:
        return self._data.get('post_max_age_hours', POST_MAX_AGE_HOURS)

    @post_max_age_hours.setter
    def post_max_age_hours(self, value: int) -> None:
        self._data['post_max_age_hours'] = max(1, int(value))
        self.save()

    @property
    def thread_mode(self) -> bool:
        return self._data.get('thread_mode', False)

    @thread_mode.setter
    def thread_mode(self, value: bool) -> None:
        self._data['thread_mode'] = bool(value)
        self.save()

    @property
    def translator_engine(self) -> str:
        return self._data.get('translator_engine', 'deepl')

    @translator_engine.setter
    def translator_engine(self, value: str) -> None:
        self._data['translator_engine'] = 'google' if value == 'google' else 'deepl'
        self.save()

    @property
    def quiet_mode(self) -> bool:
        return self._data.get('quiet_mode', True)

    @quiet_mode.setter
    def quiet_mode(self, value: bool) -> None:
        self._data['quiet_mode'] = bool(value)
        self.save()

    @property
    def last_daily_summary(self) -> str:
        return self._data.get('last_daily_summary', '')

    @last_daily_summary.setter
    def last_daily_summary(self, value: str) -> None:
        self._data['last_daily_summary'] = str(value)
        self.save()

    @property
    def tz_offset(self) -> int:
        try:
            return int(self._data.get('tz_offset', 3))
        except (TypeError, ValueError):
            return 3

    @tz_offset.setter
    def tz_offset(self, value: int) -> None:
        self._data['tz_offset'] = max(-12, min(14, int(value)))
        self.save()

    @property
    def auto_disable_sources(self) -> bool:
        return bool(self._data.get('auto_disable_sources', True))

    @auto_disable_sources.setter
    def auto_disable_sources(self, value: bool) -> None:
        self._data['auto_disable_sources'] = bool(value)
        self.save()

    @property
    def dedup_final_text(self) -> bool:
        return bool(self._data.get('dedup_final_text', True))

    @dedup_final_text.setter
    def dedup_final_text(self, value: bool) -> None:
        self._data['dedup_final_text'] = bool(value)
        self.save()

    @property
    def image_dedup(self) -> bool:
        return bool(self._data.get('image_dedup', True))

    @image_dedup.setter
    def image_dedup(self, value: bool) -> None:
        self._data['image_dedup'] = bool(value)
        self.save()

    @property
    def llm_enabled(self) -> bool:
        return bool(self._data.get('llm_enabled', True))

    @llm_enabled.setter
    def llm_enabled(self, value: bool) -> None:
        self._data['llm_enabled'] = bool(value)
        self.save()

    @property
    def llm_rewrite(self) -> bool:
        return bool(self._data.get('llm_rewrite', True))

    @llm_rewrite.setter
    def llm_rewrite(self, value: bool) -> None:
        self._data['llm_rewrite'] = bool(value)
        self.save()

    @property
    def llm_filter(self) -> bool:
        return bool(self._data.get('llm_filter', True))

    @llm_filter.setter
    def llm_filter(self, value: bool) -> None:
        self._data['llm_filter'] = bool(value)
        self.save()

    @property
    def llm_tags(self) -> bool:
        return bool(self._data.get('llm_tags', True))

    @llm_tags.setter
    def llm_tags(self, value: bool) -> None:
        self._data['llm_tags'] = bool(value)
        self.save()

    @property
    def llm_dedup_subject(self) -> bool:
        return bool(self._data.get('llm_dedup_subject', True))

    @llm_dedup_subject.setter
    def llm_dedup_subject(self, value: bool) -> None:
        self._data['llm_dedup_subject'] = bool(value)
        self.save()

    @property
    def llm_limit_repeats(self) -> bool:
        return bool(self._data.get('llm_limit_repeats', True))

    @llm_limit_repeats.setter
    def llm_limit_repeats(self, value: bool) -> None:
        self._data['llm_limit_repeats'] = bool(value)
        self.save()

    @property
    def llm_read_article(self) -> bool:
        return bool(self._data.get('llm_read_article', True))

    @llm_read_article.setter
    def llm_read_article(self, value: bool) -> None:
        self._data['llm_read_article'] = bool(value)
        self.save()

    @property
    def llm_skip_filler(self) -> bool:
        return bool(self._data.get('llm_skip_filler', True))

    @llm_skip_filler.setter
    def llm_skip_filler(self, value: bool) -> None:
        self._data['llm_skip_filler'] = bool(value)
        self.save()

    @property
    def llm_day(self) -> str:
        return str(self._data.get('llm_day', ''))

    @llm_day.setter
    def llm_day(self, value: str) -> None:
        self._data['llm_day'] = str(value)
        self.save()

    @property
    def llm_calls_today(self) -> int:
        try:
            return int(self._data.get('llm_calls_today', 0))
        except (TypeError, ValueError):
            return 0

    @llm_calls_today.setter
    def llm_calls_today(self, value: int) -> None:
        self._data['llm_calls_today'] = max(0, int(value))
        self.save()

    @property
    def startup_report(self) -> bool:
        return bool(self._data.get('startup_report', True))

    @startup_report.setter
    def startup_report(self, value: bool) -> None:
        self._data['startup_report'] = bool(value)
        self.save()

    @property
    def last_publish_at(self) -> str:
        return str(self._data.get('last_publish_at', ''))

    @last_publish_at.setter
    def last_publish_at(self, value: str) -> None:
        self._data['last_publish_at'] = str(value)
        self.save()

    @property
    def deepl_month(self) -> str:
        return str(self._data.get('deepl_month', ''))

    @deepl_month.setter
    def deepl_month(self, value: str) -> None:
        self._data['deepl_month'] = str(value)
        self.save()

    @property
    def deepl_chars(self) -> int:
        try:
            return int(self._data.get('deepl_chars', 0))
        except (TypeError, ValueError):
            return 0

    @deepl_chars.setter
    def deepl_chars(self, value: int) -> None:
        self._data['deepl_chars'] = max(0, int(value))
        self.save()

    @property
    def daily_backup(self) -> bool:
        return bool(self._data.get('daily_backup', True))

    @daily_backup.setter
    def daily_backup(self, value: bool) -> None:
        self._data['daily_backup'] = bool(value)
        self.save()

    @property
    def last_backup_date(self) -> str:
        return str(self._data.get('last_backup_date', ''))

    @last_backup_date.setter
    def last_backup_date(self, value: str) -> None:
        self._data['last_backup_date'] = str(value)
        self.save()

    @property
    def open_moderation(self) -> bool:
        return bool(self._data.get('open_moderation', True))

    @open_moderation.setter
    def open_moderation(self, value: bool) -> None:
        self._data['open_moderation'] = bool(value)
        self.save()

    @property
    def extra_admins(self) -> list[int]:
        return [int(x) for x in self._data.get('extra_admins', [])]

    def add_admin(self, user_id: int) -> bool:
        ids = self._data.setdefault('extra_admins', [])
        if int(user_id) in [int(x) for x in ids] or int(user_id) == ADMIN_ID:
            return False
        ids.append(int(user_id))
        self.save()
        return True

    def remove_admin(self, user_id: int) -> bool:
        ids = self._data.get('extra_admins', [])
        new = [x for x in ids if int(x) != int(user_id)]
        if len(new) == len(ids):
            return False
        self._data['extra_admins'] = new
        self.save()
        return True

    def is_source_enabled(self, source_name: str) -> bool:
        return source_name.lower() not in [s.lower() for s in self._data['disabled_sources']]

    def toggle_source(self, source_name: str) -> bool:
        """Переключает источник. Возвращает новое состояние (True = включён)."""
        disabled = [s.lower() for s in self._data['disabled_sources']]
        key = source_name.lower()
        if key in disabled:
            self._data['disabled_sources'] = [s for s in self._data['disabled_sources'] if s.lower() != key]
            new_state = True
            if source_health is not None:
                source_health.reset(source_name)   # включили руками — даём чистый старт
        else:
            self._data['disabled_sources'].append(source_name)
            new_state = False
        self.save()
        return new_state


settings: Optional['BotSettings'] = None


# ============== МЕТРИКИ ==============
STATS_FILE = DATA_DIR / 'bot_stats.json'
STATS_EVENTS_MAX = 2000             # храним максимум N последних событий для расчётов «за период»


class BotStats:
    """Накопительная статистика по постам и источникам.

    Хранит:
    - Накопительные счётчики (total_*, by_source) — за всё время с первого запуска
    - Лог последних N событий (timestamp + тип + источник) — для расчётов «за сутки/неделя»

    Атомарность через asyncio.Lock. Запись на диск при каждом изменении.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = self._default_data()
        self._lock = asyncio.Lock()
        self._load()

    @staticmethod
    def _default_data() -> dict:
        return {
            'bot_started_at': datetime.now().isoformat(),
            'totals': {
                'collected': 0,           # всего собрано из источников
                'published': 0,           # всего опубликовано в канал
                'skipped_no_image': 0,    # отброшено без картинок
                'skipped_too_old': 0,     # отброшено по возрасту
                'skipped_duplicate': 0,   # отброшено как дубль
                'skipped_spam': 0,        # Reddit-megathread и подобное
                'skipped_filtered': 0,    # отсеяно языковой моделью как не по теме
                'failed_send': 0,         # реальные ошибки отправки в Telegram
                'source_errors': 0,       # источник упал при сборе
            },
            'by_source': {},              # name -> {collected, published, errors, last_success_at}
            'events': [],                 # последние события: [{at, type, source}, ...]
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Мягкое слияние с дефолтами на случай новых полей
                merged = self._default_data()
                merged['bot_started_at'] = data.get('bot_started_at', merged['bot_started_at'])
                merged['totals'].update(data.get('totals', {}))
                merged['by_source'].update(data.get('by_source', {}))
                merged['events'] = data.get('events', [])[-STATS_EVENTS_MAX:]
                self._data = merged
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать {self.path}: {e}")

    def _save(self) -> None:
        try:
            with self.path.open('w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False)
        except OSError as e:
            logger.error(f"Не удалось сохранить {self.path}: {e}")

    def _add_event_unlocked(self, event_type: str, source: Optional[str] = None) -> None:
        """Добавляет событие в лог. Без блокировки — вызывается из locked-методов."""
        event = {'at': datetime.now().isoformat(), 'type': event_type}
        if source:
            event['source'] = source
        self._data['events'].append(event)
        # Обрезаем чтобы не разрасталось
        if len(self._data['events']) > STATS_EVENTS_MAX:
            self._data['events'] = self._data['events'][-STATS_EVENTS_MAX:]

    def _ensure_source_unlocked(self, source: str) -> dict:
        """Возвращает (создаёт если нужно) запись по источнику."""
        if source not in self._data['by_source']:
            self._data['by_source'][source] = {
                'collected': 0,
                'published': 0,
                'errors': 0,
                'last_success_at': None,
            }
        return self._data['by_source'][source]

    # === Методы для записи событий ===
    async def record_collected(self, source: str, count: int) -> None:
        """Собрали N постов из источника (после всех фильтров)."""
        if count <= 0:
            return
        async with self._lock:
            self._data['totals']['collected'] += count
            entry = self._ensure_source_unlocked(source)
            entry['collected'] += count
            entry['last_success_at'] = datetime.now().isoformat()
            self._add_event_unlocked('collected', source)
            self._save()

    async def record_source_error(self, source: str) -> None:
        """Источник упал при сборе."""
        async with self._lock:
            self._data['totals']['source_errors'] += 1
            entry = self._ensure_source_unlocked(source)
            entry['errors'] += 1
            self._add_event_unlocked('source_error', source)
            self._save()

    async def record_published(self, source: str) -> None:
        """Пост опубликован в канал."""
        async with self._lock:
            self._data['totals']['published'] += 1
            entry = self._ensure_source_unlocked(source)
            entry['published'] += 1
            self._add_event_unlocked('published', source)
            self._save()

    async def record_skipped(self, reason: str, source: Optional[str] = None) -> None:
        """Пост отброшен. reason: no_image / too_old / duplicate / spam / filtered."""
        key = f'skipped_{reason}'
        async with self._lock:
            if key in self._data['totals']:
                self._data['totals'][key] += 1
            self._add_event_unlocked(key, source)
            self._save()

    async def record_failed_send(self, source: Optional[str] = None) -> None:
        """Реальная ошибка отправки в Telegram."""
        async with self._lock:
            self._data['totals']['failed_send'] += 1
            self._add_event_unlocked('failed_send', source)
            self._save()

    # === Чтение ===
    def get_totals(self) -> dict:
        return dict(self._data['totals'])

    def get_by_source(self) -> dict:
        return dict(self._data['by_source'])

    def get_started_at(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self._data['bot_started_at'])
        except (ValueError, TypeError):
            return None

    def count_events_since(self, since: datetime, event_type: Optional[str] = None) -> int:
        """Сколько событий произошло после момента since.
        Если event_type указан — фильтрует по нему."""
        count = 0
        for ev in self._data['events']:
            try:
                ev_at = datetime.fromisoformat(ev['at'])
            except (ValueError, TypeError, KeyError):
                continue
            if ev_at < since:
                continue
            if event_type and ev.get('type') != event_type:
                continue
            count += 1
        return count


stats: Optional['BotStats'] = None


# ============== СЛОВАРИ ЗАМЕН ==============
# Защищённые термины — не переводятся вовсе. Подставляются плейсхолдеры на время перевода.
PROTECTED_TERMS = [
    # --- Стриминговые платформы и сервисы ---
    'Crunchyroll', 'Netflix', 'Disney+', 'HIDIVE', 'Funimation', 'Aniplex',
    'Amazon Prime Video', 'Prime Video', 'Hulu', 'Bilibili', 'Ani-One',
    'Muse Asia', 'YouTube', 'Max', 'HBO Max',
    # --- Студии анимации ---
    'MAPPA', 'Bones', 'Bones Film', 'Madhouse', 'Wit Studio', 'Studio Ghibli', 'Sunrise',
    'Toei Animation', 'Kyoto Animation', 'Trigger', 'Ufotable', 'CloverWorks',
    'A-1 Pictures', 'Production I.G', 'Shaft', 'David Production', 'P.A. Works',
    'J.C. Staff', 'OLM', 'TMS Entertainment', 'Studio Pierrot', 'Pierrot', 'White Fox',
    'MAHO FILM', 'Doga Kobo', 'Gainax', 'Khara', 'Science SARU', 'Studio Bind',
    'Lerche', 'Silver Link', 'Passione', 'Studio Deen', 'Brain\'s Base',
    'Kinema Citrus', 'Orange', 'Polygon Pictures', 'GoHands', 'Feel', 'Zexcs',
    'Bibury Animation Studios', 'Nut', 'Encourage Films', 'Tatsunoko',
    'Wawayu Animation', 'Yokohama Animation Lab', 'EMT Squared', 'Drive',
    # --- Издатели / манга-платформы ---
    'Shogakukan', 'Kodansha', 'Shueisha', 'Kadokawa', 'Square Enix', 'ASCII Media Works',
    'Manga UP!', 'MangaPlus', 'Manga Plus', 'K Manga', 'Comikey', 'Azuki',
    'Yen Press', 'Seven Seas', 'Viz Media', 'VIZ', 'Dark Horse',
    'Weekly Shonen Jump', 'Shonen Jump', 'Young Jump', 'Weekly Shonen Magazine',
    'Shonen Sunday', 'Comic Yuri Hime', 'Dengeki', 'Gangan', 'Afternoon',
    # --- Издания/сервисы новостей ---
    'MyAnimeList', 'AnimeCorner', 'Anime Corner',
    'Honey\'s Anime', 'Anime News Network', 'AnimeJapan', 'Anime Expo',
    # --- Тайтлы которые Google часто коверкает ---
    'ONE PIECE', 'BanG Dream', 'YUMEMITA', 'Kaiju No. 8', 'Kaiju No.8',
    'Solo Leveling', 'Frieren', 'Sousou no Frieren', 'Dandadan', 'Dan Da Dan',
    'Chainsaw Man', 'Jujutsu Kaisen', 'Spy x Family', 'Oshi no Ko',
    'Blue Lock', 'Blue Box', 'Wind Breaker', 'Sakamoto Days',
    'Demon Slayer', 'Kimetsu no Yaiba', 'My Hero Academia', 'Boku no Hero Academia',
    'Attack on Titan', 'Shingeki no Kyojin', 'Hunter x Hunter',
    'Re:Zero', 'Mushoku Tensei', 'Overlord', 'Konosuba',
    'Fate/stay night', 'Fate/Grand Order', 'Fate/Zero',
    'Gundam', 'Mobile Suit Gundam', 'Evangelion', 'Neon Genesis Evangelion',
    'Vinland Saga', 'Golden Kamuy', 'Dr. Stone', 'Dr. STONE',
    'Tokyo Revengers', 'Bleach', 'Naruto', 'Boruto', 'Dragon Ball',
    'Dragon Ball Super', 'Dragon Ball Daima', 'Undead Unluck',
    'The Apothecary Diaries', 'Kusuriya no Hitorigoto',
    'Delicious in Dungeon', 'Dungeon Meshi',
    'Zenshu', 'Medalist', 'Rurouni Kenshin', 'Bakemonogatari', 'Monogatari',
    # --- Кино / сериалы / гик (канал расширен) ---
    'Marvel Studios', 'Marvel', 'DC Studios', 'Warner Bros.', 'Warner Bros',
    'Paramount', 'Lucasfilm', 'Pixar', 'A24', 'Sony Pictures', 'Universal Pictures',
    'Star Wars', 'Star Trek', 'The Witcher', 'Stranger Things',
    'House of the Dragon', 'Game of Thrones', 'The Boys', 'The Mandalorian',
    'Mission: Impossible', 'Jurassic World', 'James Bond', 'Blade Runner',
    'The Last of Us', 'Fallout', 'Cyberpunk 2077', 'Cyberpunk',
]

# Названия-заглушки для случаев когда Google переводит имя собственное дословно.
# Ключ — как Google перевёл (в нижнем регистре), значение — правильная форма.
# Применяется в POST_TRANSLATION_REPLACEMENTS ниже.

# Замены терминов после перевода (формальный → литературный анимешный сленг)
POST_TRANSLATION_REPLACEMENTS = [
    # --- Опенинги/эндинги ---
    (r'\bвступительная музыкальная тема\b', 'опенинг', re.IGNORECASE),
    (r'\bвступительная тема\b', 'опенинг', re.IGNORECASE),
    (r'\bтематическая песня открытия\b', 'опенинг', re.IGNORECASE),
    (r'\bпесня открытия\b', 'опенинг', re.IGNORECASE),
    (r'\bоткрывающая тема\b', 'опенинг', re.IGNORECASE),
    (r'\bоткрывающая песня\b', 'опенинг', re.IGNORECASE),
    (r'\bопенинг тема\b', 'опенинг', re.IGNORECASE),
    (r'\bopening тема\b', 'опенинг', re.IGNORECASE),
    (r'\bглавная тема\b', 'опенинг', re.IGNORECASE),
    (r'\bзаключительная тема\b', 'эндинг', re.IGNORECASE),
    (r'\bзакрывающая тема\b', 'эндинг', re.IGNORECASE),
    (r'\bзакрывающая песня\b', 'эндинг', re.IGNORECASE),
    (r'\bфинальная песня\b', 'эндинг', re.IGNORECASE),
    (r'\bending тема\b', 'эндинг', re.IGNORECASE),
    (r'\bтематическая песня\b', 'музыкальная тема', re.IGNORECASE),

    # --- Демографические жанры (Google переводит громоздко) ---
    (r'\bсёнэн[- ]демографическ\w+\b', 'сёнэн', re.IGNORECASE),
    (r'\bсёдзё[- ]демографическ\w+\b', 'сёдзё', re.IGNORECASE),
    (r'\bсэйнэн[- ]демографическ\w+\b', 'сэйнэн', re.IGNORECASE),
    (r'\bдзёсэй[- ]демографическ\w+\b', 'дзёсэй', re.IGNORECASE),
    (r'\bдемографи\w+ сёнэн\b', 'сёнэн', re.IGNORECASE),
    (r'\bцелевая аудитория сёнэн\b', 'сёнэн', re.IGNORECASE),

    # --- Форматы релизов ---
    (r'\bкомпакт-диск\b', 'CD', re.IGNORECASE),
    (r'\bна компакт-диске\b', 'на CD', re.IGNORECASE),
    (r'\bDVD-релиз\b', 'релиз на DVD', re.IGNORECASE),
    (r'\bБлю-рей\b', 'Blu-ray', re.IGNORECASE),
    (r'\bблю-рей\b', 'Blu-ray', re.IGNORECASE),
    (r'\bБлюрей\b', 'Blu-ray', re.IGNORECASE),
    (r'\bкоробочный набор\b', 'бокс-сет', re.IGNORECASE),

    # --- ТВ-аниме и форматы ---
    (r'\bТелевизионное аниме\b', 'ТВ-аниме', re.IGNORECASE),
    (r'\bтелевизионный аниме-сериал\b', 'ТВ-аниме', re.IGNORECASE),
    (r'\bтелесериал аниме\b', 'ТВ-аниме', re.IGNORECASE),
    (r'\bТВ аниме\b', 'ТВ-аниме', re.IGNORECASE),
    (r'\bаниме сериал\b', 'аниме-сериал', re.IGNORECASE),
    (r'\bаниме фильм\b', 'аниме-фильм', re.IGNORECASE),
    (r'\bаниме-телесериал\b', 'ТВ-аниме', re.IGNORECASE),
    (r'\bманга серия\b', 'манга', re.IGNORECASE),
    (r'\bсерия манги\b', 'манга', re.IGNORECASE),
    (r'\bлайт-новелла\b', 'ранобэ', re.IGNORECASE),
    (r'\bлайт-новелл[ыеу]?\b', 'ранобэ', re.IGNORECASE),
    (r'\bлёгкая новелла\b', 'ранобэ', re.IGNORECASE),
    (r'\bл[её]гкие новеллы\b', 'ранобэ', re.IGNORECASE),
    (r'\bл[её]гкие романы\b', 'ранобэ', re.IGNORECASE),
    (r'\bл[её]гких романов\b', 'ранобэ', re.IGNORECASE),
    (r'\bлёгкий роман\b', 'ранобэ', re.IGNORECASE),
    (r'\bлегкий роман\b', 'ранобэ', re.IGNORECASE),
    (r'\bграфический роман\b', 'манга', re.IGNORECASE),

    # --- Производство/сезоны ---
    (r'\bвторой сезон\b', '2 сезон', re.IGNORECASE),
    (r'\bтретий сезон\b', '3 сезон', re.IGNORECASE),
    (r'\bпервый сезон\b', '1 сезон', re.IGNORECASE),
    (r'\bчетвёртый сезон\b', '4 сезон', re.IGNORECASE),
    (r'\bзеленый свет\b', 'анонсирован', re.IGNORECASE),
    (r'\bдали зелёный свет\b', 'анонсировали', re.IGNORECASE),
    (r'\bполучил зелёный свет\b', 'анонсирован', re.IGNORECASE),
    (r'\bбыл подтверждён\b', 'подтверждён', re.IGNORECASE),
    (r'\bбыло подтверждено\b', 'подтверждено', re.IGNORECASE),

    # --- Персонажи/сюжет ---
    (r'\bактёр озвучивания\b', 'сэйю', re.IGNORECASE),
    (r'\bактриса озвучивания\b', 'сэйю', re.IGNORECASE),
    (r'\bактёр озвучки\b', 'сэйю', re.IGNORECASE),
    (r'\bголосовой актёр\b', 'сэйю', re.IGNORECASE),
    (r'\bголосовой состав\b', 'актёры озвучки', re.IGNORECASE),

    # --- Дубляжи ---

    # --- Даты и события ---
    (r'\bпразднование мамы\b', 'День матери', re.IGNORECASE),

    # --- Пунктуация ---
    (r' - ', ' — ', 0),  # короткие тире → длинные

    # --- Часто встречающиеся косяки с названиями ---
    (r'\bАНИ-МОЖЕТ\b', 'ANI-MAY', 0),
    (r'\bАни-Мэй\b', 'ANI-MAY', 0),
    (r'\bманга ВВЕРХ\b', 'Manga UP', re.IGNORECASE),
    (r'\bМанга Вверх\b', 'Manga UP', re.IGNORECASE),
    (r'\bЗолотого Камуи\b', 'Golden Kamuy', 0),
    (r'\bЗолотой Камуи\b', 'Golden Kamuy', 0),
    (r'\bВосхождение книжного червя\b', 'Восхождение в Тени Книжного Червя', 0),
    (r'\bКласс убийц\b', 'Класс убийств', 0),
    (r'\bбанГ-мечта\b', 'BanG Dream', re.IGNORECASE),
    (r'\bбанг-мечта\b', 'BanG Dream', re.IGNORECASE),
    (r'\bатака титанов\b', 'Атака Титанов', re.IGNORECASE),
    (r'\bубийца демонов\b', 'Demon Slayer', re.IGNORECASE),
    (r'\bмоя геройская академия\b', 'Моя геройская академия', re.IGNORECASE),
    (r'\bчеловек бензопила\b', 'Chainsaw Man', re.IGNORECASE),
    (r'\bсемья шпионов\b', 'Spy x Family', re.IGNORECASE),
    (r'\bшпион х семья\b', 'Spy x Family', re.IGNORECASE),
    (r'\bодиночное повышение уровня\b', 'Solo Leveling', re.IGNORECASE),
    (r'\bповышение уровня в одиночку\b', 'Solo Leveling', re.IGNORECASE),
    (r'\bсиняя тюрьма\b', 'Blue Lock', re.IGNORECASE),
    (r'\bсиняя коробка\b', 'Blue Box', re.IGNORECASE),
    (r'\bкайдзю №8\b', 'Kaiju No. 8', re.IGNORECASE),
    (r'\bмагическая битва\b', 'Jujutsu Kaisen', re.IGNORECASE),
    (r'\bдневник аптекаря\b', 'The Apothecary Diaries', re.IGNORECASE),
]


# ============== ОЧИСТКА ТЕКСТА ==============
def clean_shortcodes(text: str) -> str:
    """Убирает WordPress-шорткоды вида [tag attr="..."]content[/tag] и одиночные [tag]."""
    if not text:
        return ''
    # Парные [tag]...[/tag]
    text = re.sub(r'\[([a-zA-Z][\w-]*)[^\]]*\].*?\[/\1\]', '', text, flags=re.DOTALL)
    # Одиночные [tag ...] и [/tag]
    text = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', text)
    return text


def clean_html(text: str) -> str:
    """Полная очистка: теги, шорткоды, HTML-сущности, неразрывные пробелы."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', text)
    text = clean_shortcodes(text)
    text = html.unescape(text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def smart_truncate(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    # Не оставляем "хвост" в виде запятой/тире
    cut = cut.rstrip(',—-:;')
    return cut + '…'


# ============== ОПРЕДЕЛЕНИЕ И ФОРМАТ ЭПИЗОДОВ ==============
EPISODE_PATTERNS = [
    re.compile(r'^(?P<title>.+?)\s*[—\-–]\s*(?:Episode|Ep\.?)\s*(?P<num>\d+)(?:\s*[—\-–]\s*(?P<sub>.+))?$', re.IGNORECASE),
    re.compile(r'^(?P<title>.+?)\s*[—\-–]\s*Серия\s*(?P<num>\d+)(?:\s*[—\-–]\s*(?P<sub>.+))?$', re.IGNORECASE),
    re.compile(r'^(?P<title>.+?)\s*[—\-–]\s*Эпизод\s*(?P<num>\d+)(?:\s*[—\-–]\s*(?P<sub>.+))?$', re.IGNORECASE),
]

DUB_MARKERS = [
    (re.compile(r'\(English Dub\)', re.IGNORECASE), 'английский дубляж'),
    (re.compile(r'\(German Dub\)', re.IGNORECASE), 'немецкий дубляж'),
    (re.compile(r'\(Spanish Dub\)', re.IGNORECASE), 'испанский дубляж'),
    (re.compile(r'\(Russian Dub\)', re.IGNORECASE), 'русский дубляж'),
    (re.compile(r'\(French Dub\)', re.IGNORECASE), 'французский дубляж'),
    (re.compile(r'\(Portuguese Dub\)', re.IGNORECASE), 'португальский дубляж'),
    (re.compile(r'\(Italian Dub\)', re.IGNORECASE), 'итальянский дубляж'),
]

RU_MONTHS = {
    1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
    5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
    9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря',
}

# Именительный падеж — для «май 2027» (без дня)
RU_MONTHS_NOM = {
    1: 'январь', 2: 'февраль', 3: 'март', 4: 'апрель',
    5: 'май', 6: 'июнь', 7: 'июль', 8: 'август',
    9: 'сентябрь', 10: 'октябрь', 11: 'ноябрь', 12: 'декабрь',
}

_EN_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

_EN_SEASONS = {'spring': 'весна', 'summer': 'лето', 'fall': 'осень', 'autumn': 'осень', 'winter': 'зима'}

_MONTH_RE = (
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December'
    r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)'
)

# Паттерны дат в английском тексте, в порядке проверки.
# Каждый: (compiled_regex, kind), где kind определяет формат вывода.
_DATE_PATTERNS = [
    # Японские: 2026年11月6日 / 11月6日 / 2026年10月
    (re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日'), 'jymd'),
    (re.compile(r'(?<!年)(?<!\d)(\d{1,2})月(\d{1,2})日'), 'jmd'),
    (re.compile(r'(\d{4})年(\d{1,2})月(?!\d{0,2}日)'), 'jym'),
    # August 12, 2026 / Aug. 12 2026 / August 12th, 2026
    (re.compile(rf'\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b', re.IGNORECASE), 'mdy'),
    # 12 August 2026 / 12th August, 2026
    (re.compile(rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?,?\s+(\d{{4}})\b', re.IGNORECASE), 'dmy'),
    # May 2027
    (re.compile(rf'\b({_MONTH_RE})\.?\s+(\d{{4}})\b', re.IGNORECASE), 'my'),
    # Spring 2027 / Fall 2026
    (re.compile(r'\b(Spring|Summer|Fall|Autumn|Winter)\s+(\d{4})\b', re.IGNORECASE), 'sy'),
    # August 12 (без года; не должно быть года следом — это уже поймал mdy)
    (re.compile(rf'\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{{4}})', re.IGNORECASE), 'md'),
    # 12 August (без года)
    (re.compile(rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\b(?!\.?,?\s+\d{{4}})', re.IGNORECASE), 'dm'),
]

# Приоритет конкретности (меньше = конкретнее) — для сортировки при равных позициях
_KIND_PRIORITY = {'mdy': 0, 'dmy': 0, 'jymd': 0, 'my': 1, 'jym': 1, 'sy': 2, 'md': 3, 'dm': 3, 'jmd': 3}


def extract_release_date_from_text(text: str) -> str:
    """Ищет дату выхода/события в английском тексте новости.
    Возвращает русскую строку («12 августа 2026», «май 2027», «весна 2027», «12 августа»)
    или '' если конкретной даты в тексте нет.

    Берётся ПЕРВАЯ дата по позиции в тексте (обычно она относится к главному событию).
    Годы вне разумного диапазона отбрасываются."""
    if not text:
        return ''

    year_now = datetime.now().year
    year_min, year_max = year_now - 1, year_now + 6

    candidates: list[tuple[int, int, str]] = []  # (позиция, приоритет, готовая строка)

    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                if kind == 'jymd':
                    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if not (1 <= month <= 12) or not (1 <= day <= 31) or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]} {year}'
                elif kind == 'jmd':
                    month, day = int(m.group(1)), int(m.group(2))
                    if not (1 <= month <= 12) or not (1 <= day <= 31):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]}'
                elif kind == 'jym':
                    year, month = int(m.group(1)), int(m.group(2))
                    if not (1 <= month <= 12) or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{RU_MONTHS_NOM[month]} {year}'
                elif kind == 'mdy':
                    month = _EN_MONTHS.get(m.group(1).lower().rstrip('.'))
                    day, year = int(m.group(2)), int(m.group(3))
                    if not month or not (1 <= day <= 31) or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]} {year}'
                elif kind == 'dmy':
                    day = int(m.group(1))
                    month = _EN_MONTHS.get(m.group(2).lower().rstrip('.'))
                    year = int(m.group(3))
                    if not month or not (1 <= day <= 31) or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]} {year}'
                elif kind == 'my':
                    month = _EN_MONTHS.get(m.group(1).lower().rstrip('.'))
                    year = int(m.group(2))
                    if not month or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{RU_MONTHS_NOM[month]} {year}'
                elif kind == 'sy':
                    season = _EN_SEASONS.get(m.group(1).lower())
                    year = int(m.group(2))
                    if not season or not (year_min <= year <= year_max):
                        continue
                    formatted = f'{season} {year}'
                elif kind == 'md':
                    month = _EN_MONTHS.get(m.group(1).lower().rstrip('.'))
                    day = int(m.group(2))
                    if not month or not (1 <= day <= 31):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]}'
                elif kind == 'dm':
                    day = int(m.group(1))
                    month = _EN_MONTHS.get(m.group(2).lower().rstrip('.'))
                    if not month or not (1 <= day <= 31):
                        continue
                    formatted = f'{day} {RU_MONTHS[month]}'
                else:
                    continue
                candidates.append((m.start(), _KIND_PRIORITY[kind], formatted))
            except (ValueError, IndexError, KeyError):
                continue

    if not candidates:
        return ''
    # Первая по позиции; при равной позиции — конкретнее
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def parse_episode(title: str) -> Optional[dict]:
    """Если заголовок описывает эпизод — возвращает dict с полями. Иначе None."""
    # Сначала вычленяем дубляж
    dub = None
    title_clean = title
    for pat, label in DUB_MARKERS:
        if pat.search(title):
            dub = label
            title_clean = pat.sub('', title).strip()
            break

    for pattern in EPISODE_PATTERNS:
        m = pattern.match(title_clean)
        if m:
            anime_title = m.group('title').strip().rstrip('-—–:').strip()
            return {
                'anime_title': anime_title,
                'episode_num': m.group('num'),
                'dub': dub,
            }
    return None


def format_release_date(published_struct) -> str:
    """Формирует фразу 'выходит уже сегодня' / 'выйдет N мая' по дате публикации RSS."""
    if not published_struct:
        return 'Серия уже доступна.'
    try:
        pub_date = datetime(*published_struct[:6])
    except (TypeError, ValueError):
        return 'Серия уже доступна.'

    today = datetime.now().date()
    pub_day = pub_date.date()
    delta = (pub_day - today).days

    if delta < 0:
        # Уже вышло (RSS отстаёт)
        return 'Серия уже доступна.'
    if delta == 0:
        return 'Серия выходит уже сегодня.'
    if delta == 1:
        return 'Серия выходит завтра.'
    if delta < 14:
        day = pub_day.day
        month = RU_MONTHS[pub_day.month]
        return f'Серия выйдет {day} {month}.'
    # Больше двух недель — наверное это что-то странное, не пишем дату
    return 'Серия скоро выйдет.'


def format_episode_post(ep: dict, published_struct) -> str:
    """Финальный текст для эпизод-поста."""
    title = ep['anime_title']
    # Оборачиваем название в «ёлочки», если ещё не обёрнуто
    if not (title.startswith('«') or title.startswith('"')):
        title = f'«{title}»'

    line1 = f'{title} — серия {ep["episode_num"]}'
    if ep['dub']:
        line1 += f' ({ep["dub"]})'

    date_phrase = format_release_date(published_struct)
    return f'{line1}\n\n{date_phrase}'


# ============== ANILIST API (распознавание аниме/манги) ==============
class AniListClient:
    """Синхронный клиент к AniList GraphQL для проверки является ли строка названием
    аниме или манги. Используется чтобы дополнительно защищать названия от перевода.

    Кеш двухуровневый:
    - В памяти (мгновенно)
    - На диске anilist_cache.json (переживает перезапуск)

    Хранит и положительные ("найдено"), и отрицательные ("не найдено") результаты.
    """

    QUERY_ANIME = """
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        id
        title { romaji english native }
      }
    }
    """

    QUERY_MANGA = """
    query ($search: String) {
      Media(search: $search, type: MANGA) {
        id
        title { romaji english native }
      }
    }
    """

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            with self.cache_path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
            logger.info(f"AniList cache loaded: {len(self._cache)} entries")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать AniList кеш: {e}")
            self._cache = {}

    def _save(self) -> None:
        try:
            with self.cache_path.open('w', encoding='utf-8') as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except OSError as e:
            logger.error(f"Не удалось сохранить AniList кеш: {e}")

    @staticmethod
    def _norm_key(query: str) -> str:
        return re.sub(r'\s+', ' ', query.strip().lower())

    def _is_cache_fresh(self, entry: dict) -> bool:
        try:
            checked = datetime.fromisoformat(entry.get('checked_at', ''))
        except (ValueError, TypeError):
            return False
        age = datetime.now() - checked
        ttl = ANILIST_CACHE_TTL_DAYS if entry.get('found') else ANILIST_NEGATIVE_TTL_DAYS
        return age < timedelta(days=ttl)

    def _query_api(self, search: str, manga: bool = False) -> Optional[dict]:
        """Один HTTP запрос с retry. Возвращает {romaji, english, native} или None."""
        query = self.QUERY_MANGA if manga else self.QUERY_ANIME
        r = http_post_with_retry(
            ANILIST_API_URL,
            json_body={'query': query, 'variables': {'search': search}},
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=ANILIST_TIMEOUT,
        )
        if r is None:
            return None
        if r.status_code != 200:
            if r.status_code == 429:
                logger.warning("AniList: rate limit (429)")
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        media = (data.get('data') or {}).get('Media')
        if not media:
            return None
        title_obj = media.get('title') or {}
        return {
            'romaji': title_obj.get('romaji'),
            'english': title_obj.get('english'),
            'native': title_obj.get('native'),
        }

    def lookup(self, query: str) -> Optional[dict]:
        """Главный метод: ищет аниме/мангу по строке. Использует кеш.
        Возвращает dict с romaji/english/native или None если не найдено.

        Делает до 2 HTTP-запросов (anime + manga). Защищает от перезапросов через кеш.
        Блокирующий — не использовать в hot path; для нас это ок, потому что вызывается
        только при первом переводе уникального заголовка."""
        if not query or len(query) < 2 or len(query) > 100:
            return None

        key = self._norm_key(query)

        # Проверка кеша
        cached = self._cache.get(key)
        if cached and self._is_cache_fresh(cached):
            if cached.get('found'):
                return {
                    'romaji': cached.get('romaji'),
                    'english': cached.get('english'),
                    'native': cached.get('native'),
                }
            return None

        # Запрашиваем API
        result = self._query_api(query, manga=False)
        if not result:
            result = self._query_api(query, manga=True)

        # Сохраняем в кеш (и положительный, и отрицательный)
        if result:
            entry = {
                'found': True,
                'romaji': result.get('romaji'),
                'english': result.get('english'),
                'native': result.get('native'),
                'checked_at': datetime.now().isoformat(),
            }
            self._cache[key] = entry
            self._save()
            return result
        else:
            self._cache[key] = {
                'found': False,
                'checked_at': datetime.now().isoformat(),
            }
            self._save()
            return None


anilist: Optional['AniListClient'] = None


# ============== ПЕРЕВОД С ЗАЩИТОЙ ТЕРМИНОВ ==============
_translation_cache: dict[str, str] = {}
# Лимит кэша переводов в памяти: при переполнении выкидываем старейшую треть
# (dict в Python сохраняет порядок вставки). Без лимита за месяцы работы
# кэш растёт бесконечно и подъедает RAM.
TRANSLATION_CACHE_MAX = 4000

# Кавычки разных видов, в которых могут быть названия
_QUOTE_PATTERNS = [
    re.compile(r'«([^»\n]+)»'),
    re.compile(r'„([^"\n]+)"'),
    re.compile(r'\u201C([^\u201D\n]+)\u201D'),  # кудрявые
    re.compile(r"'([A-Z][^'\n]{1,80})'"),
    # ASCII-кавычки: только если внутри минимум 2 слова и первое с заглавной (избегаем
    # разговорных выражений типа "now" или цитат предложений)
    re.compile(r'"([A-Z][a-zA-Z\u00C0-\u017F]+(?:[\s\-][a-zA-Z\u00C0-\u017F]+){1,15})"'),
]

# Японские частицы и хоноративы — индикатор японского названия
_JP_MARKERS = (
    'no', 'na', 'ni', 'wa', 'to', 'ga', 'de', 'ka', 'mo', 'ya', 'ne',
    'kun', 'chan', 'san', 'sama', 'sensei', 'senpai', 'kohai', 'tan',
    'shin', 'shi', 'kai', 'jou', 'sho', 'kyou', 'gakuen', 'gakkou',
)

# Словообразование с дефисом: Wakao-kun, Tomo-chan
_HYPHEN_MARKERS = ('kun', 'chan', 'san', 'sama', 'sensei', 'senpai', 'tan')

# Цепочки 2+ слов с заглавной — НО только если в цепочке встречается японская частица
# Структура: <CapWord> (<space> <CapWord или частица>)+
# Главное: хотя бы одно из слов в середине должно быть частицей
_PROPER_CHAIN_JP = re.compile(
    r'\b('
    r'[A-Z][a-zA-Z\u00C0-\u017F]+(?:-[a-zA-Z\u00C0-\u017F]+)*'
    r'(?:\s+(?:[A-Z][a-zA-Z\u00C0-\u017F]+(?:-[a-zA-Z\u00C0-\u017F]+)*|'
    + '|'.join(_JP_MARKERS) + r')){1,7}'
    r')\b'
)

# Слово с японским дефисным суффиксом (Wakao-kun, Tomo-chan)
_HYPHEN_SUFFIX = re.compile(
    r'\b([A-Z][a-zA-Z\u00C0-\u017F]+-(?:' + '|'.join(_HYPHEN_MARKERS) + r'))\b'
)

# Слова целиком в верхнем регистре (3+ букв): MAPPA, ANI-MAY, ONE PIECE
# Не защищаем римские цифры (II, III, IV, XIV) — они должны идти вместе с предыдущим словом
_UPPERCASE_WORD = re.compile(r'\b([A-Z][A-Z0-9]{2,}(?:[-\s][A-Z][A-Z0-9]{2,}){0,5})\b')
_ROMAN_NUMERAL = re.compile(r'^[IVXLCDM]+$')

# "Word! Word" — Sound! Euphonium, Yuri!! On Ice
_EXCLAMATION_TITLE = re.compile(
    r'\b([A-Z][a-zA-Z\u00C0-\u017F]+[!?]+\s+[A-Z][a-zA-Z\u00C0-\u017F]+(?:\s+[A-Z][a-zA-Z\u00C0-\u017F]+)*)\b'
)

# Стоп-слова — не считаем именем, даже если с большой буквы
_STOPWORDS_EN = {
    'I', 'A', 'AN', 'THE', 'AND', 'OR', 'OF', 'IN', 'ON', 'TO', 'IS', 'BE',
    'AT', 'BY', 'FOR', 'WITH', 'AS', 'IF', 'IT', 'NO', 'NOT', 'BUT', 'ARE',
    'CD', 'DVD', 'TV', 'OVA', 'OAD', 'AI', 'CG', 'PV', 'OP', 'ED', 'BD',
    'USA', 'UK', 'EU', 'JP', 'US', 'PR', 'CEO', 'GM', 'CM',
}

# Английские стоп-слова в обычном регистре (для проверки начала цепочки)
_COMMON_FIRST = {
    'the', 'a', 'an', 'this', 'that', 'these', 'those', 'new', 'now',
    'in', 'on', 'of', 'at', 'for', 'and', 'or', 'but', 'is', 'are', 'was',
    'when', 'where', 'why', 'how', 'what', 'who', 'which', 'while',
    'it', 'its', 'my', 'your', 'his', 'her', 'their', 'our',
    'every', 'all', 'any', 'some', 'each', 'no', 'one', 'two', 'three',
    'do', 'don', 'does', 'doing', 'have', 'has', 'had', 'be', 'been',
    'use', 'using', 'used', 'check', 'want', 'here', 'there', 'now',
    'see', 'look', 'find', 'get', 'got', 'try', 'go', 'come',
    'preferred', 'prefer', 'similar', 'recommendations', 'questions',
    'additional', 'first', 'second', 'third', 'last', 'next',
}


def _make_token(idx: int) -> str:
    """Создаёт надёжный плейсхолдер. Используем символы которые Google Translate не трогает."""
    # 〖〗 — японские квадратные скобки, не транслитерируются
    return f'〖{idx}〗'


_TOKEN_PATTERN = re.compile(r'〖\s*(\d+)\s*〗')


def auto_protect_proper_nouns(text: str, start_index: int = 1000) -> tuple[str, dict]:
    """Защита имён собственных перед переводом.
    Консервативная: не трогает слова в начале предложений и общие английские слова."""
    placeholders: dict[str, str] = {}
    result = text
    counter = [start_index]

    def make_placeholder(value: str) -> str:
        ph = _make_token(counter[0])
        counter[0] += 1
        placeholders[ph] = value
        return ph

    # 1. "Sound! Euphonium"
    def replace_excl(m):
        value = m.group(1).strip()
        return make_placeholder(value)
    result = _EXCLAMATION_TITLE.sub(replace_excl, result)

    # 2. Кавычки — внутреннее содержимое имя собственное
    for pattern in _QUOTE_PATTERNS:
        def replace_quoted(m):
            inner = m.group(1).strip()
            if not inner or len(inner) > 80:
                return m.group(0)
            ph = make_placeholder(inner)
            quote_char = m.group(0)[0]
            close_char = m.group(0)[-1]
            return f'{quote_char}{ph}{close_char}'
        result = pattern.sub(replace_quoted, result)

    # 3. Слова целиком в верхнем регистре (без изменений: MAPPA, ONE PIECE)
    def replace_upper(m):
        value = m.group(1)
        if value.upper() in _STOPWORDS_EN:
            return m.group(0)
        # Не защищаем одиночные римские цифры — оставляем их в составе имени
        if _ROMAN_NUMERAL.match(value):
            return m.group(0)
        return make_placeholder(value)
    result = _UPPERCASE_WORD.sub(replace_upper, result)

    # 4. Слова с японским суффиксом (Wakao-kun, Tomo-chan)
    def replace_hyphen(m):
        return make_placeholder(m.group(1))
    result = _HYPHEN_SUFFIX.sub(replace_hyphen, result)

    # 5. Цепочки слов с заглавной — ТОЛЬКО если в цепочке есть японская частица
    # Это надёжный маркер транскрипции с японского. Без него скорее всего
    # обычная английская фраза вроде "Anime Questions Recommendations".
    def replace_chain(m):
        value = m.group(1).strip()
        words = value.split()
        # Должна быть хотя бы одна частица среди слов цепочки
        has_jp_marker = any(w.lower() in _JP_MARKERS for w in words)
        if not has_jp_marker:
            return m.group(0)
        first = words[0]
        if first.lower() in _COMMON_FIRST:
            return m.group(0)
        return make_placeholder(value)
    result = _PROPER_CHAIN_JP.sub(replace_chain, result)

    return result, placeholders


def protect_terms(text: str) -> tuple[str, dict]:
    """Заменяет защищённые термины (PROTECTED_TERMS) на плейсхолдеры."""
    placeholders = {}
    result = text
    counter = 0
    for term in PROTECTED_TERMS:
        pattern = re.compile(r'\b' + re.escape(term) + r'\b', re.IGNORECASE)
        if pattern.search(result):
            placeholder = _make_token(counter)
            placeholders[placeholder] = term
            result = pattern.sub(placeholder, result)
            counter += 1
    return result, placeholders


def restore_terms(text: str, placeholders: dict) -> str:
    """Возвращает все плейсхолдеры обратно. Использует _TOKEN_PATTERN для устойчивости
    к тому, что переводчик может вставить пробелы внутрь токена."""
    if not placeholders:
        return text
    # Используем глобальную замену по паттерну — это надёжнее цикла по словарю
    def replace_token(m):
        idx_str = m.group(1)
        # Ищем плейсхолдер с этим индексом
        for ph, value in placeholders.items():
            if ph == _make_token(int(idx_str)):
                return value
        return m.group(0)  # не нашли — оставляем как было
    result = _TOKEN_PATTERN.sub(replace_token, text)

    # Fallback: переводчик мог исковеркать скобки токена (например, DeepL без
    # XML-режима превращал 〖2000〗 в «2000»). Для каждого невосстановленного
    # плейсхолдера ищем его индекс в кавычках/скобках и возвращаем значение.
    for ph, value in placeholders.items():
        m = _TOKEN_PATTERN.fullmatch(ph)
        if not m:
            continue
        idx = m.group(1)
        if _make_token(int(idx)) in result:
            continue  # обычный токен остался — его уже обработали выше
        broken = re.compile(r'[«"„‹<\[〈]\s*' + re.escape(idx) + r'\s*[»"“›>\]〉]')
        if broken.search(result):
            result = broken.sub(value, result)
    return result


def apply_replacements(text: str) -> str:
    """Косметические замены после перевода."""
    for pattern, replacement, flags in POST_TRANSLATION_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=flags)
    return text


# Регулярка для поиска кандидатов на «возможные названия» в тексте.
# Цепочка из 2-6 слов, где минимум первое и последнее — с заглавной.
# Это шире чем _PROPER_CHAIN_JP — не требует японских частиц,
# потому что мы потом проверяем через AniList.
_ANILIST_CANDIDATE = re.compile(
    r'\b('
    r'[A-Z][a-zA-Z\u00C0-\u017F]+(?:-[a-zA-Z\u00C0-\u017F]+)*'
    r'(?:\s+(?:[a-z]{1,4}|[A-Z][a-zA-Z\u00C0-\u017F]+(?:-[a-zA-Z\u00C0-\u017F]+)*)){0,5}'
    r'(?:\s+[A-Z][a-zA-Z\u00C0-\u017F]+(?:-[a-zA-Z\u00C0-\u017F]+)*)?'
    r')\b'
)


def anilist_protect_titles(text: str, start_index: int = 2000) -> tuple[str, dict]:
    """Дополнительная защита через AniList API.
    Ищет в тексте последовательности слов с заглавной буквы, спрашивает AniList,
    защищает плейсхолдером если подтверждено что это название аниме/манги.

    Использует ROMAJI как форму возврата (Tonari no Wakao-kun).
    """
    placeholders: dict[str, str] = {}
    result = text
    counter = [start_index]
    checked: set[str] = set()  # чтобы не спрашивать одно и то же дважды в этом проходе

    def make_placeholder(value: str) -> str:
        ph = _make_token(counter[0])
        counter[0] += 1
        placeholders[ph] = value
        return ph

    # Собираем кандидатов (от длинных к коротким, чтобы длинные находились первыми)
    candidates = []
    for m in _ANILIST_CANDIDATE.finditer(text):
        candidate = m.group(1).strip()
        # Пропускаем слишком короткие (не имена) и слишком длинные (точно не названия)
        if len(candidate) < 4 or len(candidate) > 80:
            continue
        # Пропускаем если уже выглядит как плейсхолдер (или содержит его)
        if '〖' in candidate or '〗' in candidate:
            continue
        # Пропускаем если первое слово — общее английское
        first = candidate.split()[0]
        if first.lower() in _COMMON_FIRST:
            continue
        # Пропускаем если кандидат покрывает большую часть текста: это скорее
        # газетный Title-Case заголовок целиком ("PlayStation to End Physical
        # Disc Production"), а не название внутри него. Защита такого «кандидата»
        # блокирует перевод всего заголовка.
        if len(candidate) >= 0.55 * len(text.strip()):
            continue
        candidates.append((m.start(), m.end(), candidate))

    # Сортируем по длине убывающе, чтобы длинные имена защищались первыми
    candidates.sort(key=lambda x: -len(x[2]))

    for start, end, candidate in candidates:
        if candidate.lower() in checked:
            continue
        checked.add(candidate.lower())

        info = anilist.lookup(candidate)
        if info:
            # Выбираем "лучшую" форму названия:
            # - если исходный текст совпадает с какой-то формой AniList (romaji/english/native) — оставляем как есть
            # - иначе предпочитаем romaji (вариант A)
            cand_lower = candidate.lower()
            forms = [info.get('romaji'), info.get('english'), info.get('native')]
            preferred = candidate  # по умолчанию — что было в тексте
            for form in forms:
                if form and form.lower() == cand_lower:
                    preferred = form  # каноническая форма с правильным регистром
                    break
            else:
                # Не нашли точного совпадения — берём romaji (или english если romaji нет)
                preferred = info.get('romaji') or info.get('english') or candidate

            # Заменяем ВСЕ вхождения этого кандидата в результирующем тексте
            pattern = re.compile(r'\b' + re.escape(candidate) + r'\b', re.IGNORECASE)
            if pattern.search(result):
                ph = make_placeholder(preferred)
                result = pattern.sub(ph, result, count=1)
                logger.debug(f"AniList: защищено '{candidate}' → '{preferred}'")

    return result, placeholders


def _deepl_usage() -> tuple[Optional[dict], str]:
    """Запрашивает у DeepL использование лимита.
    Возвращает (данные, '') при успехе или (None, описание_ошибки) при неудаче.

    Особенности:
    - Передаём нормальный User-Agent: WAF DeepL может отдавать 403 на GET
      с дефолтным python-requests с серверных IP (перевод при этом работает).
    - Usage-эндпоинт принимает и GET, и POST — при 403 на GET пробуем POST.
    - При 403 пробуем второй endpoint (вдруг тип ключа не совпал с эвристикой ':fx')."""
    if not DEEPL_API_KEY:
        return None, 'ключ не задан'
    primary = (
        'https://api-free.deepl.com/v2/usage'
        if DEEPL_API_KEY.endswith(':fx')
        else 'https://api.deepl.com/v2/usage'
    )
    fallback = (
        'https://api.deepl.com/v2/usage'
        if 'api-free' in primary
        else 'https://api-free.deepl.com/v2/usage'
    )
    headers = {
        'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}',
        'User-Agent': USER_AGENT,
    }
    first_err = ''

    def _remember(err: str) -> None:
        nonlocal first_err
        if not first_err:
            first_err = err

    for endpoint in (primary, fallback):
        host = endpoint.split('/')[2]
        last_status = None
        for method, method_name in ((requests.get, 'GET'), (requests.post, 'POST')):
            try:
                r = method(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
            except requests.Timeout:
                _remember('таймаут соединения')
                logger.warning(f"DeepL usage: таймаут {endpoint}")
                return None, first_err
            except Exception as e:
                _remember(f'{type(e).__name__}')
                logger.warning(f"DeepL usage error: {type(e).__name__}: {e}")
                return None, first_err
            if r.status_code == 200:
                try:
                    return r.json(), ''
                except Exception:
                    _remember('невалидный ответ')
                    return None, first_err
            last_status = r.status_code
            logger.warning(f"DeepL usage: HTTP {r.status_code} от {host} ({method_name})")
            if r.status_code != 403:
                break  # не-403 повторять POST'ом бессмысленно
        _remember(f'HTTP {last_status} от {host}')
        if last_status != 403:
            break  # только при 403 есть смысл пробовать другой endpoint
    return None, first_err or 'неизвестная ошибка'


def _deepl_translate(text: str) -> Optional[str]:
    """Переводит текст на русский через DeepL API.
    Возвращает перевод или None (если ключа нет / ошибка / лимит) — тогда вызывающий
    код откатывается на Google Translate.

    Определяет endpoint по типу ключа: ':fx' → бесплатный тир, иначе Pro."""
    if not DEEPL_API_KEY:
        return None

    endpoint = (
        'https://api-free.deepl.com/v2/translate'
        if DEEPL_API_KEY.endswith(':fx')
        else 'https://api.deepl.com/v2/translate'
    )

    # КРИТИЧНО: DeepL коверкает наши плейсхолдеры 〖N〗 (превращает скобки в кавычки
    # «N»), из-за чего restore_terms не может вернуть названия — в постах появлялись
    # голые числа «2000». Официальное решение DeepL — XML-теги с ignore_tags:
    # содержимое <x>N</x> DeepL гарантированно не трогает.
    # Сырые &, <, > ломают XML-парсер DeepL (tag_handling=xml) — вывод
    # усекается до обрывков вроде «Netflix.». Экранируем до, возвращаем после.
    safe_in = (text.replace('&', '&amp;')
                   .replace('<', '&lt;')
                   .replace('>', '&gt;'))
    text_xml = re.sub(r'〖\s*(\d+)\s*〗', r'<x>\1</x>', safe_in)

    # 2 попытки на временные ошибки
    for attempt in range(2):
        try:
            r = requests.post(
                endpoint,
                data={
                    'text': text_xml,
                    'target_lang': 'RU',
                    'tag_handling': 'xml',
                    'ignore_tags': 'x',
                    # source_lang не указываем — DeepL определит сам
                },
                headers={'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}'},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                _count_deepl_chars(text)       # расход считаем по исходному тексту
                data = r.json()
                translations = data.get('translations') or []
                if translations:
                    out = translations[0].get('text') or None
                    if out:
                        # Возвращаем XML-теги обратно в наш формат плейсхолдеров
                        out = re.sub(r'<\s*x\s*>\s*(\d+)\s*<\s*/\s*x\s*>', r'〖\1〗', out)
                        # Разэкранируем entity (порядок важен: &amp; — последним)
                        out = (out.replace('&lt;', '<')
                                  .replace('&gt;', '>')
                                  .replace('&amp;', '&'))
                    return out
                return None
            elif r.status_code == 456:
                logger.warning("DeepL: исчерпан месячный лимит символов — откат на Google")
                return None
            elif r.status_code == 403:
                logger.warning("DeepL: неверный ключ (403) — откат на Google")
                return None
            elif r.status_code == 429 or r.status_code >= 500:
                # временная ошибка — повторим
                logger.debug(f"DeepL: временная ошибка {r.status_code}, попытка {attempt + 1}")
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None
            else:
                logger.debug(f"DeepL: HTTP {r.status_code}")
                return None
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.debug(f"DeepL сетевая ошибка ({type(e).__name__}), попытка {attempt + 1}")
            if attempt == 0:
                time.sleep(1)
                continue
            return None
        except Exception as e:
            logger.debug(f"DeepL error: {e}")
            return None
    return None


def translate_text(text: str, input_limit: int = TRANSLATION_INPUT_LIMIT) -> str:
    """Переводит на русский с защитой терминов и пост-обработкой.
    input_limit — сколько символов исходного текста максимум переводить
    (для режима ветки передаём больший лимит, чтобы текст не обрезался).

    Переводчик: DeepL (если задан DEEPL_API_KEY), иначе/при ошибке — Google Translate."""
    if not text:
        return text
    text = text[:input_limit]

    if text in _translation_cache:
        return _translation_cache[text]

    # 1. Защита явных терминов из словаря
    protected_text, term_placeholders = protect_terms(text)

    # 2. Авто-защита по регуляркам (кавычки, японские частицы, верхний регистр)
    protected_text, auto_placeholders = auto_protect_proper_nouns(protected_text, start_index=1000)

    # 3. Дополнительная защита через AniList API (только то, что не покрыто авто-защитой)
    protected_text, anilist_placeholders = anilist_protect_titles(protected_text, start_index=2000)

    # Объединяем словари плейсхолдеров
    all_placeholders = {**term_placeholders, **auto_placeholders, **anilist_placeholders}

    # 4. Перевод. Движок выбирается настройкой translator_engine:
    #    'deepl'  — DeepL (если ключ задан), при ошибке fallback на Google
    #    'google' — принудительно Google Translate
    # getattr с default — на случай если settings ещё не инициализирован (тесты, импорт).
    engine = getattr(settings, 'translator_engine', 'deepl')
    translated = None
    if engine != 'google':
        translated = _deepl_translate(protected_text)
    if translated is None:
        try:
            translated = translator.translate(protected_text)
        except Exception as e:
            logger.warning(f"Ошибка перевода: {e}")
            return text

    if not translated:
        return text

    # 5. Возвращаем плейсхолдеры
    translated = restore_terms(translated, all_placeholders)

    # 6. Косметические замены
    translated = apply_replacements(translated)

    # 7. Финальная очистка
    translated = re.sub(r'\s+', ' ', translated).strip()

    if len(_translation_cache) >= TRANSLATION_CACHE_MAX:
        for old_key in list(_translation_cache.keys())[:TRANSLATION_CACHE_MAX // 3]:
            del _translation_cache[old_key]
    _translation_cache[text] = translated
    return translated


# ============== ПОЛУЧЕНИЕ КАРТИНКИ ==============
def upgrade_image_url(url: str) -> str:
    """Пытается превратить URL уменьшенной картинки в URL оригинала.
    Знает популярные паттерны CDN: WordPress, MyAnimeList, Reddit и др."""
    if not url:
        return url
    original = url

    # WordPress: image-150x150.jpg → image.jpg
    # Покрывает Honey's Anime, Anime Corner и большинство WP-сайтов
    url = re.sub(
        r'-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp|gif))(?=$|\?)',
        r'\1', url, flags=re.IGNORECASE,
    )

    # MyAnimeList: cdn.myanimelist.net/r/100x140/images/... → cdn.myanimelist.net/images/...
    url = re.sub(
        r'(myanimelist\.net|kitsu\.io|anilist\.co|cdn\.myanimelist\.net)/r/\d+x\d+/',
        r'\1/', url, flags=re.IGNORECASE,
    )

    # Reddit preview: external-preview.redd.it/...?width=320 → убираем width
    if 'redd.it' in url or 'redditmedia' in url:
        url = re.sub(r'[?&](width|height)=\d+', '', url)
        url = re.sub(r'[?&]auto=webp', '', url)
        # Cleanup — & в начале query
        url = re.sub(r'\?&', '?', url).rstrip('?&')

    # Yahoo / Tumblr: _250.jpg → _1280.jpg (запросим макс размер)
    url = re.sub(r'_\d{2,3}(\.(?:jpe?g|png|webp))(?=$|\?)', r'_1280\1', url, flags=re.IGNORECASE)

    # Generic: /thumb/ или /thumbs/ в пути → /
    url = re.sub(r'/(?:thumb|thumbs|thumbnail|thumbnails)/', '/', url, flags=re.IGNORECASE)

    # Generic: ?size=small / ?w=300 — убираем
    url = re.sub(r'[?&](size|s|sz)=(?:small|thumb|thumbnail|tiny|sm)', '', url, flags=re.IGNORECASE)

    # Cleanup
    url = re.sub(r'\?&+', '?', url).rstrip('?&')

    if url != original:
        logger.debug(f"Upgraded image URL: {original[:80]}... -> {url[:80]}...")
    return url


ARTICLE_CACHE_MAX = 200         # сколько разобранных статей держим в памяти
ARTICLE_MIN_WORDS = 25          # ниже этого RSS-описание считаем бедным
ARTICLE_MAX_CHARS = 2500        # столько текста статьи отдаём модели
_article_cache: dict = {}

# Мусор, который на новостных сайтах лежит вперемешку с текстом
_ARTICLE_JUNK = re.compile(
    r'(?:^|\s)(?:advertisement|sponsored|read more|related:|share this|'
    r'subscribe|newsletter|follow us|click here|источник:|читайте также)',
    re.IGNORECASE)


# Слова, по которым видно: у новости почти наверняка есть ролик.
# Ради них стоит заглянуть в статью, если в ленте видео не оказалось.
_VIDEO_HINT_RE = re.compile(
    r'(?:trailer|teaser|promo video|\bpv\b|opening|ending|first look|'
    r'трейлер|тизер|промо|опенинг|эндинг|ролик|видео)', re.IGNORECASE)


def _probably_has_video(news: dict) -> bool:
    """Похоже ли, что новость про ролик (трейлер, опенинг, тизер)."""
    text = f"{news.get('title', '')} {news.get('summary', '')[:200]}"
    return bool(_VIDEO_HINT_RE.search(text))


def _find_video_in_html(html_text: str) -> Optional[str]:
    """Ищет ролик на странице статьи: og:video, встроенный плеер, ссылки.

    В RSS обычно лежит обрезанный тизер без плеера — трейлер живёт в самой
    статье. Раньше мы туда не заглядывали, и новости про трейлеры выходили
    без видео."""
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
    except Exception:
        return None

    # 1. Мета-теги: самый надёжный признак
    for prop in ('og:video:url', 'og:video:secure_url', 'og:video',
                 'twitter:player:stream'):
        meta = (soup.select_one(f'meta[property="{prop}"]')
                or soup.select_one(f'meta[name="{prop}"]'))
        content = (meta.get('content') or '').strip() if meta else ''
        if content.startswith('http') and (_is_video_host(content)
                                           or _is_direct_video(content)):
            return content

    # 2. Встроенный плеер
    for frame in soup.select('iframe[src], embed[src]'):
        url = (frame.get('src') or '').strip()
        if url.startswith('//'):
            url = 'https:' + url
        if url.startswith('http') and _is_video_host(url):
            return url

    # 3. Тег video
    tag = soup.select_one('video[src]') or soup.select_one('video source[src]')
    if tag and (tag.get('src') or '').startswith('http'):
        return tag['src'].strip()

    # 4. Ссылка на видеохостинг в тексте статьи
    for link in soup.select('a[href]'):
        url = (link.get('href') or '').strip()
        if url.startswith('http') and _is_video_host(url):
            return url
    return None


def _looks_thin(text: str) -> bool:
    """Хватает ли описания из ленты, чтобы написать содержательный пост."""
    return len((text or '').split()) < ARTICLE_MIN_WORDS


def _extract_article_text(html_text: str) -> str:
    """Достаёт основной текст статьи из HTML.

    Без тяжёлых библиотек: выбрасываем служебные блоки, находим контейнер
    с наибольшим объёмом связного текста и собираем из него абзацы."""
    soup = BeautifulSoup(html_text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside',
                     'form', 'noscript', 'iframe', 'figure']):
        tag.decompose()

    # Кандидаты в порядке убывания надёжности
    containers = []
    for selector in ('article', '[itemprop="articleBody"]', '.article-content',
                     '.entry-content', '.post-content', '.article-body', 'main'):
        containers.extend(soup.select(selector))
    if not containers:
        containers = soup.find_all('div')

    best_text, best_score = '', 0
    for node in containers[:60]:            # не перебираем весь документ
        paragraphs = [p.get_text(' ', strip=True) for p in node.find_all('p')]
        paragraphs = [p for p in paragraphs
                      if len(p) > 60 and not _ARTICLE_JUNK.search(p)]
        if not paragraphs:
            continue
        text = ' '.join(paragraphs)
        # Оцениваем по объёму текста, а не по числу тегов: так навигационные
        # блоки со ссылками проигрывают настоящему тексту статьи
        score = len(text)
        if score > best_score:
            best_text, best_score = text, score

    text = re.sub(r'\s+', ' ', best_text).strip()
    return text[:ARTICLE_MAX_CHARS]


def fetch_article(url: str) -> dict:
    """Читает статью по ссылке: основной текст и ролик, если он там есть.

    Один запрос закрывает две дыры сразу. В RSS лежит обрезанный тизер в 8-10
    слов — модели не из чего собрать пост; и там же нет плеера, из-за чего
    новости про трейлеры выходили без видео."""
    empty = {'text': '', 'video': None}
    if not url or not url.startswith(('http://', 'https://')):
        return empty
    if url in _article_cache:
        return _article_cache[url]
    try:
        r = http_get_with_retry(url, headers={'User-Agent': USER_AGENT},
                                timeout=HTTP_TIMEOUT)
    except Exception as e:
        logger.debug(f"статья не прочиталась ({type(e).__name__}): {url[:70]}")
        return empty
    if not r or r.status_code != 200:
        return empty
    ctype = (r.headers.get('Content-Type') or '').lower()
    if 'html' not in ctype and ctype:
        return empty
    try:
        text = _extract_article_text(r.text)
    except Exception as e:
        logger.debug(f"статья не разобралась ({type(e).__name__}): {url[:70]}")
        text = ''
    try:
        video = _find_video_in_html(r.text)
    except Exception:
        video = None
    result = {'text': text, 'video': video}
    if len(_article_cache) > ARTICLE_CACHE_MAX:
        _article_cache.clear()
    _article_cache[url] = result
    if text or video:
        logger.info(f"📄 Статья: {len(text.split())} слов"
                    + (f", найден ролик {video[:50]}" if video else ", ролика нет")
                    + f" — {url[:55]}")
    return result


def fetch_article_text(url: str) -> str:
    """Только текст статьи (обёртка над fetch_article)."""
    return fetch_article(url).get('text', '')


def fetch_og_image(url: str) -> Optional[str]:
    try:
        r = http_get_with_retry(
            url,
            headers={'User-Agent': USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if not r or r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        og = soup.find('meta', property='og:image')
        if og and og.get('content'):
            return og['content']
        tw = soup.find('meta', attrs={'name': 'twitter:image'})
        if tw and tw.get('content'):
            return tw['content']
        img = soup.find('img', src=True)
        if img:
            return img['src']
    except Exception as e:
        logger.debug(f"og:image fail для {url}: {e}")
    return None


# Если RSS-превью длиннее этого — на страницу не лезем, текста уже достаточно
ARTICLE_FETCH_THRESHOLD = 400
# Сколько максимум символов берём из полной статьи
ARTICLE_MAX_CHARS = 3500
# Кеш полных текстов (по URL) в памяти на время работы
_article_cache: dict[str, str] = {}

# Селекторы мусора, который надо выкинуть из текста статьи
_ARTICLE_JUNK_SELECTORS = [
    'script', 'style', 'nav', 'header', 'footer', 'aside', 'form',
    'figure', 'figcaption', 'noscript', 'iframe',
    '.share', '.social', '.related', '.advertisement', '.ad',
    '.newsletter', '.comments', '.author-bio', '.tags', '.breadcrumb',
]


def fetch_full_article_text(url: str) -> Optional[str]:
    """Заходит на страницу новости и пытается вытащить полный текст статьи.
    Возвращает текст (несколько абзацев) или None если не удалось.

    Эвристика: ищем <article> или контейнер с наибольшей плотностью <p>,
    выкидываем мусор (меню, реклама, подписи). Если не нашли — берём og:description."""
    if not url:
        return None
    if url in _article_cache:
        return _article_cache[url] or None

    try:
        r = http_get_with_retry(url, headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT)
        if not r or r.status_code != 200:
            _article_cache[url] = ''
            return None

        soup = BeautifulSoup(r.text, 'html.parser')

        # Удаляем явный мусор
        for selector in _ARTICLE_JUNK_SELECTORS:
            for tag in soup.select(selector):
                tag.decompose()

        # Стратегия 1: тег <article>
        container = soup.find('article')

        # Стратегия 2: контейнер с наибольшим числом <p> (если article не нашёлся)
        if not container:
            candidates = soup.find_all(['div', 'section', 'main'])
            best = None
            best_p_count = 0
            for cand in candidates:
                p_count = len(cand.find_all('p', recursive=False)) + len(cand.find_all('p'))
                if p_count > best_p_count:
                    best_p_count = p_count
                    best = cand
            if best and best_p_count >= 2:
                container = best

        text = ''
        if container:
            paragraphs = container.find_all('p')
            parts = []
            for p in paragraphs:
                t = p.get_text(strip=True)
                # Пропускаем мусорные короткие абзацы (копирайт, "Source:", и т.п.)
                if len(t) < 25:
                    continue
                low = t.lower()
                if low.startswith(('source:', 'via:', 'image:', 'photo:', 'credit', '©')):
                    continue
                parts.append(t)
            text = ' '.join(parts)

        # Стратегия 3: og:description как fallback
        if len(text) < ARTICLE_FETCH_THRESHOLD:
            og_desc = soup.find('meta', property='og:description')
            if og_desc and og_desc.get('content'):
                desc = og_desc['content'].strip()
                if len(desc) > len(text):
                    text = desc

        text = re.sub(r'\s+', ' ', text).strip()
        text = text[:ARTICLE_MAX_CHARS]

        _article_cache[url] = text
        return text or None
    except Exception as e:
        logger.debug(f"full article fail для {url}: {e}")
        _article_cache[url] = ''
        return None


def enrich_summary_from_page(news: dict) -> None:
    """Если RSS-превью короткое/обрезанное — догружает полный текст со страницы.
    Изменяет news['summary'] на месте."""
    summary = news.get('summary') or ''
    link = news.get('link')
    if not link:
        return
    # Если в RSS уже достаточно текста — не лезем
    if len(summary) >= ARTICLE_FETCH_THRESHOLD:
        return
    full = fetch_full_article_text(link)
    if full and len(full) > len(summary):
        news['summary'] = full
        logger.debug(f"Текст догружен со страницы: {len(summary)} → {len(full)} символов")


def extract_image_from_entry(entry, summary_html: Optional[str] = None) -> Optional[str]:
    """Возвращает первую найденную картинку (для совместимости)."""
    images = extract_all_images_from_entry(entry, summary_html)
    return images[0] if images else None


def _download_image_bytes(url: str) -> Optional[bytes]:
    """Скачивает картинку сами (для случаев, когда Bot API не может забрать её
    по URL — например, cdn-telegram.org из t.me/s/-постов). До 9 МБ."""
    try:
        r = http_get_with_retry(url, headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT)
        if not r or r.status_code != 200:
            return None
        ctype = (r.headers.get('Content-Type') or '').lower()
        if not ctype.startswith('image/'):
            return None
        data = r.content
        if not data or len(data) > 9 * 1024 * 1024:
            return None
        return data
    except Exception as e:
        logger.debug(f"download image fail {url[:80]}: {e}")
        return None


# Размерные query-параметры: вся разница вариантов картинки часто только в них
_IMG_SIZE_QUERY_KEYS = {'w', 'h', 'width', 'height', 'size', 'resize', 'fit',
                        'quality', 'q', 'dpr', 'crop', 'auto', 'fm', 'zoom'}


def _image_variant_key(url: str) -> str:
    """Ключ группировки: варианты ОДНОЙ картинки в разных размерах дают один ключ.
    Срезает размерные суффиксы имени файла (-1280x720, _large, @2x)
    и размерные query-параметры (?width=640)."""
    try:
        p = urlparse(url)
    except Exception:
        return url
    path = p.path.lower()
    path = re.sub(r'[-_]\d{2,4}x\d{2,4}(?=\.\w{2,5}$)', '', path)      # -1280x720.jpg
    path = re.sub(r'[-_]\d{2,4}w(?=\.\w{2,5}$)', '', path)              # _640w.jpg
    path = re.sub(r'@\dx(?=\.\w{2,5}$)', '', path)                      # @2x.jpg
    path = re.sub(
        r'[-_](?:large|medium|small|thumb(?:nail)?|full|scaled|mini|big|orig(?:inal)?|wide)'
        r'(?=\.\w{2,5}$)', '', path)                                    # _thumb.jpg / _full.jpg
    # Query без размерных ключей
    kept = [kv for kv in p.query.split('&')
            if kv and kv.split('=', 1)[0].lower() not in _IMG_SIZE_QUERY_KEYS]
    return f'{p.netloc.lower()}{path}?{"&".join(sorted(kept))}'


def _image_size_score(url: str) -> int:
    """Оценка «крупности» варианта по URL: больше — лучше."""
    score = 0
    low = url.lower()
    for m in re.finditer(r'(\d{2,4})x(\d{2,4})', low):
        score = max(score, int(m.group(1)) * int(m.group(2)))
    for m in re.finditer(r'[?&](?:w|width)=(\d{2,4})', low):
        score = max(score, int(m.group(1)) * 720)
    if re.search(r'[-_](?:full|orig(?:inal)?|large|big)\b|[-_](?:full|orig(?:inal)?|large|big)\.', low):
        score += 10_000_000
    if re.search(r'[-_](?:thumb(?:nail)?|mini|small)\b|[-_](?:thumb(?:nail)?|mini|small)\.', low):
        score -= 10_000_000
    return score


def _dedup_image_variants(urls: list[str]) -> list[str]:
    """Схлопывает размерные варианты одной картинки, оставляя лучший (крупнейший).
    Источники (Crunchyroll и др.) отдают одну картинку в 3-5 размерах с разными URL —
    без этого в пост уходят 5 одинаковых фото убывающего качества.
    Порядок групп — по первому появлению."""
    if len(urls) <= 1:
        return urls
    order: list[str] = []                       # ключи в порядке появления
    best: dict[str, str] = {}                   # ключ → лучший URL
    best_score: dict[str, int] = {}
    for u in urls:
        key = _image_variant_key(u)
        s = _image_size_score(u)
        if key not in best:
            order.append(key)
            best[key], best_score[key] = u, s
        elif s > best_score[key]:
            best[key], best_score[key] = u, s
    return [best[k] for k in order]


def _normalize_image_url(url: str, base_url: Optional[str] = None) -> Optional[str]:
    """Приводит URL картинки к абсолютному виду и проверяет валидность.
    Возвращает нормализованный URL или None если URL битый/невалидный."""
    if not url:
        return None
    url = url.strip()
    # Протокол-относительный: //example.com/pic.jpg → https://example.com/pic.jpg
    if url.startswith('//'):
        url = 'https:' + url
    # Относительный путь (/images/pic.jpg или images/pic.jpg) → добавляем домен из base_url
    if base_url and not url.startswith(('http://', 'https://')):
        from urllib.parse import urljoin
        url = urljoin(base_url, url)
    # Проверяем что получился валидный абсолютный URL с хостом
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ('http', 'https'):
        return None
    if not parsed.netloc:  # пустой хост — битый URL (та самая ошибка "url host is empty")
        return None
    return url


def extract_all_images_from_entry(entry, summary_html: Optional[str] = None,
                                  base_url: Optional[str] = None) -> list[str]:
    """Собирает все картинки из RSS-записи и HTML-описания, с дедупликацией.
    Применяет upgrade_image_url для замены thumbnail на полное разрешение.
    base_url (ссылка на статью) нужен чтобы превращать относительные URL в абсолютные."""
    seen: set[str] = set()
    images: list[str] = []

    def add(url: Optional[str]) -> None:
        if not url:
            return
        url = html.unescape(url)
        # Игнорируем иконки/спиннеры (мелкие декоративные)
        if re.search(r'/(?:icon|avatar|favicon|emoji|spinner)[/_-]', url, re.IGNORECASE):
            return
        # Нормализуем: относительный → абсолютный, проверяем валидность
        normalized = _normalize_image_url(url, base_url)
        if not normalized:
            return
        url = normalized
        # Пытаемся получить полноразмерную версию
        url = upgrade_image_url(url)
        if url in seen:
            return
        seen.add(url)
        images.append(url)

    # 1. media_content (обычно полное разрешение)
    for media in (getattr(entry, 'media_content', None) or []):
        if 'image' in media.get('type', '') or media.get('medium') == 'image':
            add(media.get('url'))

    # 2. enclosures (тоже часто полные)
    for enc in (getattr(entry, 'enclosures', None) or []):
        if 'image' in enc.get('type', ''):
            add(enc.get('href'))

    # 3. <img> в HTML-описании
    if summary_html:
        for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)', summary_html):
            add(match.group(1))

    # 4. media_thumbnail — последним, потому что обычно мелкое
    for thumb in (getattr(entry, 'media_thumbnail', None) or []):
        add(thumb.get('url'))

    # Схлопываем размерные варианты одной картинки (оставляем лучший)
    images = _dedup_image_variants(images)
    return images[:MAX_PHOTOS_PER_POST]


# ============== ВИДЕО ==============
def _is_video_host(url: str) -> bool:
    """Проверяет, является ли URL видеохостингом, который умеет yt-dlp."""
    try:
        host = urlparse(url).netloc.lower().lstrip('www.')
    except Exception:
        return False
    return any(vh in host for vh in VIDEO_HOSTS)


def _is_direct_video(url: str) -> bool:
    """Проверяет, что URL — прямая ссылка на видеофайл."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        host = parsed.netloc.lower()
    except Exception:
        return False
    # Видео из t.me/s/ живут на cdn-telegram/telesco без расширения в пути
    if 'cdn-telegram.org' in host or 'telesco.pe' in host:
        return True
    return path.endswith(DIRECT_VIDEO_EXTENSIONS)


def extract_video_url(entry, summary_html: Optional[str] = None) -> Optional[str]:
    """Ищет видео в RSS-записи: enclosures, media:content, iframe, ссылки на YouTube/Twitter/etc."""
    # 1. enclosures с типом video/*
    enclosures = getattr(entry, 'enclosures', None) or []
    for enc in enclosures:
        enc_type = enc.get('type', '')
        href = enc.get('href', '')
        if 'video' in enc_type and href:
            return html.unescape(href)
        if href and _is_direct_video(href):
            return html.unescape(href)

    # 2. media:content с типом video
    media_content = getattr(entry, 'media_content', None) or []
    for media in media_content:
        if 'video' in media.get('type', ''):
            url = media.get('url')
            if url:
                return html.unescape(url)

    # 3. Поиск в HTML описания
    if summary_html:
        # iframe (YouTube/Vimeo embed)
        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)', summary_html, re.IGNORECASE)
        if iframe_match:
            url = html.unescape(iframe_match.group(1))
            if _is_video_host(url):
                return url

        # <video src="...">
        video_tag = re.search(r'<video[^>]+src=["\']([^"\']+)', summary_html, re.IGNORECASE)
        if video_tag:
            return html.unescape(video_tag.group(1))

        # Прямая ссылка <a href="...youtube.../watch?v=...">
        for link_match in re.finditer(r'href=["\']([^"\']+)', summary_html):
            url = html.unescape(link_match.group(1))
            if _is_video_host(url) or _is_direct_video(url):
                return url
    return None


def download_video(url: str, note: Optional[list] = None) -> Optional[Path]:
    """Скачивает видео через yt-dlp с лимитами по длине и размеру.
    Возвращает путь к файлу или None.

    В note (если передан) кладём причину провала: без неё «видео не пришло»
    выглядит одинаково и когда yt-dlp не установлен, и когда ролик слишком
    большой, и когда хостинг закрыл доступ.
    Эту функцию нужно вызывать через asyncio.to_thread, она блокирующая."""
    def say(reason: str):
        if note is not None:
            note.append(reason)
        return None

    if not YT_DLP_AVAILABLE:
        logger.warning("yt-dlp не установлен — видео с внешних хостингов недоступны")
        return say('yt-dlp не установлен')

    # Уникальное имя файла на основе URL, чтобы не было коллизий
    safe_name = re.sub(r'[^\w\-]', '_', url)[-80:]
    output_template = str(VIDEO_DOWNLOAD_DIR / f'{safe_name}.%(ext)s')

    ydl_opts = {
        'format': VIDEO_FORMAT,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'max_filesize': VIDEO_MAX_FILE_SIZE_MB * 1024 * 1024,
        'socket_timeout': 30,
        'retries': 2,
        'fragment_retries': 2,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Сначала extract_info без скачивания — проверяем длину
            info = ydl.extract_info(url, download=False)

            duration = info.get('duration', 0)
            if VIDEO_MAX_DURATION_SEC > 0 and duration and duration > VIDEO_MAX_DURATION_SEC:
                logger.info(f"Видео слишком длинное ({duration}с): {url[:60]}")
                return say(f'ролик длиннее лимита ({duration}с)')

            # Скачиваем
            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))

            if not file_path.exists():
                # yt-dlp иногда меняет расширение после конвертации
                stem = file_path.stem
                for candidate in VIDEO_DOWNLOAD_DIR.glob(f'{stem}.*'):
                    if candidate.suffix.lower() in DIRECT_VIDEO_EXTENSIONS:
                        file_path = candidate
                        break

            if not file_path.exists():
                logger.warning(f"yt-dlp скачал, но файл не найден: {file_path}")
                return say('файл после скачивания не найден '
                           '(возможно, нужна склейка дорожек и ffmpeg)')

            size_mb = file_path.stat().st_size / (1024 * 1024)
            if size_mb > VIDEO_MAX_FILE_SIZE_MB:
                logger.info(f"Видео слишком большое ({size_mb:.1f} МБ): {url[:60]}")
                file_path.unlink(missing_ok=True)
                return say(f'файл {size_mb:.0f} МБ больше лимита '
                           f'{VIDEO_MAX_FILE_SIZE_MB} МБ')

            logger.info(f"🎬 Скачано видео: {file_path.name} ({size_mb:.1f} МБ)")
            if note is not None:
                note.append(f'скачано через yt-dlp, {size_mb:.1f} МБ')
            return file_path
    except Exception as e:
        text = str(e)[:120]
        logger.warning(f"Видео не скачалось ({type(e).__name__}): {text}")
        return say(f'{type(e).__name__}: {text}')


def cleanup_video_dir(max_age_hours: int = 1) -> None:
    """Чистит старые временные видеофайлы."""
    if not VIDEO_DOWNLOAD_DIR.exists():
        return
    now = datetime.now().timestamp()
    for f in VIDEO_DOWNLOAD_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > max_age_hours * 3600:
                f.unlink(missing_ok=True)
        except OSError:
            pass



_THUMB_MARKERS = re.compile(
    r'(thumb|small|tiny|/(?:32|48|64|75|100|120|128|140|150|160|180|200)/|'
    r'_(?:32|48|64|75|100|120|128|140|150|160|180|200)x|'
    r'-(?:32|48|64|75|100|120|128|140|150|160|180|200)x|'
    r'width=(?:[1-9]?\d{1,2}|[12]\d{2})\b)',
    re.IGNORECASE,
)


def _looks_like_thumbnail(url: str) -> bool:
    """Эвристика: похож ли URL на уменьшенную версию."""
    if not url:
        return False
    return bool(_THUMB_MARKERS.search(url))


def _is_too_old(published_struct, max_age_hours: Optional[int] = None) -> bool:
    """Проверяет, старее ли пост чем max_age_hours.
    Если дата публикации неизвестна — возвращает False (пропускаем как свежий)."""
    if not published_struct:
        return False
    if max_age_hours is None:
        max_age_hours = settings.post_max_age_hours
    try:
        # published_parsed это struct_time в UTC
        pub_dt = datetime(*published_struct[:6])
    except (TypeError, ValueError, OverflowError):
        return False
    # utcnow() объявлен устаревшим в 3.12 — берём aware-время и снимаем tz
    age = datetime.now(timezone.utc).replace(tzinfo=None) - pub_dt
    return age > timedelta(hours=max_age_hours)


def _parse_rss_with_fallback(
    rss_url: str,
    source_name: str,
    fetch_og: bool = True,
    force_og: bool = False,
) -> list[dict]:
    """Парсит RSS-ленту.
    - fetch_og: если в RSS нет картинки или она похожа на thumbnail, идём за og:image
    - force_og: для лент, у которых RSS вообще не отдаёт нормальных картинок —
      всегда лезем за og:image (медленнее, но качественнее)
    """
    news_list = []
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:NEWS_PER_SOURCE * 3]:
            link = getattr(entry, 'link', None)
            if not link or link in sent_links:
                continue
            published_parsed = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
            if _is_too_old(published_parsed):
                continue
            summary_html = entry.get('summary', '')
            images = extract_all_images_from_entry(entry, summary_html, base_url=link)
            # Решаем нужно ли лезть за og:image
            need_og = fetch_og and (
                force_og  # для известно-проблемных лент
                or not images
                or _looks_like_thumbnail(images[0])
            )
            if need_og:
                og = fetch_og_image(link)
                if og:
                    og = upgrade_image_url(og)
                    if og not in images:
                        images.insert(0, og)
                        images = images[:MAX_PHOTOS_PER_POST]
            video_url = extract_video_url(entry, summary_html)
            news_list.append({
                'title': entry.title,
                'link': link,
                'summary': clean_html(summary_html),
                'source': source_name,
                'image': images[0] if images else None,
                'images': images,
                'video': video_url,
                'published_parsed': published_parsed,
            })
            if len(news_list) >= NEWS_PER_SOURCE:
                break
    except Exception as e:
        logger.error(f"{source_name} error: {e}")
    return news_list


def get_animenewsnetwork():
    return _parse_rss_with_fallback(
        'https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us',
        'AnimeNewsNetwork',
        force_og=True,
    )


def get_ann_newsroom():
    return _parse_rss_with_fallback(
        'https://www.animenewsnetwork.com/newsroom/rss.xml?ann-edition=us',
        'ANN Newsroom',
        force_og=True,
    )


def get_crunchyroll_news():
    return _parse_rss_with_fallback('https://www.crunchyroll.com/rss/news', 'Crunchyroll')


def get_honeys_anime():
    return _parse_rss_with_fallback('https://honeysanime.com/feed/', "Honey's Anime")


def get_anime_corner():
    return _parse_rss_with_fallback('https://animecorner.me/feed/', 'Anime Corner', force_og=True)


# === Дополнительные источники ===
def get_ann_anime_review():
    """ANN Anime Reviews — отдельная лента обзоров (с картинками)."""
    return _parse_rss_with_fallback(
        'https://www.animenewsnetwork.com/reviews/rss.xml?ann-edition=us',
        'ANN Reviews',
    )


def get_otaquest():
    """OtaQuest — большой англоязычный сайт о манге, аниме и японской культуре."""
    return _parse_rss_with_fallback('https://www.otaquest.com/feed/', 'OtaQuest')


def get_animehunch():
    """AnimeHunch — обзоры и новости индустрии."""
    return _parse_rss_with_fallback('https://animehunch.com/feed/', 'AnimeHunch')


def get_otakukart():
    """OtakuKart — крупный новостной портал, есть отдельная аниме-категория."""
    return _parse_rss_with_fallback('https://otakukart.com/news/anime/feed/', 'OtakuKart')


def get_animeherald():
    """Anime Herald — анимаджурналистика."""
    return _parse_rss_with_fallback('https://www.animeherald.com/feed/', 'Anime Herald', force_og=True)


def get_animefeminist():
    """Anime Feminist — глубокий анализ и обзоры."""
    return _parse_rss_with_fallback('https://www.animefeminist.com/feed/', 'Anime Feminist')


def get_comicbook_anime():
    """ComicBook.com — раздел про аниме."""
    return _parse_rss_with_fallback('https://comicbook.com/category/anime/feed/', 'ComicBook Anime')


def get_screenrant_anime():
    """ScreenRant — раздел про аниме (только на запад)."""
    return _parse_rss_with_fallback('https://screenrant.com/feed/category/anime-news/', 'ScreenRant Anime')


def get_ann_industry():
    """ANN Industry News — индустрия (лицензии, дистрибьюторы, компании)."""
    return _parse_rss_with_fallback(
        'https://www.animenewsnetwork.com/news/rss.xml?ann-edition=us',
        'ANN Industry',
        force_og=True,
    )


def get_cbr_anime():
    """CBR (Comic Book Resources) — раздел аниме/манги."""
    return _parse_rss_with_fallback('https://www.cbr.com/feed/category/anime-news/', 'CBR Anime')


def get_polygon_anime():
    """Polygon — раздел аниме."""
    return _parse_rss_with_fallback('https://www.polygon.com/rss/group/anime/index.xml', 'Polygon')


def get_kotaku_anime():
    """Kotaku — раздел аниме."""
    return _parse_rss_with_fallback('https://kotaku.com/tag/anime/rss', 'Kotaku')


def get_gamerant_anime():
    """GameRant — раздел аниме."""
    return _parse_rss_with_fallback('https://gamerant.com/feed/category/anime/', 'GameRant Anime')


def get_manga_tokyo():
    """Manga Tokyo — англоязычный сайт о манге/аниме."""
    return _parse_rss_with_fallback('https://manga.tokyo/feed/', 'Manga Tokyo')


def get_yatta_tachi():
    """Yatta-Tachi — обзоры и колонки про аниме/мангу."""
    return _parse_rss_with_fallback('https://yattatachi.com/feed', 'Yatta-Tachi')


def get_manga_mavericks():
    """Manga Mavericks — обзоры манги."""
    return _parse_rss_with_fallback('https://mangamavericks.com/feed/', 'Manga Mavericks')


def get_animation_magazine():
    """Animation Magazine — раздел аниме."""
    return _parse_rss_with_fallback('https://www.animationmagazine.net/category/anime/feed/', 'Animation Magazine')


def get_anitrendz():
    """AniTrendz — еженедельные опросы, тренды, чарты."""
    return _parse_rss_with_fallback('https://www.anitrendz.com/feed/', 'AniTrendz')


def get_myanimelist():
    news_list = []
    try:
        response = http_get_with_retry(
            'https://myanimelist.net/news',
            headers={'User-Agent': USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if not response or response.status_code != 200:
            return news_list
        soup = BeautifulSoup(response.text, 'html.parser')
        for item in soup.select('div.news-unit')[:NEWS_PER_SOURCE]:
            title_tag = item.select_one('p.title a')
            if not title_tag:
                continue
            link = title_tag['href']
            if not link.startswith('http'):
                link = 'https://myanimelist.net' + link
            if link in sent_links:
                continue
            # Собираем все картинки в карточке
            images: list[str] = []
            seen_imgs: set[str] = set()
            for img_tag in item.select('img[src]'):
                src = img_tag.get('src')
                if not src:
                    continue
                src = upgrade_image_url(src)
                if src not in seen_imgs:
                    seen_imgs.add(src)
                    images.append(src)
                if len(images) >= MAX_PHOTOS_PER_POST:
                    break
            summary_tag = item.select_one('div.text')
            summary = summary_tag.get_text(strip=True) if summary_tag else ''
            news_list.append({
                'title': title_tag.get_text(strip=True),
                'link': link,
                'summary': summary or '',
                'source': 'MyAnimeList',
                'image': images[0] if images else None,
                'images': images,
                'video': None,
                'published_parsed': None,
            })
    except Exception as e:
        logger.error(f"MyAnimeList error: {e}")
    return news_list


def get_reddit_anime():
    news_list = []
    # Пробуем несколько URL по очереди (Reddit агрессивно банит)
    urls_to_try = [
        'https://www.reddit.com/r/anime/hot.json?limit=15',
        'https://old.reddit.com/r/anime/hot.json?limit=15',
        'https://www.reddit.com/r/anime/.rss',  # RSS как последний fallback
    ]
    headers = {
        'User-Agent': REDDIT_USER_AGENT,
        'Accept': 'application/json, text/html, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    # Опциональный прокси (если с сервера Reddit отдаёт 403 — заполни константу REDDIT_PROXY)
    proxies = None
    if REDDIT_PROXY:
        proxies = {'http': REDDIT_PROXY, 'https': REDDIT_PROXY}
        logger.debug("Reddit: используется прокси")

    data = None
    is_rss = False
    for url in urls_to_try:
        response = http_get_with_retry(
            url, headers=headers, timeout=HTTP_TIMEOUT, proxies=proxies,
        )
        if response is None:
            continue
        if response.status_code == 200:
            if url.endswith('.rss'):
                is_rss = True
                data = response.text
            else:
                try:
                    data = response.json()
                except ValueError as e:
                    logger.warning(f"Reddit {url}: не JSON ({e})")
                    continue
            logger.info(f"Reddit: использую {url}")
            break
        else:
            logger.warning(f"Reddit {url}: HTTP {response.status_code}")

    if data is None:
        logger.error("Reddit: все источники недоступны")
        return news_list

    # Маркеры служебных/мета-постов сабреддита — отбрасываем
    spam_markers = re.compile(
        r'\b('
        r'megathread|'
        r'daily\s+megathread|'
        r'daily\s+(thread|discussion)|weekly\s+(thread|discussion)|'
        r'questions[\s,]+(?:and\s+)?recommendations|'   # Anime Questions, Recommendations
        r'recommendations[\s,]+(?:and\s+)?discussion|'
        r'recommendations\s+thread|questions\s+thread|help\s+thread|'
        r'discord\.gg|'
        r'check\s+our\s+wiki|check\s+the\s+wiki|'
        r'casual\s+discussion'
        r')\b',
        re.IGNORECASE,
    )

    def is_spam_post(title: str, body: str = '') -> bool:
        if spam_markers.search(title):
            return True
        if body and spam_markers.search(body[:1000]):
            return True
        return False

    try:
        if is_rss:
            feed = feedparser.parse(data)
            for entry in feed.entries[:NEWS_PER_SOURCE * 3]:
                link = getattr(entry, 'link', None)
                if not link or link in sent_links:
                    continue
                title = entry.title
                # Фильтр по возрасту
                published_parsed = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
                if _is_too_old(published_parsed):
                    continue
                summary_html = entry.get('summary', '')
                summary_text = clean_html(summary_html)
                if is_spam_post(title, summary_text):
                    logger.info(f"Reddit: пропускаю служебный пост: {title[:60]}")
                    continue
                images = extract_all_images_from_entry(entry, summary_html)
                video_url = extract_video_url(entry, summary_html)
                news_list.append({
                    'title': title,
                    'link': link,
                    'summary': summary_text,
                    'source': 'Reddit r/anime',
                    'image': images[0] if images else None,
                    'images': images,
                    'video': video_url,
                    'published_parsed': published_parsed,
                })
                if len(news_list) >= NEWS_PER_SOURCE:
                    break
        else:
            # JSON API
            good_flairs = {'News'}
            count = 0
            for post in data['data']['children']:
                if count >= NEWS_PER_SOURCE:
                    break
                p = post['data']
                # Закреплённые посты — всегда служебные
                if p.get('stickied'):
                    continue
                # Фильтр по возрасту: created_utc это секунды
                created_utc = p.get('created_utc')
                if created_utc:
                    try:
                        post_dt = datetime.utcfromtimestamp(float(created_utc))
                        if datetime.now(timezone.utc).replace(tzinfo=None) - post_dt > timedelta(hours=settings.post_max_age_hours):
                            continue
                    except (TypeError, ValueError):
                        pass
                flair = p.get('link_flair_text') or ''
                if not any(f in flair for f in good_flairs):
                    continue
                title = p.get('title', '')
                selftext = p.get('selftext', '') or ''
                if is_spam_post(title, selftext):
                    logger.info(f"Reddit: пропускаю служебный пост: {title[:60]}")
                    continue
                link = 'https://reddit.com' + p['permalink']
                if link in sent_links:
                    continue

                # Собираем картинки. Приоритет: галерея > preview > thumbnail
                images: list[str] = []
                seen_imgs: set[str] = set()

                def _add_img(url: Optional[str]) -> None:
                    if not url:
                        return
                    url = html.unescape(url)
                    url = upgrade_image_url(url)
                    if url not in seen_imgs:
                        seen_imgs.add(url)
                        images.append(url)

                # Reddit-галерея (несколько фото в одном посте)
                if p.get('is_gallery') and p.get('media_metadata'):
                    gallery_order = [item['media_id'] for item in (p.get('gallery_data', {}).get('items', []))]
                    for mid in gallery_order[:MAX_PHOTOS_PER_POST]:
                        meta = p['media_metadata'].get(mid, {})
                        if meta.get('status') == 'valid' and meta.get('s', {}).get('u'):
                            _add_img(meta['s']['u'])

                # preview.images
                if not images:
                    for preview_img in p.get('preview', {}).get('images', [])[:MAX_PHOTOS_PER_POST]:
                        _add_img(preview_img.get('source', {}).get('url'))

                # thumbnail как последний fallback
                if not images:
                    thumbnail = p.get('thumbnail', '')
                    if isinstance(thumbnail, str) and thumbnail.startswith('http'):
                        _add_img(thumbnail)

                # Reddit-видео
                video_url = None
                secure_media = p.get('secure_media') or {}
                reddit_video = secure_media.get('reddit_video') or {}
                if reddit_video.get('fallback_url'):
                    video_url = reddit_video['fallback_url']
                elif p.get('url_overridden_by_dest') and _is_video_host(p['url_overridden_by_dest']):
                    video_url = p['url_overridden_by_dest']

                summary = selftext
                # Сохраним дату создания в формате struct_time для совместимости
                published_struct = None
                if created_utc:
                    try:
                        import time as _t
                        published_struct = _t.gmtime(float(created_utc))
                    except (TypeError, ValueError):
                        pass
                news_list.append({
                    'title': title,
                    'link': link,
                    'summary': summary,
                    'source': 'Reddit r/anime',
                    'image': images[0] if images else None,
                    'images': images,
                    'video': video_url,
                    'published_parsed': published_struct,
                })
                count += 1
    except Exception as e:
        logger.error(f"Reddit parse error: {e}")
    return news_list


# ============== TELEGRAM-КАНАЛЫ КАК ИСТОЧНИКИ ==============
# Читаем ПУБЛИЧНЫЕ каналы через веб-превью t.me/s/<канал> — без API, авторизации
# и telethon. Отдаёт последние ~20 постов с текстом, фото и датами.
# Посты на русском — помечаются lang='ru' и НЕ переводятся.
# Состав каналов легко менять: (имя_канала_без_@, метка_в_статистике)
TELEGRAM_CHANNELS = [
    ('nexvlsz', 'TG: Nexvlsz'),
    ('currentanimenews', 'TG: CurrentAnime'),
    ('ytkanews', 'TG: YtkaNews'),
    ('advance_emp', 'TG: Advance'),
]


def _detect_lang(text: str) -> Optional[str]:
    """Грубое определение языка поста: 'ru' если текст преимущественно кириллица,
    иначе None (значит переводим). Нужно потому, что TG-каналы бывают не только
    русские — итальянские/английские посты раньше уходили без перевода."""
    if not text:
        return None
    cyrillic = len(re.findall(r'[а-яёА-ЯЁ]', text))
    latin = len(re.findall(r'[a-zA-Z]', text))
    if cyrillic == 0:
        return None
    # Русский пост может содержать латинские названия тайтлов — важна пропорция
    return 'ru' if cyrillic >= latin else None


def _msg_own(msg, selector: str) -> list:
    """Элементы, принадлежащие ИМЕННО этому посту.

    t.me/s/ иногда отдаёт разметку, в которой соседние посты оказываются вложены
    друг в друга (незакрытый div в обёртке альбома). Тогда обычный msg.select()
    захватывает содержимое соседа: пост получал чужой текст/дату и публиковался
    со смесью «старый альбом + сегодняшний текст». Проверяем, что ближайший
    предок-сообщение — это сам msg."""
    out = []
    for el in msg.select(selector):
        holder = el.find_parent(class_='tgme_widget_message')
        if holder is msg:
            out.append(el)
    return out


def _msg_own_one(msg, selector: str):
    """Первый собственный элемент поста (или None)."""
    found = _msg_own(msg, selector)
    return found[0] if found else None


TG_EMBED_LOOKUPS_PER_RUN = 3    # сколько «ленивых» видео пробуем достать за проверку


def _parse_duration(text: str) -> Optional[int]:
    """'1:43' → 103 секунды. None, если не разобрать."""
    try:
        parts = [int(x) for x in (text or '').strip().split(':')]
    except ValueError:
        return None
    if not parts:
        return None
    return (parts[-1] + (parts[-2] * 60 if len(parts) > 1 else 0)
            + (parts[-3] * 3600 if len(parts) > 2 else 0))


def _extract_video_url(html_text: str):
    """Разбирает страницу отдельного поста.
    Возвращает (ссылка на mp4, длительность, каким способом нашли, кадр-превью).

    Кадр берём отсюда же: в ленте t.me/s/ превью видео — это размытая заглушка
    в десяток пикселей, а на странице поста лежит полноразмерный кадр."""
    soup = BeautifulSoup(html_text, 'html.parser')

    dur_el = soup.select_one('.tgme_widget_message_video_duration')
    duration = _parse_duration(dur_el.get_text()) if dur_el else None

    # --- Кадр-превью ---
    thumb = None
    for prop in ('og:image', 'twitter:image', 'og:image:url'):
        meta = (soup.select_one(f'meta[property="{prop}"]')
                or soup.select_one(f'meta[name="{prop}"]'))
        content = meta.get('content', '').strip() if meta else ''
        if content.startswith('http'):
            thumb = content
            break
    if not thumb:
        for el in soup.select('[class*="video"][style], [class*="photo"][style]'):
            mm = re.search(r"background-image:url\('([^']+)'\)", el.get('style', ''))
            if mm:
                thumb = mm.group(1)
                break

    # --- Ссылка на файл ---
    vid = soup.select_one('video[src]') or soup.select_one('video source[src]')
    if vid and vid.get('src'):
        return vid['src'], duration, 'тег video', thumb

    for prop in ('og:video', 'og:video:url', 'og:video:secure_url',
                 'twitter:player:stream'):
        meta = (soup.select_one(f'meta[property="{prop}"]')
                or soup.select_one(f'meta[name="{prop}"]'))
        if meta and meta.get('content', '').strip():
            return meta['content'].strip(), duration, f'мета {prop}', thumb

    # Ссылка может лежать внутри скрипта. Сначала ищем на известных хостах
    # Telegram (cdn-telegram.org, telesco.pe), потом любой mp4.
    for pattern in (r'https://[^"\'\s\\]+(?:cdn-telegram\.org|telesco\.pe)/file/[^"\'\s\\]+?\.mp4[^"\'\s\\]*',
                    r'https://[^"\'\s\\]+\.mp4[^"\'\s\\]*'):
        m = re.search(pattern, html_text)
        if m:
            return m.group(0), duration, 'ссылка в коде страницы', thumb

    return None, duration, '', thumb


def _ytdlp_telegram_video(post_id: str):
    """Последняя попытка достать видео — через yt-dlp (экстрактор telegram:embed).

    Он понимает ссылки вида t.me/канал/номер и вытаскивает прямую ссылку на файл
    там, где её нет в HTML: yt-dlp разбирает служебные данные страницы, до которых
    обычным парсингом не добраться. Сам файл НЕ качаем — берём только адрес,
    чтобы дальше применить свои лимиты по размеру.
    Возвращает (ссылка, длительность, кадр-превью)."""
    if not YT_DLP_AVAILABLE:
        return None, None, None
    url = f'https://t.me/{post_id}'
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'noplaylist': True, 'socket_timeout': 20, 'retries': 1,
        'extractor_args': {'generic': {'impersonate': ['']}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.info(f"  {post_id}: yt-dlp — не смог ({type(e).__name__}: {str(e)[:90]})")
        return None, None, None
    if not info:
        return None, None, None
    if info.get('_type') == 'playlist':          # в посте несколько видео
        entries = [e for e in (info.get('entries') or []) if e]
        info = entries[0] if entries else {}
    direct = info.get('url')
    if not direct:
        formats = [f for f in (info.get('formats') or []) if f.get('url')]
        direct = formats[-1]['url'] if formats else None
    if direct:
        logger.info(f"  {post_id}: yt-dlp — нашёл видео")
    return direct, info.get('duration'), info.get('thumbnail')


def _fetch_video_from_embed(post_id: str):
    """Пробует достать прямой mp4 со страницы отдельного поста.

    В ленте t.me/s/ Telegram часто НЕ отдаёт ссылку на видео — её подставляет
    скрипт. Обходим двумя адресами (embed-версия и обычная страница) и тремя
    способами разбора. Всё, что происходит, пишем в лог: без этого «видео нет»
    выглядит одинаково и когда страница не открылась, и когда файла там нет."""
    best_thumb = None
    for url in (f'https://t.me/{post_id}?embed=1&mode=tme',
                f'https://t.me/{post_id}'):
        kind = 'embed' if 'embed' in url else 'страница'
        try:
            r = http_get_with_retry(url, headers={'User-Agent': USER_AGENT},
                                    timeout=HTTP_TIMEOUT)
        except Exception as e:
            logger.info(f"  {post_id}: {kind} — запрос не удался ({type(e).__name__}: {e})")
            continue
        if not r or r.status_code != 200:
            logger.info(f"  {post_id}: {kind} — HTTP {r.status_code if r else 'нет ответа'}")
            continue
        direct, duration, how, thumb = _extract_video_url(r.text)
        if direct:
            logger.info(f"  {post_id}: {kind} — нашёл mp4 ({how})")
            return direct, duration, thumb
        if thumb:
            best_thumb = thumb
        # Отличаем «страница пустая/закрыта» от «страница есть, а файла нет»
        has_post = 'tgme_widget_message' in r.text
        logger.info(f"  {post_id}: {kind} — mp4 нет "
                    f"({'пост загружен, ссылки на файл в HTML не оказалось' if has_post else 'пост не отдался (приватный/защищённый?)'})"
                    + (', но забрал полноразмерный кадр' if best_thumb else ''))

    # HTML ничего не дал — пробуем yt-dlp, он копает глубже
    direct, duration, thumb = _ytdlp_telegram_video(post_id)
    if direct:
        return direct, duration, thumb or best_thumb
    return None, None, best_thumb or thumb


def get_telegram_channel(channel: str, label: str) -> list[dict]:
    """Парсит публичный Telegram-канал через t.me/s/. Возвращает список news-словарей."""
    url = f'https://t.me/s/{channel}'
    r = http_get_with_retry(url, headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT)
    if not r or r.status_code != 200:
        logger.warning(f"TG {channel}: HTTP {r.status_code if r else 'нет ответа'}")
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    news_list: list[dict] = []
    seen_ids: set[str] = set()
    embed_budget = TG_EMBED_LOOKUPS_PER_RUN   # не тормозим цикл лишними запросами
    for msg in soup.select('div.tgme_widget_message'):
        post_id = msg.get('data-post')          # вида 'channel/123'
        text_el = _msg_own_one(msg, 'div.tgme_widget_message_text')
        if not post_id or not text_el:
            continue                             # пост без текста — пропускаем
        if post_id in seen_ids:
            continue                             # тот же пост встретился дважды
        seen_ids.add(post_id)
        full_text = text_el.get_text('\n', strip=True)
        if len(full_text) < 15:
            continue
        lines = [ln.strip() for ln in full_text.split('\n') if ln.strip()]
        title = lines[0][:200]
        summary = ' '.join(lines[1:])[:1000] if len(lines) > 1 else ''
        # Дата поста
        published_parsed = None
        t = _msg_own_one(msg, 'time[datetime]')
        if t and t.get('datetime'):
            try:
                dt = datetime.fromisoformat(t['datetime'].replace('Z', '+00:00'))
                published_parsed = dt.timetuple()
            except ValueError:
                pass
        # Без собственной даты пост не берём: у TG-постов дата есть всегда, а её
        # отсутствие означает кривую разметку — такой пост мог бы обойти фильтр
        # свежести и вылезти месячной давности.
        if published_parsed is None:
            logger.debug(f"TG {channel}: пост {post_id} без даты — пропускаю")
            continue
        if _is_too_old(published_parsed):
            continue
        # Фото: обёртки со style="background-image:url('...')"
        images: list[str] = []
        for wrap in _msg_own(msg, 'a.tgme_widget_message_photo_wrap[style]'):
            m = re.search(r"background-image:url\('([^']+)'\)", wrap.get('style', ''))
            if m:
                images.append(m.group(1))
        # Видео: t.me/s/ отдаёт его в разных формах. Прямой mp4 бывает в <video src>,
        # но часто — только превью-обёртка (.._video_thumb / .._video_player) с фоновым
        # изображением, а сам файл подгружается по клику. Берём mp4 если он доступен
        # напрямую и не длиннее 5 минут; иначе достаём превью-кадр в images, чтобы пост
        # не остался без картинки.
        video_url = None
        thumb_only = False
        video_note = ''
        video_thumb = None
        dur_el = _msg_own_one(msg, '.tgme_widget_message_video_duration')
        # Признак видео ищем широко: у t.me/s/ имена классов отличаются от случая
        # к случаю (одиночный ролик, видео внутри альбома, кружок), и жёсткий
        # список из трёх классов пропускал видео-посты у части каналов.
        video_tag = _msg_own_one(msg, 'video')
        video_box = _msg_own_one(msg, '[class*="video"]')
        has_video_marker = bool(dur_el or video_tag or video_box)
        if has_video_marker:
            dur_s = _parse_duration(dur_el.get_text()) if dur_el else None

            # Кадр-превью запоминаем ВСЕГДА: пригодится, если ролик не доедет
            # Кадр берём из любой видео-обёртки с фоновой картинкой —
            # по той же причине не привязываемся к конкретным классам.
            video_thumb = None
            for el in _msg_own(msg, '[class*="video"][style]'):
                mm = re.search(r"background-image:url\('([^']+)'\)", el.get('style', ''))
                if mm:
                    video_thumb = mm.group(1)
                    break

            # Прямой mp4 в разметке ленты
            vid = _msg_own_one(msg, 'video[src]')
            direct = vid.get('src') if vid else None
            if not direct:
                src_el = _msg_own_one(msg, 'video source[src]')
                direct = src_el.get('src') if src_el else None

            # Нет ссылки в ленте — идём на страницу самого поста
            if not direct and embed_budget > 0:
                embed_budget -= 1
                direct, embed_dur, embed_thumb = _fetch_video_from_embed(post_id)
                if embed_thumb:
                    # Кадр со страницы поста всегда лучше размытой заглушки из ленты
                    video_thumb = embed_thumb
                if direct:
                    # Источники длительности могут расходиться (подпись в ленте
                    # против метаданных файла) — берём большее, чтобы случайно
                    # не протащить ролик длиннее лимита.
                    known = [d for d in (dur_s, embed_dur) if d]
                    dur_s = max(known) if known else None
                    video_note = 'ссылка добыта со страницы поста'

            if direct and (dur_s is None or dur_s <= TG_VIDEO_MAX_SECONDS):
                video_url = direct
                video_note = video_note or f'прямой mp4 ({dur_s if dur_s else "?"}с)'
            else:
                if direct:
                    video_note = (f'ролик {dur_s}с длиннее лимита '
                                  f'{TG_VIDEO_MAX_SECONDS}с — только кадр')
                else:
                    video_note = 'Telegram не отдал mp4 даже на странице поста — только кадр'
                if video_thumb:
                    # Ставим кадр первым и убираем размытую заглушку из ленты
                    images = [video_thumb] + [i for i in images if i != video_thumb]
                    thumb_only = True
            logger.info(f"TG {post_id}: видео — {video_note}")
        news_list.append({
            'title': title,
            'link': f'https://t.me/{post_id}',
            'summary': summary,
            'images': images[:MAX_PHOTOS_PER_POST],
            'video': video_url,
            'published_parsed': published_parsed,
            'source': label,
            # Язык определяем по тексту: TG-каналы бывают не только русские
            # (напр. итальянский @VanitasNews) — их надо переводить.
            'lang': _detect_lang(full_text),
            # True — единственная картинка это мыльный кадр-превью видео,
            # перед отправкой попробуем найти вариант получше
            '_thumb_only': thumb_only,
            '_video_thumb': video_thumb,      # запасной кадр, если ролик не доедет
            '_video_note': video_note,        # что случилось с видео — видно в /logs
        })
    # На странице свежие посты ВНИЗУ — берём последние
    result = news_list[-NEWS_PER_SOURCE:]
    logger.info(f"TG {channel}: собрано {len(result)} постов (на странице {len(news_list)})")
    return result


# ============== КИНО / СЕРИАЛЫ / ГИК ==============
def get_animatetimes() -> list[dict]:
    """AnimateTimes — крупный японский аниме-портал. Публичного RSS нет,
    парсим свежие новости с главной (/news/details.php?id=N).
    Заголовки японские — переводятся DeepL (JA→RU он умеет).
    Превью в списке нет — картинку подтянет og:image-fallback при отправке."""
    r = http_get_with_retry('https://www.animatetimes.com/',
                            headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT)
    if not r or r.status_code != 200:
        logger.warning(f"AnimateTimes: HTTP {r.status_code if r else 'нет ответа'}")
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    news_list: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/news/details.php?id="]'):
        href = a.get('href', '')
        m = re.search(r'/news/details\.php\?id=(\d+)', href)
        if not m:
            continue
        link = f'https://www.animatetimes.com/news/details.php?id={m.group(1)}'
        if link in seen:
            continue
        title = a.get_text(' ', strip=True)
        if not title or len(title) < 10:
            continue
        # В разметке заголовок часто задвоен (alt картинки + текст): «X X» → «X»
        half = len(title) // 2
        if len(title) % 2 == 1 and title[:half] == title[half + 1:]:
            title = title[:half]
        seen.add(link)
        # Картинка-превью внутри ссылки, если есть
        images: list[str] = []
        img = a.select_one('img[src]')
        if img:
            norm = _normalize_image_url(img['src'], link)
            if norm:
                images.append(norm)
        news_list.append({
            'title': title[:250],
            'link': link,
            'summary': '',
            'images': images,
            'video': None,
            'published_parsed': None,
            'source': 'AnimateTimes(JP)',
        })
        if len(news_list) >= NEWS_PER_SOURCE:
            break
    return news_list


def get_filmix() -> list[dict]:
    """Filmix — русские новости кино и сериалов (/mnews/).
    Контент на русском — lang='ru', перевод не нужен."""
    r = http_get_with_retry('https://filmix.gg/mnews/',
                            headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT)
    if not r or r.status_code != 200:
        logger.warning(f"Filmix: HTTP {r.status_code if r else 'нет ответа'}")
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    news_list: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('h2 a[href*="/mnews/"], h3 a[href*="/mnews/"]'):
        href = a.get('href', '')
        if not re.search(r'/mnews/\d+-', href):
            continue
        link = href if href.startswith('http') else f'https://filmix.gg{href}'
        title = a.get_text(' ', strip=True)
        if link in seen or len(title) < 10:
            continue
        seen.add(link)
        # Превью: другая ссылка на тот же адрес с <img> внутри
        images: list[str] = []
        img = soup.select_one(f'a[href="{href}"] img[src]')
        if img:
            norm = _normalize_image_url(img['src'], link)
            if norm:
                images.append(norm)
        # Сниппет: первый содержательный блок после заголовка
        summary = ''
        holder = a.find_parent(['h2', 'h3'])
        sib = holder.find_next_sibling() if holder else None
        hops = 0
        while sib is not None and hops < 4:
            text = sib.get_text(' ', strip=True) if hasattr(sib, 'get_text') else ''
            if len(text) > 40:
                summary = text[:600]
                break
            sib = sib.find_next_sibling()
            hops += 1
        news_list.append({
            'title': title[:250],
            'link': link,
            'summary': summary,
            'images': images,
            'video': None,
            'published_parsed': None,
            'source': 'Filmix',
            'lang': 'ru',
        })
        if len(news_list) >= NEWS_PER_SOURCE:
            break
    return news_list


def get_collider():
    """Collider — кино и сериалы."""
    return _parse_rss_with_fallback('https://collider.com/feed/', 'Collider')


def get_slashfilm():
    """/Film — кино-новости и обзоры."""
    return _parse_rss_with_fallback('https://www.slashfilm.com/feed/', '/Film')


def get_variety():
    """Variety — индустрия кино и ТВ."""
    return _parse_rss_with_fallback('https://variety.com/feed/', 'Variety')


def get_polygon():
    """Polygon — гик-культура: игры, кино, сериалы."""
    return _parse_rss_with_fallback('https://www.polygon.com/rss/index.xml', 'Polygon')


def get_comingsoon():
    """ComingSoon — анонсы фильмов и сериалов."""
    return _parse_rss_with_fallback('https://www.comingsoon.net/feed', 'ComingSoon')


SOURCES = [
    # 🟢 Топ-3 — основные продуктивные
    ('ComicBook Anime', get_comicbook_anime),
    ('CBR Anime', get_cbr_anime),
    ('MyAnimeList', get_myanimelist),
    # 🟡 С force_og — обещают давать картинки через og:image
    ('AnimeNewsNetwork', get_animenewsnetwork),
    ('ANN Newsroom', get_ann_newsroom),
    ('ANN Industry', get_ann_industry),
    ('Anime Corner', get_anime_corner),
    ('Anime Herald', get_animeherald),
    # 🟡 Редкие, но иногда дают свежие
    ('Crunchyroll', get_crunchyroll_news),
    ("Honey's Anime", get_honeys_anime),
    ('AnimeHunch', get_animehunch),
    ('AnimateTimes(JP)', get_animatetimes),
    # 🎬 Кино / сериалы / гик (канал расширен до гик-тематики).
    # Мёртвые ленты можно отключить в /settings → Источники.
    ('Collider', get_collider),
    ('/Film', get_slashfilm),
    ('Variety', get_variety),
    ('Polygon', get_polygon),
    ('ComingSoon', get_comingsoon),
    ('Filmix', get_filmix),
    # Kotaku и Yatta-Tachi отключены: за 18+ часов работы на сервере — 0 собранных
    # новостей (RSS пустой или недоступен). Функции оставлены — можно вернуть
    # раскомментировав, если ленты оживут.
    # ('Kotaku', get_kotaku_anime),
    # ('Yatta-Tachi', get_yatta_tachi),
    # Reddit отключён: банит серверные IP (403 на все запросы с хостинга).
    # Функция get_reddit_anime оставлена в коде — при наличии рабочего прокси
    # (REDDIT_PROXY) можно вернуть строку ниже.
    # ('Reddit', get_reddit_anime),
]

# Telegram-каналы подключаются из TELEGRAM_CHANNELS (см. выше).
# lambda с default-аргументами фиксирует канал для каждой записи.
for _tg_ch, _tg_label in TELEGRAM_CHANNELS:
    SOURCES.append((_tg_label, (lambda _c=_tg_ch, _l=_tg_label: get_telegram_channel(_c, _l))))


# ============== ФИЛЬТР И ФОРМАТИРОВАНИЕ ==============
_BLACKLIST_PATTERN: Optional[re.Pattern] = None


def _get_blacklist_pattern() -> Optional[re.Pattern]:
    """Лениво компилирует regex из BLACKLIST. Кешируется."""
    global _BLACKLIST_PATTERN
    if _BLACKLIST_PATTERN is not None:
        return _BLACKLIST_PATTERN
    if not BLACKLIST:
        return None
    parts = [re.escape(w) for w in BLACKLIST]
    _BLACKLIST_PATTERN = re.compile(r'\b(?:' + '|'.join(parts) + r')\b', re.IGNORECASE)
    return _BLACKLIST_PATTERN


def matches_blacklist(news: dict) -> Optional[str]:
    """Если в посте есть запрещённое слово — возвращает само слово.
    Иначе None."""
    pattern = _get_blacklist_pattern()
    if not pattern:
        return None
    # Проверяем заголовок + первые 500 символов summary (чтобы не сканировать огромный текст)
    text = (news.get('title', '') + ' ' + news.get('summary', '')[:500])
    m = pattern.search(text)
    return m.group(0) if m else None


# Дайджесты и самореклама источников — не новости, отсеиваем по заголовку/началу текста.
# Проверяется на ОРИГИНАЛЬНОМ английском тексте до перевода.
DIGEST_SKIP_PATTERNS = [
    re.compile(r'north american anime,?\s*manga releases', re.IGNORECASE),
    re.compile(r'this week in (anime|manga|games)', re.IGNORECASE),
    re.compile(r'weekly (anime|manga|news) (round-?up|digest|recap)', re.IGNORECASE),
    re.compile(r'come (visit|see) us at', re.IGNORECASE),
    re.compile(r'our panels?,? events?,? and booth', re.IGNORECASE),
    re.compile(r'(anime expo|comic-?con|ax) \d{4}\s+(news|coverage|guide|preview)', re.IGNORECASE),
    re.compile(r'all (of )?our .{0,30}(news|coverage|reviews)', re.IGNORECASE),
]


def matches_keywords(news: dict) -> bool:
    """Применяет whitelist (KEYWORDS) и blacklist. Возвращает True если пост подходит."""
    # 1) Blacklist — жёсткий отказ
    blocked = matches_blacklist(news)
    if blocked:
        logger.info(f"⊘ Blacklist: пост содержит '{blocked}': {news.get('title', '')[:60]}")
        return False
    # 1b) Дайджесты и промо источников — не новости
    check_text = (news.get('title') or '') + ' ' + (news.get('summary') or '')[:300]
    for pattern in DIGEST_SKIP_PATTERNS:
        if pattern.search(check_text):
            logger.info(f"⊘ Дайджест/промо: {news.get('title', '')[:60]}")
            return False
    # 2) Whitelist — если задан
    if not KEYWORDS:
        return True
    text = (news['title'] + ' ' + news['summary']).lower()
    return any(kw.lower() in text for kw in KEYWORDS)


def _extract_first_sentence(text: str, max_len: int = 300) -> str:
    """Извлекает первое предложение из текста.
    Обрезает на границе предложения (. ! ?). Если предложение слишком длинное —
    аккуратно укорачивает. Убирает хвост '[...]' от обрезанных RSS-превью."""
    if not text:
        return ''
    text = text.strip()

    # Убираем '[...]', '[…]', 'Read more' и подобные хвосты обрезки
    text = re.sub(r'\s*\[\.{2,3}\]\s*$', '', text)
    text = re.sub(r'\s*\[…\]\s*$', '', text)
    text = re.sub(r'\s*\(?(?:read more|continue reading|подробнее)\)?\s*$', '', text, flags=re.IGNORECASE)

    # Ищем конец первого предложения. Точка/!/? за которыми пробел+заглавная или конец строки.
    # Избегаем ложных срабатываний на сокращениях (No. 8, Dr. Stone, vol. 2 и т.п.):
    # lookbehind (?<!\s\d) не даёт считать границей точку сразу после одиночной цифры
    # («Akuma de Sourou 4. Doctor…» — не граница; «…в 2026. Новый…» — граница, т.к. 4 цифры).
    match = re.search(r'(?<!\s\d)[.!?](?:\s+[«"A-ZА-ЯЁ]|\s*$)|[。！？]', text)
    if match:
        sentence = text[:match.start() + 1].strip()
    else:
        # Нет явной границы — берём весь текст
        sentence = text

    # Если предложение всё ещё длиннее лимита — укорачиваем аккуратно
    if len(sentence) > max_len:
        sentence = smart_truncate(sentence, max_len)

    # Чистим мусорные хвосты, оставшиеся от обрезки источником/переводом:
    # «…с Naruto,…» → «…с Naruto»; «студии TriF.(с» → «студии TriF.»
    sentence = re.sub(r'\s*,\s*(?:…|\.{2,3})\s*$', '', sentence)   # висящее «,…» / «, ...»
    sentence = re.sub(r'\s*\([^)]{0,6}$', '', sentence)            # незакрытая скобка с обрывком
    sentence = re.sub(r'[\s,;:—–-]+$', '', sentence)               # висящие знаки в конце

    return sentence.strip()


def _extract_sentences(text: str, max_sentences: int = 3, max_len: int = 700) -> str:
    """Извлекает до max_sentences первых предложений (для более полного текста поста).
    Границы предложений — латинские/кириллические . ! ? и японские 。！？.
    Общая длина ограничена max_len. Хвосты-обрывки чистятся как в _extract_first_sentence."""
    if not text:
        return ''
    text = text.strip()
    # Чистим хвосты обрезки источником
    text = re.sub(r'\s*\[\.{2,3}\]\s*$', '', text)
    text = re.sub(r'\s*\[…\]\s*$', '', text)
    text = re.sub(r'\s*\(?(?:read more|continue reading|подробнее)\)?\s*$', '', text, flags=re.IGNORECASE)

    sentences: list[str] = []
    pos = 0
    # Тот же паттерн границы, что и для одного предложения (учитывает сокращения и цифры)
    pattern = re.compile(r'(?<!\s\d)[.!?](?:\s+[«"A-ZА-ЯЁ]|\s*$)|[。！？]')
    for m in pattern.finditer(text):
        end = m.start() + 1
        chunk = text[pos:end].strip()
        if chunk:
            sentences.append(chunk)
        pos = end
        if len(sentences) >= max_sentences:
            break
    # Если границ не нашлось совсем — берём весь текст как одно «предложение»
    if not sentences:
        sentences = [text]

    result = ' '.join(sentences).strip()
    if len(result) > max_len:
        result = smart_truncate(result, max_len)
    # Финальная чистка висящих знаков
    result = re.sub(r'\s*,\s*(?:…|\.{2,3})\s*$', '', result)
    result = re.sub(r'\s*\([^)]{0,6}$', '', result)
    result = re.sub(r'[\s,;:—–-]+$', '', result)
    return result.strip()


def _format_post_date(published_struct) -> str:
    """Форматирует дату новости как 'D месяца' (напр. '1 июля').
    Возвращает пустую строку если даты нет или она невалидна."""
    if not published_struct:
        return ''
    try:
        pub = datetime(*published_struct[:6])
    except (TypeError, ValueError):
        return ''
    return f'{pub.day} {RU_MONTHS.get(pub.month, "")}'.strip()


def _with_tags(text: str, news: dict) -> str:
    """Дописывает хэштеги от модели в конец поста.

    Настройку проверяем именно здесь, а не только при обращении к модели:
    пост мог пролежать в ветке с уже готовыми тегами, а админ тем временем
    их выключил — в канал они уйти не должны."""
    if settings is not None and not getattr(settings, 'llm_tags', True):
        return text
    tags = news.get('_llm_tags')
    if not tags or tags in text:
        return text
    return f'{text}\n\n{tags}'


def format_news_short(news: dict) -> str:
    """Короткий формат поста: заголовок + одно предложение сути + дата.
    Используется и для канала, и для ветки. Без воды.
    Посты с lang='ru' (русские Telegram-каналы) не переводятся.
    Если админ правил текст вручную (_edited_text) — отдаём его как есть."""
    edited = news.get('_edited_text')
    if edited:
        return edited
    # Текст от языковой модели (если она включена и ответила адекватно)
    llm_text = news.get('_llm_text')
    if llm_text:
        return _with_tags(llm_text, news)
    is_ru = news.get('lang') == 'ru'

    # Эпизоды форматируем отдельно (они и так короткие); парсер английский
    if not is_ru:
        ep = parse_episode(news['title'])
        if ep:
            return format_episode_post(ep, news.get('published_parsed'))

    # Заголовок
    raw_title = news['title']
    ru_title = (raw_title if is_ru else translate_text(raw_title)).rstrip('.')
    # Санити-чек: если перевод «съел» заголовок до огрызка («Netflix.») —
    # лучше показать оригинал целиком, чем обрывок.
    if (not is_ru and len(ru_title) < 15
            and len(raw_title.rstrip('.')) > len(ru_title) * 2.5):
        logger.warning(f"Перевод заголовка подозрительно короткий "
                       f"({ru_title!r} из {raw_title!r}) — использую оригинал")
        ru_title = raw_title.rstrip('.')
    if ru_title and not ru_title.endswith(('.', '!', '?', '…', ':')):
        ru_title += '.'

    # До трёх предложений из описания (более полный текст, влезает в caption 1024)
    summary = news.get('summary') or ''
    ru_summary = ''
    if summary:
        excerpt = _extract_sentences(summary, max_sentences=3, max_len=700)
        if excerpt:
            ru_summary = excerpt if is_ru else translate_text(excerpt, input_limit=1200)
            # После перевода приводим к <=3 предложениям и разумной длине
            ru_summary = _extract_sentences(ru_summary, max_sentences=3, max_len=850)

    # Если предложение дублирует заголовок — не показываем
    if ru_summary and ru_title.rstrip('.').lower() in ru_summary.lower():
        ru_summary = ''

    # Дата СОБЫТИЯ из текста новости (не дата публикации RSS!).
    # Ищем в оригинальном английском тексте — там форматы дат предсказуемы.
    # Если конкретной даты в тексте нет — строка даты не показывается вообще.
    search_text = (news.get('title') or '') + ' ' + (news.get('summary') or '')[:600]
    date_str = extract_release_date_from_text(search_text)

    # Собираем: заголовок / предложение / дата
    parts = [ru_title]
    if ru_summary:
        parts.append(ru_summary)
    body = '\n\n'.join(parts)
    if date_str:
        body += f'\n\n📅 {date_str}'
    body = _with_tags(body, news)
    return body


def format_news_text_long(news: dict) -> str:
    """Формат текста для ветки — теперь тоже короткий (заголовок + предложение + дата)."""
    return format_news_short(news)


def format_news_post(news: dict) -> str:
    """Формат поста для канала — короткий: заголовок + предложение + дата."""
    return format_news_short(news)


# ============== ОТПРАВКА ==============
def fit_to_limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


async def _prepare_video_file(news: dict) -> Optional[Path]:
    """Если у новости есть видео — пытается его скачать. Возвращает путь к файлу или None.
    Прямые видео (.mp4 и т.д.) возвращаются как URL-ссылка не здесь — для них Telegram сам качает.
    Здесь занимаемся только yt-dlp-хостингами."""
    if not settings.video_enabled:
        news.setdefault('_video_note', 'настройка «🎬 Видео» выключена')
        return None
    video_url = news.get('video')
    if not video_url:
        if _probably_has_video(news):
            news.setdefault('_video_note', 'новость про ролик, но ссылки на него нет')
        return None
    # Прямой mp4/webm — Telegram скачает сам, нам качать не надо
    if _is_direct_video(video_url):
        return None
    if not _is_video_host(video_url):
        news['_video_note'] = f'хостинг не поддерживается: {urlparse(video_url).netloc}'
        return None
    if not YT_DLP_AVAILABLE:
        news['_video_note'] = 'yt-dlp не установлен — ролик не скачать'
        return None
    note: list = []
    path = await asyncio.to_thread(download_video, video_url, note)
    if note:
        news['_video_note'] = note[0]
    return path


def _add_video_link_to_text(text: str, video_url: str) -> str:
    """Добавляет ссылку на видео в текст поста (когда не встраиваем его).
    Для cdn-telegram/telesco ссылку НЕ добавляем: она гигантская, нечитаемая
    и быстро протухает — читателю бесполезна."""
    if _download_needed_host(video_url):
        return text
    return f'{text}\n\n🎬 Смотреть: {video_url}'


async def _send_post(bot: Bot, news: dict, target, video_file: Optional[Path],
                     thread_id: Optional[int] = None) -> bool:
    """Главная отправка: собирает альбом из видео и фото, шлёт media group или одиночное сообщение.
    Если thread_id указан — отправляет в конкретную тему форума (ветку обсуждения)."""
    text = format_news_post(news)
    video_url = news.get('video')

    # Доп. kwargs для отправки в тему форума
    thread_kw = {'message_thread_id': thread_id} if thread_id is not None else {}

    # Что реально отправим как видео: файл | bytes | url.
    # cdn-telegram Bot API по URL не принимает — качаем сами (как и фото).
    video_media = None
    if settings.video_enabled and video_file is None and video_url \
            and _is_direct_video(video_url):
        video_media = await _resolve_video(video_url)
    has_inline_video = settings.video_enabled and (
        video_file is not None or video_media is not None
    )
    if video_url and not has_inline_video:
        text = _add_video_link_to_text(text, video_url)

    safe_text = html.escape(text)
    caption = fit_to_limit(safe_text, TG_CAPTION_LIMIT)

    photos = _dedup_image_variants(news.get('images') or [])
    # Ролик не доехал, а картинок нет — ставим кадр-превью из самого поста,
    # он всегда лучше, чем случайная og:image со страницы канала.
    if not photos and video_media is None and news.get('_video_thumb'):
        photos = [news['_video_thumb']]
        logger.info(f"🎬 Видео не доехало — кадр из поста: {news.get('title', '')[:50]}")
    # Картинки с хостов, которые Bot API не может скачать по URL (cdn-telegram.org
    # из t.me/s/-постов), заранее качаем байтами — иначе публикация в канал падала
    # с webpage_curl_failed / "Wrong type of the web page content" все 3 попытки.
    if photos:
        photos = await _resolve_photos_for_album(photos)
    media_count = len(photos) + (1 if has_inline_video else 0)

    # ЖЁСТКОЕ ПРАВИЛО: если включено "Только с картинками" и медиа нет — НЕ публикуем
    if settings.require_image and media_count == 0:
        logger.info(f"⊘ Пропускаю пост без медиа (require_image): {news['title'][:60]}")
        return False

    # --- Случай 1: Только текст ---
    if media_count == 0:
        try:
            await bot.send_message(
                chat_id=target,
                text=fit_to_limit(safe_text, TG_TEXT_LIMIT),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
                **thread_kw,
            )
            logger.info(f"📝 {news['source']}: {news['title'][:60]}")
            return True
        except TelegramError as e:
            logger.error(f"Не удалось отправить текст: {e}")
            return False

    # --- Случай 2: Один медиа-объект (1 фото или 1 видео) ---
    if media_count == 1:
        if has_inline_video:
            try:
                if video_file:
                    with open(video_file, 'rb') as f:
                        await bot.send_video(
                            chat_id=target, video=f, caption=caption,
                            parse_mode=ParseMode.HTML, supports_streaming=True,
                            **thread_kw,
                        )
                else:
                    # Прямой видео-URL
                    await bot.send_video(
                        chat_id=target, video=video_media, caption=caption,
                        parse_mode=ParseMode.HTML, supports_streaming=True,
                        **thread_kw,
                    )
                logger.info(f"🎬 {news['source']}: {news['title'][:60]}")
                return True
            except TelegramError as e:
                if settings.require_image:
                    logger.warning(f"⊘ Видео не отправилось ({e}), require_image включено — пост пропущен")
                    return False
                logger.warning(f"Видео не отправилось ({e}), шлю текстом")
                # fallback на текст
                fallback_text = _add_video_link_to_text(text, video_url) if video_url else text
                try:
                    await bot.send_message(
                        chat_id=target,
                        text=fit_to_limit(html.escape(fallback_text), TG_TEXT_LIMIT),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=False,
                        **thread_kw,
                    )
                    return True
                except TelegramError as e2:
                    logger.error(f"Текстовый fallback тоже упал: {e2}")
                    return False
        else:
            # Одна фотка
            try:
                await bot.send_photo(
                    chat_id=target, photo=photos[0], caption=caption,
                    parse_mode=ParseMode.HTML,
                    **thread_kw,
                )
                logger.info(f"📷 {news['source']}: {news['title'][:60]}")
                return True
            except TelegramError as e:
                # URL не принят Bot API — скачиваем сами и шлём байтами
                if isinstance(photos[0], str):
                    data = await asyncio.to_thread(_download_image_bytes, photos[0])
                    if data:
                        try:
                            await bot.send_photo(
                                chat_id=target, photo=data, caption=caption,
                                parse_mode=ParseMode.HTML, **thread_kw,
                            )
                            logger.info(f"📷 {news['source']}: {news['title'][:60]} (байтами)")
                            return True
                        except TelegramError as e2:
                            e = e2
                if settings.require_image:
                    logger.warning(f"⊘ Фото не отправилось ({e}), require_image включено — пост пропущен")
                    return False
                logger.warning(f"Фото не отправилось ({e}), шлю текстом")
                try:
                    await bot.send_message(
                        chat_id=target,
                        text=fit_to_limit(safe_text, TG_TEXT_LIMIT),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=False,
                        **thread_kw,
                    )
                    return True
                except TelegramError as e2:
                    logger.error(f"Текстовый fallback тоже упал: {e2}")
                    return False

    # --- Случай 3: Альбом (media group) ---
    # Telegram limit: 10 элементов в группе
    media: list = []
    opened_files: list = []  # чтобы корректно закрыть после отправки

    try:
        # Видео идёт первым, чтобы caption на нём
        if has_inline_video:
            if video_file:
                f = open(video_file, 'rb')
                opened_files.append(f)
                media.append(InputMediaVideo(
                    media=f, caption=caption, parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                ))
            else:
                # Прямой видео-URL
                media.append(InputMediaVideo(
                    media=video_media, caption=caption, parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                ))
            # Дальше фото без caption
            for photo_url in photos[:9]:  # 1 видео + до 9 фото = 10
                media.append(InputMediaPhoto(media=photo_url))
        else:
            # Только фото. Caption на первой.
            for i, photo_url in enumerate(photos[:10]):
                if i == 0:
                    media.append(InputMediaPhoto(
                        media=photo_url, caption=caption, parse_mode=ParseMode.HTML,
                    ))
                else:
                    media.append(InputMediaPhoto(media=photo_url))

        try:
            await bot.send_media_group(chat_id=target, media=media, **thread_kw)
            kind = '🎬+🖼' if has_inline_video else '🖼'
            logger.info(f"{kind} {news['source']}: {news['title'][:60]} ({len(media)} медиа)")
            return True
        except TelegramError as e:
            logger.warning(f"Альбом не отправился ({e}), пробую одиночно")
            # Fallback: пробуем по очереди — сначала видео/первая фотка с caption, остальное без
            return await _send_post_fallback(bot, news, target, video_file, photos, caption, safe_text, has_inline_video, thread_id)
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass


async def _send_post_fallback(
    bot: Bot, news: dict, target,
    video_file: Optional[Path], photos: list[str],
    caption: str, safe_text: str, has_inline_video: bool,
    thread_id: Optional[int] = None,
) -> bool:
    """Если media group не прошла — шлём первый медиа-объект с caption, остальные следом без."""
    thread_kw = {'message_thread_id': thread_id} if thread_id is not None else {}
    try:
        sent_first = False
        if has_inline_video:
            video_url = news.get('video')
            if video_file:
                with open(video_file, 'rb') as f:
                    await bot.send_video(
                        chat_id=target, video=f, caption=caption,
                        parse_mode=ParseMode.HTML, supports_streaming=True,
                        **thread_kw,
                    )
            else:
                await bot.send_video(
                    chat_id=target, video=video_media, caption=caption,
                    parse_mode=ParseMode.HTML, supports_streaming=True,
                    **thread_kw,
                )
            sent_first = True
            for ph in photos[:9]:
                try:
                    await bot.send_photo(chat_id=target, photo=ph, **thread_kw)
                except TelegramError:
                    pass
                await asyncio.sleep(0.3)
        elif photos:
            try:
                await bot.send_photo(
                    chat_id=target, photo=photos[0], caption=caption,
                    parse_mode=ParseMode.HTML,
                    **thread_kw,
                )
            except TelegramError:
                # URL не принят — качаем байтами (типично для cdn-telegram.org)
                data = None
                if isinstance(photos[0], str):
                    data = await asyncio.to_thread(_download_image_bytes, photos[0])
                if not data:
                    raise
                await bot.send_photo(
                    chat_id=target, photo=data, caption=caption,
                    parse_mode=ParseMode.HTML,
                    **thread_kw,
                )
            sent_first = True
            for ph in photos[1:10]:
                try:
                    await bot.send_photo(chat_id=target, photo=ph, **thread_kw)
                except TelegramError:
                    pass
                await asyncio.sleep(0.3)
        if sent_first:
            logger.info(f"📩 {news['source']}: {news['title'][:60]} (одиночными)")
            return True
        # Совсем не получилось — текст
        await bot.send_message(
            chat_id=target,
            text=fit_to_limit(safe_text, TG_TEXT_LIMIT),
            parse_mode=ParseMode.HTML, disable_web_page_preview=False,
            **thread_kw,
        )
        return True
    except TelegramError as e:
        logger.error(f"Fallback провалился: {e}")
        return False


# Посты, которые прямо сейчас отправляются. Отправка занимает секунды, и за это
# время можно успеть нажать кнопку второй раз или получить тик планировщика —
# без этой защиты пост уходил в канал дважды.
_publishing_now: set[str] = set()


class _PublishGuard:
    """Не даёт начать вторую отправку того же поста."""

    def __init__(self, key: str):
        self.key = str(key)
        self.acquired = False

    def __enter__(self):
        if self.key in _publishing_now:
            return self
        _publishing_now.add(self.key)
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:
            _publishing_now.discard(self.key)
        return False


async def _prepare_news_for_send(news: dict, source: str,
                                count_stats: bool = True) -> Optional[str]:
    """Общий конвейер подготовки поста: картинка, модель, дедупы.

    Живёт отдельно, потому что путей отправки два — в ветку и напрямую в канал.
    Раньше вся обработка была вшита только в первый, и режим канала публиковал
    сырые машинные переводы без фильтров и тегов. Любая новая проверка теперь
    автоматически действует в обоих режимах.

    Возвращает код пропуска ('skipped_filter' / 'skipped_dup') или None, если
    пост можно отправлять."""
    await _improve_thumb(news)

    # Модель: перевод, чистый текст, теги, отсев непрофильного и повторов
    if await _llm_enrich(news) == 'skip':
        if count_stats:
            await stats.record_skipped('filtered', source)
        return 'skipped_filter'

    # Дедуп по картинке: один кадр с разных сайтов под разными заголовками
    dup_title = await _image_duplicate(news)
    if dup_title:
        logger.info(f"⊘ Картинка уже публиковалась («{dup_title[:40]}»): "
                    f"{news.get('title', '')[:60]}")
        if count_stats:
            await stats.record_skipped('duplicate', source)
        return 'skipped_dup'

    # Последняя проверка — на готовом тексте. Все предыдущие сравнивают исходные
    # заголовки, а одна новость с двух сайтов приходит разными формулировками
    # и совпадает только после перевода.
    if published_texts is not None and settings.dedup_final_text:
        final_text = format_news_short(news)
        twin = published_texts.find_similar(final_text)
        if twin:
            logger.info(f"⊘ Такой пост уже выходил («{twin[:45]}»): "
                        f"{final_text.split(chr(10))[0][:50]}")
            if count_stats:
                await stats.record_skipped('duplicate', source)
            return 'skipped_dup'
        news['_final_text'] = final_text     # запомним после публикации
    return None


async def send_news(bot: Bot, news: dict, chat_id=None) -> str:
    """Отправляет один пост. Возвращает строковый код результата:
    - 'sent' — успешно отправлено
    - 'skipped_filter' — отфильтровано (keywords)
    - 'skipped_dup' — уже было в истории (дубль)
    - 'failed' — реальная ошибка отправки или fail-фильтр (нет картинки и т.п.)
    """
    source = news.get('source', 'unknown')
    is_channel = chat_id is None  # без chat_id = идём в канал, метрики считаем

    if not matches_keywords(news):
        return 'skipped_filter'
    # Fuzzy-дедуп: та же новость с другого источника с иной формулировкой
    if sent_links.has_similar_title(news.get('title', '')):
        logger.info(f"⊘ Похожая новость уже публиковалась: {news.get('title', '')[:60]}")
        await stats.record_skipped('duplicate', news.get('source', 'unknown'))
        return 'skipped_dup'
    if not await sent_links.claim(news['link'], news.get('title', '')):
        if is_channel:
            await stats.record_skipped('duplicate', source)
        return 'skipped_dup'

    target = chat_id or CHANNEL_ID

    skip = await _prepare_news_for_send(news, source, count_stats=is_channel)
    if skip:
        return skip

    video_file = await _prepare_video_file(news)

    try:
        ok = await _send_post(bot, news, target, video_file)
        if ok:
            _commit_image_fingerprint(news)
            _mark_published()
            if is_channel:
                await stats.record_published(source)
            return 'sent'
        # Не отправилось — снимаем claim, чтобы можно было попробовать снова
        await sent_links.release(news['link'], news.get('title', ''))
        if is_channel:
            await stats.record_failed_send(source)
        return 'failed'
    finally:
        if video_file:
            try:
                video_file.unlink(missing_ok=True)
            except Exception:
                pass


CUSTOM_SOURCES_FILE = DATA_DIR / 'custom_sources.json'


class CustomSources:
    """Динамические источники, добавляемые командами через чат (/addsource).
    Типы: 'rss' (любая RSS/Atom-лента) и 'tg' (публичный Telegram-канал через t.me/s/).
    Хранятся на диске и подключаются к SOURCES при старте и при добавлении."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []   # {'type': 'rss'|'tg', 'value': str, 'label': str}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._items = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            logger.warning(f"custom_sources не загружен: {e}")

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._items, ensure_ascii=False), encoding='utf-8')
        except OSError as e:
            logger.error(f"custom_sources не сохранён: {e}")

    def all(self) -> list[dict]:
        return list(self._items)

    def add(self, src_type: str, value: str, label: str) -> bool:
        if any(it['label'].lower() == label.lower() for it in self._items):
            return False
        self._items.append({'type': src_type, 'value': value, 'label': label})
        self._save()
        return True

    def remove(self, label: str):
        for it in self._items:
            if it['label'].lower() == label.lower():
                self._items.remove(it)
                self._save()
                return it
        return None


custom_sources: Optional['CustomSources'] = None


def _make_source_fn(src_type: str, value: str, label: str):
    """Фабрика функции-источника для динамических записей."""
    if src_type == 'tg':
        return lambda: get_telegram_channel(value, label)
    return lambda: _parse_rss_with_fallback(value, label)


def _attach_custom_source(item: dict) -> None:
    """Подключает динамический источник в общий список SOURCES (если ещё нет)."""
    if any(name == item['label'] for name, _ in SOURCES):
        return
    SOURCES.append((item['label'], _make_source_fn(item['type'], item['value'], item['label'])))


def _parse_addsource_args(args: list) :
    """Разбирает аргументы /addsource → (type, value, label) или None.
    Форматы:
      /addsource https://site.com/feed/ [Название]
      /addsource @channel  |  /addsource t.me/channel [Название]"""
    if not args:
        return None
    first = args[0].strip()
    rest_label = ' '.join(args[1:]).strip()
    m = re.match(r'^@([A-Za-z0-9_]{4,})$', first)
    if not m:
        m = re.match(r'^(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{4,})/?$', first)
    if m:
        ch = m.group(1)
        label = rest_label or f'TG: {ch}'
        if not label.lower().startswith('tg'):
            label = f'TG: {label}'
        return 'tg', ch, label
    if first.startswith(('http://', 'https://')):
        try:
            host = urlparse(first).netloc.replace('www.', '')
        except Exception:
            host = 'RSS'
        label = rest_label or host or 'RSS'
        return 'rss', first, label
    return None


# ============== ОТЛОЖЕННАЯ ПУБЛИКАЦИЯ ==============
# Bot API не умеет нативную отложку Telegram (параметра schedule_date нет),
# поэтому планировщик свой: бот хранит посты на диске и публикует их сам.
# Время считаем через UTC явно — не зависим от часового пояса сервера.

def _tz_offset() -> int:
    """Часовой пояс админа относительно UTC (по умолчанию МСК = +3)."""
    return getattr(settings, 'tz_offset', 3) if settings is not None else 3


def _local_now() -> datetime:
    """Текущее время в часовом поясе админа (naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=_tz_offset())


def _local_to_utc(local_naive: datetime) -> datetime:
    """Локальное время админа (naive) → aware UTC."""
    return (local_naive - timedelta(hours=_tz_offset())).replace(tzinfo=timezone.utc)


def _utc_to_local(dt_utc: datetime) -> datetime:
    """Aware/naive UTC → локальное время админа (naive)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=_tz_offset())


def _fmt_local(dt_utc: datetime) -> str:
    """Человекочитаемое время публикации в поясе админа."""
    return _utc_to_local(dt_utc).strftime('%d.%m в %H:%M')


_REL_TIME_RE = re.compile(
    r'^\+\s*(\d{1,4})\s*(мин\w*|м|min|m|час\w*|ч|h|дн\w*|д|d)$', re.IGNORECASE)
_DAY_WORD_RE = re.compile(r'^(сегодня|завтра|послезавтра)\s+(.+)$', re.IGNORECASE)
_DATE_TIME_RE = re.compile(
    r'^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s+(\d{1,2})[:.](\d{2})$')
_TIME_RE = re.compile(r'^(\d{1,2})[:.](\d{2})$')


def _parse_schedule_time(text: str) -> Optional[datetime]:
    """Разбирает время публикации, введённое админом вручную.
    Возвращает aware UTC datetime в будущем или None если формат не понят
    либо время уже прошло.

    Понимает: '18:30', '12.07 18:30', '12.07.2026 18:30',
              'завтра 10:00', '+2ч', '+30м', '+1д'."""
    if not text:
        return None
    t = re.sub(r'\s+', ' ', text.strip().lower().replace(',', ' ')).strip()
    now_local = _local_now()

    # Относительное смещение: +2ч / +30м / +1д
    m = _REL_TIME_RE.match(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith(('м', 'm')):
            delta = timedelta(minutes=n)
        elif unit.startswith(('ч', 'h')):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        if delta.total_seconds() < 60:
            return None
        return _local_to_utc(now_local + delta)

    # Словесный сдвиг дня: 'завтра 10:00'
    day_shift = 0
    m = _DAY_WORD_RE.match(t)
    if m:
        day_shift = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2}[m.group(1).lower()]
        t = m.group(2).strip()

    # Дата со временем: 12.07 18:30 / 12.07.2026 18:30
    m = _DATE_TIME_RE.match(t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year_raw, hh, mi = m.group(3), int(m.group(4)), int(m.group(5))
        if not (0 <= hh <= 23 and 0 <= mi <= 59):
            return None
        year = now_local.year
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        try:
            local = datetime(year, month, day, hh, mi)
        except ValueError:
            return None
        # Без года и дата уже прошла — значит имелся в виду следующий год
        if not year_raw and local <= now_local:
            try:
                local = local.replace(year=year + 1)
            except ValueError:
                return None
        return _local_to_utc(local) if local > now_local else None

    # Просто время: 18:30
    m = _TIME_RE.match(t)
    if m:
        hh, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mi <= 59):
            return None
        local = now_local.replace(hour=hh, minute=mi, second=0, microsecond=0)
        if day_shift:
            local += timedelta(days=day_shift)
        elif local <= now_local:
            local += timedelta(days=1)   # время на сегодня прошло — значит завтра
        return _local_to_utc(local) if local > now_local else None

    return None


SCHEDULED_POSTS_FILE = DATA_DIR / 'scheduled_posts.json'


class ScheduledPosts:
    """Посты, отложенные админом на конкретное время. Публикует сам бот.
    Хранится на диске — отложка переживает перезапуски."""
    MAX_ITEMS = 500
    MAX_TRIES = 3

    def __init__(self, path: Path):
        self.path = path
        self._counter = 0
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                self._counter = int(data.get('counter', 0))
                self._items = data.get('items', {})
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"scheduled_posts не загружен: {e}")

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({'counter': self._counter, 'items': self._items}, ensure_ascii=False),
                encoding='utf-8')
        except OSError as e:
            logger.error(f"scheduled_posts не сохранён: {e}")

    @staticmethod
    def _at(item: dict) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(item['at'])
        except (KeyError, ValueError, TypeError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def add(self, news: dict, when_utc: datetime, by: Optional[dict] = None) -> str:
        """by — кто отложил: {'id': int, 'name': str}. Нужен для внятных уведомлений."""
        self._counter += 1
        key = str(self._counter)
        clean = {k: v for k, v in news.items() if k != 'published_parsed'}
        self._items[key] = {
            'news': clean,
            'at': when_utc.astimezone(timezone.utc).isoformat(),
            'tries': 0,
            'by': by,
            'created': datetime.now(timezone.utc).isoformat(),
        }
        if len(self._items) > self.MAX_ITEMS:
            oldest = sorted(self._items, key=lambda k: self._items[k].get('at', ''))
            for k in oldest[:len(self._items) - self.MAX_ITEMS]:
                del self._items[k]
        self._save()
        return key

    def all(self) -> list:
        """Список (key, news, when_utc), отсортированный по времени публикации."""
        out = []
        for k, item in self._items.items():
            dt = self._at(item)
            if dt:
                out.append((k, item.get('news', {}), dt))
        out.sort(key=lambda x: x[2])
        return out

    def due(self, now_utc: Optional[datetime] = None) -> list:
        """Посты, время которых уже наступило."""
        now = now_utc or datetime.now(timezone.utc)
        return [(k, news) for k, news, dt in self.all() if dt <= now]

    def get(self, key: str) -> Optional[dict]:
        item = self._items.get(key)
        return item.get('news') if item else None

    def when(self, key: str) -> Optional[datetime]:
        item = self._items.get(key)
        return self._at(item) if item else None

    def meta(self, key: str) -> dict:
        """Служебные данные поста: кто отложил, на какое время, сколько попыток.
        Вызывать ДО pop — после удаления записи их уже не будет."""
        item = self._items.get(key) or {}
        return {
            'by': item.get('by') or {},
            'at': self._at(item),
            'tries': int(item.get('tries', 0)),
        }

    def reschedule(self, key: str, when_utc: datetime) -> bool:
        """Меняет время публикации, не теряя пост и историю попыток."""
        item = self._items.get(key)
        if not item:
            return False
        item['at'] = when_utc.astimezone(timezone.utc).isoformat()
        item['tries'] = 0          # новое время — новые попытки
        self._save()
        return True

    def clear(self) -> int:
        """Снимает всю отложку. Возвращает, сколько постов убрали."""
        count = len(self._items)
        self._items.clear()
        self._save()
        return count

    def pop(self, key: str) -> Optional[dict]:
        item = self._items.pop(key, None)
        if item is not None:
            self._save()
            return item.get('news')
        return None

    def mark_try(self, key: str) -> int:
        """Считает неудачные попытки публикации. Возвращает их количество."""
        item = self._items.get(key)
        if not item:
            return 0
        item['tries'] = int(item.get('tries', 0)) + 1
        self._save()
        return item['tries']


scheduled_posts: Optional['ScheduledPosts'] = None


# ============== ЗДОРОВЬЕ ИСТОЧНИКОВ И ОТПЕЧАТКИ КАРТИНОК ==============

PUBLISHED_TEXTS_FILE = DATA_DIR / 'published_texts.json'
PUBLISHED_TEXT_HOURS = 48
PUBLISHED_TEXT_MAX = 300
FINAL_SIMILARITY = 0.55        # доля общих слов, при которой считаем дублем


class PublishedTexts:
    """Тексты уже опубликованных постов — в том виде, в каком они ушли.

    Все прочие дедупы работают ДО перевода, на исходных заголовках. Но одна
    новость с двух сайтов приходит разными формулировками на английском
    («Anime of FX Fighter Kurumi-chan Manga Premieres in October» и «FX Senshi
    Kurumi-chan Anime Reveals October Premiere»), и совпадать они начинают
    только после перевода. Эта проверка — последняя, уже на готовом тексте."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    self._items = data
        except (OSError, ValueError) as e:
            logger.warning(f"published_texts не загружен: {e}")
        self._prune()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._items, ensure_ascii=False),
                                 encoding='utf-8')
        except OSError as e:
            logger.error(f"published_texts не сохранён: {e}")

    def _prune(self) -> None:
        edge = time.time() - PUBLISHED_TEXT_HOURS * 3600
        self._items = [i for i in self._items
                       if isinstance(i.get('ts'), (int, float)) and i['ts'] > edge]
        if len(self._items) > PUBLISHED_TEXT_MAX:
            self._items = self._items[-PUBLISHED_TEXT_MAX:]

    # Кириллица → латиница: названия тайтлов приходят в обоих написаниях
    # («Куруми» и «Kurumi»), и без приведения к одному алфавиту один и тот же
    # тайтл выглядит как два разных.
    _TRANSLIT = str.maketrans({
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'j', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'c', 'ш': 's', 'щ': 's', 'ъ': '',
        'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'u', 'я': 'a',
    })

    @classmethod
    def _words(cls, text: str) -> set:
        """Значимые слова первой строки, приведённые к латинице."""
        head = (text or '').split('\n')[0].lower()
        raw = re.findall(r'[а-яёa-z0-9]{3,}', head)
        stop = {'аниме', 'манга', 'манге', 'манги', 'the', 'and', 'for', 'уже',
                'выйдет', 'вышел', 'вышла', 'состоится', 'получит', 'anime',
                'manga', 'премьера', 'этого', 'года'}
        words = set()
        for word in raw:
            if word in stop:
                continue
            latin = word.translate(cls._TRANSLIT)
            if len(latin) >= 3:
                words.add(latin[:6])
        return words

    def find_similar(self, text: str) -> Optional[str]:
        """Заголовок недавнего поста, который говорит о том же. Или None."""
        words = self._words(text)
        if len(words) < 3:                 # слишком коротко, чтобы судить
            return None
        self._prune()
        for item in reversed(self._items):
            old = set(item.get('w') or [])
            if not old:
                continue
            overlap = len(words & old) / min(len(words), len(old))
            if overlap >= FINAL_SIMILARITY:
                return item.get('t', '')
        return None

    def add(self, text: str) -> None:
        words = self._words(text)
        if len(words) < 3:
            return
        self._items.append({
            'w': sorted(words),
            't': re.sub(r'\s+', ' ', (text or '').split('\n')[0])[:70],
            'ts': time.time(),
        })
        self._prune()
        self._save()

    def __len__(self) -> int:
        return len(self._items)


published_texts: Optional['PublishedTexts'] = None


SUBJECT_MEMORY_FILE = DATA_DIR / 'recent_subjects.json'
SUBJECT_MEMORY_HOURS = 36       # столько помним, о чём уже писали
SUBJECT_MAX_PER_DAY = 3         # больше постов про один тайтл за сутки — перебор


class RecentSubjects:
    """О чём бот уже писал: предмет новости + её тип.

    Нужно для двух вещей. Во-первых, одну новость подхватывают сразу несколько
    сайтов, и текстовый дедуп её не ловит — заголовки-то разные. После обработки
    моделью у них совпадает subject, и повтор становится виден. Во-вторых, лента
    из пяти постов про один тайтл подряд выглядит зациклённой."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    self._items = data
        except (OSError, ValueError) as e:
            logger.warning(f"recent_subjects не загружен: {e}")
        self._prune()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._items, ensure_ascii=False),
                                 encoding='utf-8')
        except OSError as e:
            logger.error(f"recent_subjects не сохранён: {e}")

    def _prune(self) -> None:
        edge = datetime.now(timezone.utc) - timedelta(hours=SUBJECT_MEMORY_HOURS)
        kept = []
        for item in self._items:
            try:
                if datetime.fromisoformat(item['at']) > edge:
                    kept.append(item)
            except (KeyError, ValueError, TypeError):
                continue
        self._items = kept

    @staticmethod
    def _key(subject: str) -> str:
        """Ключ сравнения: без регистра, кавычек и служебных слов."""
        text = re.sub(r'[«»"\'`:,.\-–—]', ' ', (subject or '').lower())
        text = re.sub(r'\b(?:season|сезон|часть|part|the|аниме|anime)\b', ' ', text)
        return ' '.join(text.split())

    def seen_same_news(self, subject: str, kind: str) -> bool:
        """Писали ли уже об этом же событии (тот же тайтл и тот же тип)."""
        key = self._key(subject)
        if not key:
            return False
        return any(it.get('key') == key and it.get('kind') == kind
                   for it in self._items)

    def count_today(self, subject: str) -> int:
        """Сколько постов про этот тайтл за последние сутки."""
        key = self._key(subject)
        if not key:
            return 0
        edge = datetime.now(timezone.utc) - timedelta(hours=24)
        total = 0
        for it in self._items:
            if it.get('key') != key:
                continue
            try:
                if datetime.fromisoformat(it['at']) > edge:
                    total += 1
            except (KeyError, ValueError, TypeError):
                continue
        return total

    def add(self, subject: str, kind: str, title: str = '') -> None:
        key = self._key(subject)
        if not key:
            return
        self._items.append({
            'key': key, 'kind': kind,
            'title': re.sub(r'\s+', ' ', title or '').strip()[:70],
            'at': datetime.now(timezone.utc).isoformat(),
        })
        self._prune()
        self._save()

    def __len__(self) -> int:
        return len(self._items)


recent_subjects: Optional['RecentSubjects'] = None


SOURCE_HEALTH_FILE = DATA_DIR / 'source_health.json'
AUTO_DISABLE_AFTER_HOURS = 24   # столько часов без единой новости → пауза
AUTO_DISABLE_MIN_CHECKS = 3     # но не раньше, чем после стольких проверок


class SourceHealth:
    """Состояние источников: сколько проверок подряд прошло без новостей или
    с ошибкой. Нужен, чтобы автоматически ставить на паузу умершие источники
    (403 от анти-бота и т.п.) и показывать картину в /health.
    Хранится на диске — счётчик переживает передеплой."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    self._data = loaded
        except (OSError, ValueError) as e:
            logger.warning(f"source_health не загружен: {e}")

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, ensure_ascii=False),
                                 encoding='utf-8')
        except OSError as e:
            logger.error(f"source_health не сохранён: {e}")

    def _entry(self, name: str) -> dict:
        return self._data.setdefault(
            name, {'fails': 0, 'last_ok': None, 'last_count': 0, 'last_error': '',
                   'silent_since': None})

    def record_ok(self, name: str, count: int) -> None:
        entry = self._entry(name)
        entry['fails'] = 0
        entry['silent_since'] = None
        entry['last_ok'] = datetime.now(timezone.utc).isoformat()
        entry['last_count'] = int(count)
        entry['last_error'] = ''
        self._save()

    def record_fail(self, name: str, reason: str) -> int:
        """Отмечает неудачу. Возвращает число неудач подряд."""
        entry = self._entry(name)
        entry['fails'] = int(entry.get('fails', 0)) + 1
        entry['last_error'] = str(reason)[:200]
        if not entry.get('silent_since'):
            # Засекаем момент, с которого источник замолчал: считать паузу
            # по времени надёжнее, чем по числу проверок — ночью или в выходной
            # живой источник тоже может ничего не отдать.
            entry['silent_since'] = datetime.now(timezone.utc).isoformat()
        self._save()
        return entry['fails']

    def silent_hours(self, name: str) -> Optional[float]:
        """Сколько часов источник не отдаёт новостей. None — если всё хорошо."""
        entry = self._data.get(name)
        if not entry:
            return None
        started = entry.get('silent_since') or entry.get('last_ok')
        if not started or not entry.get('fails'):
            return None
        try:
            since = datetime.fromisoformat(started)
        except (ValueError, TypeError):
            return None
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - since).total_seconds() / 3600

    def reset(self, name: str) -> None:
        """Сброс — например, когда источник включили вручную."""
        if name in self._data:
            self._data[name]['fails'] = 0
            self._data[name]['silent_since'] = None
            self._save()

    def info(self, name: str) -> dict:
        return dict(self._data.get(name, {}))

    def all(self) -> dict:
        return {k: dict(v) for k, v in self._data.items()}


source_health: Optional['SourceHealth'] = None
# Источники, которые бот выключил сам — check_news заберёт отсюда и уведомит
_auto_disabled_pending: list[tuple[str, str]] = []


IMAGE_HASHES_FILE = DATA_DIR / 'image_hashes.json'
IMAGE_HASH_MAX = 500            # сколько последних отпечатков помним
IMAGE_HASH_DISTANCE = 5         # расстояние Хэмминга, при котором картинки «те же»


def _image_fingerprint(data: Optional[bytes]) -> Optional[str]:
    """Отпечаток картинки.

    С Pillow считаем перцептивный dHash: он переживает пережатие, смену размера
    и лёгкий кроп — то, что происходит с одним и тем же кадром на разных сайтах.
    Без Pillow откатываемся на md5 — тогда ловим только точные копии файла."""
    if not data:
        return None
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as im:
                # LANCZOS усредняет по площади: отпечаток почти не меняется при
                # смене размера, чего не даёт быстрый bicubic по умолчанию
                small = im.convert('L').resize((9, 8), Image.LANCZOS)
                px = list(small.getdata())
            bits = 0
            pos = 0
            for row in range(8):
                base = row * 9
                for col in range(8):
                    if px[base + col] > px[base + col + 1]:
                        bits |= (1 << pos)
                    pos += 1
            return f'd:{bits:016x}'
        except Exception as e:
            logger.debug(f"dHash не посчитан ({e}) — откат на md5")
    return 'm:' + hashlib.md5(data).hexdigest()


def _hash_distance(a: str, b: str) -> Optional[int]:
    """Расстояние между отпечатками. None — если типы разные (несравнимы)."""
    if not a or not b or a[:2] != b[:2]:
        return None
    if a.startswith('m:'):
        return 0 if a == b else 64
    try:
        return bin(int(a[2:], 16) ^ int(b[2:], 16)).count('1')
    except ValueError:
        return None


class ImageHashes:
    """Отпечатки картинок опубликованных постов. Ловит один и тот же анонс,
    пришедший с разных сайтов с разными заголовками (fuzzy-дедуп по тексту
    такие случаи пропускает)."""

    def __init__(self, path: Path, max_items: int = IMAGE_HASH_MAX):
        self.path = path
        self.max_items = max_items
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded, list):
                    self._items = [x for x in loaded if isinstance(x, dict) and x.get('h')]
        except (OSError, ValueError) as e:
            logger.warning(f"image_hashes не загружен: {e}")

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._items, ensure_ascii=False),
                                 encoding='utf-8')
        except OSError as e:
            logger.error(f"image_hashes не сохранён: {e}")

    def find_duplicate(self, fingerprint: str) -> Optional[dict]:
        """Ищет ранее опубликованную картинку, похожую на эту."""
        if not fingerprint:
            return None
        for item in reversed(self._items):        # свежие сначала
            dist = _hash_distance(fingerprint, item.get('h', ''))
            if dist is not None and dist <= IMAGE_HASH_DISTANCE:
                return dict(item, distance=dist)
        return None

    def add(self, fingerprint: str, title: str = '') -> None:
        if not fingerprint:
            return
        self._items.append({
            'h': fingerprint,
            't': re.sub(r'\s+', ' ', title or '').strip()[:80],
            'at': datetime.now(timezone.utc).isoformat(),
        })
        if len(self._items) > self.max_items:
            self._items = self._items[-self.max_items:]
        self._save()

    def __len__(self) -> int:
        return len(self._items)


image_hashes: Optional['ImageHashes'] = None


MIN_GOOD_IMAGE_PX = 500         # ниже этой ширины кадр выглядит мыльным


IMAGE_BYTES_CACHE_MAX = 40      # столько скачанных картинок держим в памяти
_image_bytes_cache: dict = {}


def _cached_image_bytes(url: str) -> Optional[bytes]:
    """Скачивает картинку с кэшем на время цикла.

    Один и тот же файл нужен несколько раз: посчитать отпечаток для дедупа,
    измерить размер превью, отправить байтами при отказе Bot API. Раньше каждый
    шаг качал заново — лишний трафик и задержка на ровном месте."""
    if not url:
        return None
    if url in _image_bytes_cache:
        return _image_bytes_cache[url]
    data = _download_image_bytes(url)
    if len(_image_bytes_cache) >= IMAGE_BYTES_CACHE_MAX:
        _image_bytes_cache.clear()
    _image_bytes_cache[url] = data
    return data


def _image_width(data: Optional[bytes]) -> Optional[int]:
    """Ширина картинки в пикселях (нужен Pillow). None — если не определить."""
    if not data or Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.width
    except Exception:
        return None


async def _improve_thumb(news: dict) -> None:
    """Если единственная картинка — мыльный кадр «ленивого» видео, пробуем взять
    вариант покрупнее со страницы поста (og:image). Меняем только если он реально
    больше: пост без картинки при включённом require_image вообще не выйдет,
    так что мыло всё равно лучше пустоты."""
    if not news.pop('_thumb_only', False):
        return
    images = news.get('images') or []
    if not images or Image is None:
        return
    current = await asyncio.to_thread(_cached_image_bytes, images[0])
    width = _image_width(current)
    if width is not None and width >= MIN_GOOD_IMAGE_PX:
        return
    # width is None — картинку не удалось измерить. Раньше на этом сдавались,
    # хотя это как раз повод поискать замену.
    link = news.get('link')
    if not link:
        return
    og = await asyncio.to_thread(fetch_og_image, link)
    og_norm = _normalize_image_url(og, link) if og else None
    if not og_norm or og_norm == images[0]:
        logger.debug(f"Превью {width}px, замены не нашлось: {news.get('title', '')[:50]}")
        return
    candidate = await asyncio.to_thread(_cached_image_bytes, og_norm)
    cand_width = _image_width(candidate)
    if cand_width and (width is None or cand_width > width):
        news['images'] = [og_norm] + [i for i in images[1:] if i != og_norm]
        logger.info(f"🖼 Превью видео заменено на крупное: "
                    f"{width or '?'}px → {cand_width}px")
    else:
        logger.debug(f"Превью {width}px осталось: og:image не крупнее")


async def _image_duplicate(news: dict) -> Optional[str]:
    """Если картинка поста уже публиковалась — возвращает заголовок той новости.
    Иначе запоминает отпечаток и возвращает None.

    Скачивается только первая картинка: этого достаточно, чтобы узнать кадр,
    и не хочется тратить трафик на весь альбом."""
    if settings is None or not getattr(settings, 'image_dedup', True):
        return None
    if image_hashes is None:
        return None
    images = news.get('images') or []
    if not images:
        return None
    data = await asyncio.to_thread(_cached_image_bytes, images[0])
    fingerprint = _image_fingerprint(data)
    if not fingerprint:
        return None
    dup = image_hashes.find_duplicate(fingerprint)
    if dup:
        return dup.get('t') or 'без заголовка'
    # Запоминаем не сейчас, а после успешной публикации (_commit_image_fingerprint):
    # иначе сорвавшаяся отправка «застолбила» бы кадр, и та же новость с другого
    # источника больше никогда бы не вышла.
    news['_img_fp'] = fingerprint          # строка — безопасно для JSON-хранилищ
    return None


def _commit_image_fingerprint(news: dict) -> None:
    """Фиксирует отпечаток картинки и предмет новости после того, как пост
    реально ушёл. До отправки не запоминаем: сорвавшаяся публикация не должна
    закрывать дорогу той же новости из другого источника."""
    fingerprint = news.pop('_img_fp', None)
    if fingerprint and image_hashes is not None:
        image_hashes.add(fingerprint, news.get('title', ''))
    subject = news.get('_llm_subject')
    if subject and recent_subjects is not None:
        recent_subjects.add(subject, news.get('_llm_kind', 'новость'),
                            news.get('title', ''))
    final_text = news.pop('_final_text', None)
    if final_text and published_texts is not None:
        published_texts.add(final_text)


PENDING_POSTS_FILE = DATA_DIR / 'pending_posts.json'


class PendingPosts:
    """Посты, отправленные в ветку и ждущие решения админа (кнопки под постом).
    Хранится на диске: кнопки работают и после перезапуска бота."""
    MAX_ITEMS = 300
    TTL_DAYS = 7

    def __init__(self, path: Path):
        self.path = path
        self._counter = 0
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                self._counter = int(data.get('counter', 0))
                self._items = data.get('items', {})
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"pending_posts не загружен: {e}")

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({'counter': self._counter, 'items': self._items},
                           ensure_ascii=False),
                encoding='utf-8')
        except OSError as e:
            logger.error(f"pending_posts не сохранён: {e}")

    def _cleanup(self) -> None:
        cutoff = time.time() - self.TTL_DAYS * 86400
        self._items = {k: v for k, v in self._items.items() if v.get('ts', 0) >= cutoff}
        if len(self._items) > self.MAX_ITEMS:
            oldest = sorted(self._items, key=lambda k: self._items[k].get('ts', 0))
            for k in oldest[:len(self._items) - self.MAX_ITEMS]:
                del self._items[k]

    def add(self, news: dict) -> str:
        """Сохраняет пост, возвращает короткий ключ для callback-кнопок."""
        self._counter += 1
        key = str(self._counter)
        clean = {k: v for k, v in news.items() if k != 'published_parsed'}
        self._items[key] = {'news': clean, 'ts': time.time()}
        self._cleanup()
        self._save()
        return key

    def get(self, key: str) -> Optional[dict]:
        item = self._items.get(key)
        return item.get('news') if item else None

    def pop(self, key: str) -> Optional[dict]:
        item = self._items.pop(key, None)
        if item is not None:
            self._save()
            return item.get('news')
        return None

    def update_news(self, key: str, news: dict) -> bool:
        """Заменяет сохранённый пост (используется при ручном редактировании текста)."""
        item = self._items.get(key)
        if not item:
            return False
        item['news'] = {k: v for k, v in news.items() if k != 'published_parsed'}
        self._save()
        return True

    def set_preview(self, key: str, chat_id: int, message_id: int) -> None:
        """Запоминает сообщение-превью в ветке, чтобы обновлять его при правках."""
        item = self._items.get(key)
        if item:
            item['preview'] = {'chat_id': chat_id, 'message_id': message_id}
            self._save()

    def get_preview(self, key: str) -> Optional[dict]:
        item = self._items.get(key)
        return item.get('preview') if item else None


pending_posts: Optional['PendingPosts'] = None


def _moderation_markup(key: str) -> InlineKeyboardMarkup:
    """Кнопки модерации под постом в ветке."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📢 В канал', callback_data=f'pub:{key}'),
         InlineKeyboardButton('📅 В отложку', callback_data=f'sch:{key}')],
        [InlineKeyboardButton('✏️ Изменить', callback_data=f'edit:{key}'),
         InlineKeyboardButton('✖ Скрыть', callback_data=f'dis:{key}')],
    ])


async def _tg_call_flood_safe(coro_factory):
    """Вызывает Telegram-метод; при флуд-лимите (RetryAfter) честно ждёт
    указанное Telegram время и повторяет один раз вместо провала отправки."""
    try:
        return await coro_factory()
    except RetryAfter as e:
        wait = int(getattr(e, 'retry_after', 5)) + 1
        logger.warning(f"Flood-лимит Telegram: жду {wait}с и повторяю отправку")
        await asyncio.sleep(wait)
        return await coro_factory()


def _download_media_bytes(url: str, max_mb: int = TG_VIDEO_MAX_MB) -> Optional[bytes]:
    """Скачивает медиа (фото/видео) для отправки байтами, когда Bot API не берёт URL.

    Качаем ПОТОКОМ и обрываем, как только превышен лимит: пятиминутное видео может
    весить сотни мегабайт, а хостинг у нас на 1 ГБ RAM — читать такое целиком в
    память нельзя. Дополнительно смотрим Content-Length, чтобы не начинать зря."""
    limit = max_mb * 1024 * 1024
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT},
                         timeout=HTTP_TIMEOUT, stream=True)
        with r:
            if r.status_code != 200:
                logger.info(f"Медиа не скачалось: HTTP {r.status_code} — {url[:70]}")
                return None
            ctype = (r.headers.get('Content-Type') or '').lower()
            if not (ctype.startswith('image/') or ctype.startswith('video/')):
                logger.info(f"Медиа не скачалось: тип «{ctype or '?'}» вместо файла "
                            f"— {url[:70]}")
                return None
            declared = r.headers.get('Content-Length')
            if declared and declared.isdigit() and int(declared) > limit:
                logger.info(f"Медиа {int(declared) // (1024 * 1024)} МБ — больше лимита "
                            f"{max_mb} МБ, не качаю: {url[:60]}")
                return None
            chunks = []
            total = 0
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    logger.info(f"Медиа превысило {max_mb} МБ на лету — обрываю: {url[:60]}")
                    return None
                chunks.append(chunk)
            return b''.join(chunks) if total else None
    except Exception as e:
        logger.info(f"Медиа не скачалось ({type(e).__name__}: {e}) — {url[:70]}")
        return None


async def _resolve_video(url: Optional[str]):
    """Готовит видео к отправке. cdn-telegram/telesco Bot API по URL не принимает
    (та же болезнь, что у фото) — качаем сами и шлём байтами (до TG_VIDEO_MAX_MB).
    Возвращает: bytes | url | None (недоступно — пост пойдёт без видео)."""
    if not url:
        return None
    if not _download_needed_host(url):
        return url
    data = await asyncio.to_thread(_download_media_bytes, url, TG_VIDEO_MAX_MB)
    if data:
        logger.info(f"🎬 Видео скачано байтами ({len(data) // (1024 * 1024)} МБ): {url[:60]}")
        return data
    logger.warning(f"🎬 Видео недоступно для скачивания — пост пойдёт без него: {url[:80]}")
    return None


async def _resolve_photos_for_album(photos: list[str]) -> list:
    """Готовит список картинок к отправке альбомом. Каждую, что Telegram не сможет
    забрать по URL (cdn-telegram.org и пр.), заменяем скачанными байтами.
    Возвращает список пригодных к отправке значений (URL-строки или bytes)."""
    resolved: list = []
    for ph in photos[:MAX_PHOTOS_PER_POST]:
        if _download_needed_host(ph):
            data = await asyncio.to_thread(_cached_image_bytes, ph)
            resolved.append(data if data else ph)
        else:
            resolved.append(ph)
    return resolved


def _download_needed_host(url: str) -> bool:
    """Хосты, с которых Bot API обычно не может скачать картинку по URL —
    их качаем сами заранее."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return 'cdn-telegram.org' in host or 'telesco.pe' in host


async def _send_post_thread_split(bot: Bot, news: dict, video_file: Optional[Path]) -> bool:
    """Отправка поста в ветку ЦЕЛЬНЫМ сообщением: медиа + текст в подписи (caption).

    Посты короткие (заголовок + предложение + дата), всегда влезают в caption-лимит
    1024. Поэтому фото/видео и текст идут ОДНИМ сообщением, а не двумя.
    Все картинки источника сохраняются (альбомом), а не только первая.
    Если текст внезапно длиннее 1024 — откат на старый режим (медиа + отдельный текст)."""
    thread_kw = {'message_thread_id': DISCUSSION_THREAD_ID}
    target = DISCUSSION_CHAT_ID

    text = format_news_text_long(news)
    video_url = news.get('video')
    # Что реально отправим как видео: файл | bytes | url. cdn-telegram качаем сами.
    video_media = None
    if settings.video_enabled and video_file is None and video_url \
            and _is_direct_video(video_url):
        video_media = await _resolve_video(video_url)
    has_inline_video = settings.video_enabled and (
        video_file is not None or video_media is not None
    )
    if video_url and not has_inline_video:
        text = _add_video_link_to_text(text, video_url)

    photos = _dedup_image_variants(news.get('images') or [])
    if not photos and video_media is None and news.get('_video_thumb'):
        photos = [news['_video_thumb']]
        news['_thumb_only'] = True
        logger.info(f"🎬 Видео не доехало — ставлю кадр из поста: "
                    f"{news.get('title', '')[:50]}")
    media_count = len(photos) + (1 if has_inline_video else 0)

    # Нет медиа — пробуем og:image со страницы, иначе (при require_image) пропуск
    if media_count == 0 and news.get('link'):
        og = await asyncio.to_thread(fetch_og_image, news['link'])
        og_norm = _normalize_image_url(og, news['link']) if og else None
        if og_norm:
            photos = [og_norm]
            media_count = 1
            logger.info(f"Картинка взята со страницы (og:image): {news['title'][:50]}")

    if settings.require_image and media_count == 0:
        logger.info(f"⊘ Пропускаю пост без медиа (require_image): {news['title'][:60]}")
        return False

    # Caption для медиа. Telegram-лимит подписи — 1024 символа.
    caption = fit_to_limit(html.escape(text), TG_CAPTION_LIMIT)
    caption_kw = {'caption': caption, 'parse_mode': ParseMode.HTML}

    # Кнопки модерации: 📢 В канал / 📅 В отложку / ✏️ Изменить / ✖ Скрыть
    reply_markup = None
    pending_key = None
    if pending_posts is not None:
        pending_key = pending_posts.add(news)
        reply_markup = _moderation_markup(pending_key)

    # Если медиа нет вовсе (require_image выключен) — просто текст
    if media_count == 0:
        try:
            msg = await _tg_call_flood_safe(lambda: bot.send_message(
                chat_id=target, text=caption, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=reply_markup, **thread_kw))
            _remember_preview(pending_key, msg)
            logger.info(f"🧵 {news['source']}: {news['title'][:60]} (только текст)")
            return True
        except TelegramError as e:
            logger.error(f"Текст в ветку не отправился: {e}")
            return False

    # Текст не влез в caption — откатываемся на раздельную отправку
    if len(text) > TG_CAPTION_LIMIT:
        return await _send_thread_media_then_text(
            bot, news, photos, has_inline_video, video_file, video_media,
            text, reply_markup, thread_kw, target)

    # === ЦЕЛЬНЫЙ РЕЖИМ: одно сообщение с медиа и подписью ===

    # 1) Одно фото + (нет видео): фото с подписью и кнопками — идеально цельно
    if len(photos) == 1 and not has_inline_video:
        if await _send_single_photo_caption(
                bot, target, photos[0], caption_kw, reply_markup, thread_kw, news,
                pending_key):
            return True
        # не удалось даже байтами — на всякий случай откат
        return await _send_thread_media_then_text(
            bot, news, photos, has_inline_video, video_file, video_media,
            text, reply_markup, thread_kw, target)

    # 2) Только видео (без фото): видео с подписью и кнопками
    if has_inline_video and not photos:
        try:
            if video_file:
                with open(video_file, 'rb') as f:
                    msg = await bot.send_video(chat_id=target, video=f, supports_streaming=True,
                                               reply_markup=reply_markup, **caption_kw, **thread_kw)
            else:
                msg = await bot.send_video(chat_id=target, video=video_media, supports_streaming=True,
                                           reply_markup=reply_markup, **caption_kw, **thread_kw)
            _remember_preview(pending_key, msg)
            logger.info(f"🧵 {news['source']}: {news['title'][:60]} (видео+подпись)")
            return True
        except TelegramError as e:
            logger.warning(f"Видео с подписью не ушло ({e}) — откат")
            return await _send_thread_media_then_text(
                bot, news, photos, has_inline_video, video_file, video_url,
                text, reply_markup, thread_kw, target)

    # 3) Альбом (2+ фото и/или видео+фото): подпись на первом элементе.
    #    У media_group нет reply_markup — поэтому кнопки шлём отдельным
    #    маленьким сообщением-«хвостом» сразу после альбома.
    media: list = []
    opened: list = []
    try:
        if has_inline_video:
            if video_file:
                f = open(video_file, 'rb'); opened.append(f)
                media.append(InputMediaVideo(media=f, supports_streaming=True,
                                             caption=caption, parse_mode=ParseMode.HTML))
            elif video_media is not None:
                media.append(InputMediaVideo(media=video_media, supports_streaming=True,
                                             caption=caption, parse_mode=ParseMode.HTML))
            for ph in (await _resolve_photos_for_album(photos))[:9]:
                media.append(InputMediaPhoto(media=ph))
        else:
            resolved = await _resolve_photos_for_album(photos)
            for i, ph in enumerate(resolved[:10]):
                if i == 0:
                    media.append(InputMediaPhoto(media=ph, caption=caption,
                                                 parse_mode=ParseMode.HTML))
                else:
                    media.append(InputMediaPhoto(media=ph))
        msgs = await _tg_call_flood_safe(lambda: bot.send_media_group(
            chat_id=target, media=media, **thread_kw))
        if isinstance(msgs, (list, tuple)) and msgs:
            _remember_preview(pending_key, msgs[0])   # подпись живёт на первом элементе
        # Хвост с кнопками (media_group не поддерживает inline-кнопки)
        if reply_markup is not None:
            try:
                await bot.send_message(chat_id=target, text='👆 Опубликовать этот пост?',
                                       reply_markup=reply_markup, **thread_kw)
            except TelegramError:
                pass
        logger.info(f"🧵 {news['source']}: {news['title'][:60]} (альбом {len(media)}+подпись)")
        return True
    except TelegramError as e:
        logger.warning(f"Альбом с подписью не прошёл ({e}) — откат на раздельную отправку")
        return await _send_thread_media_then_text(
            bot, news, photos, has_inline_video, video_file, video_media,
            text, reply_markup, thread_kw, target)
    finally:
        for f in opened:
            try:
                f.close()
            except Exception:
                pass


def _remember_preview(pending_key, msg) -> None:
    """Запоминает сообщение, в котором виден текст поста, чтобы правки
    (кнопка ✏️ Изменить) могли обновить его прямо в ветке."""
    if not pending_key or pending_posts is None or msg is None:
        return
    try:
        mid = getattr(msg, 'message_id', None)
        cid = getattr(msg, 'chat_id', None)
        if isinstance(mid, int) and isinstance(cid, int):
            pending_posts.set_preview(pending_key, cid, mid)
    except Exception as e:
        logger.debug(f"preview не запомнен: {e}")


async def _send_single_photo_caption(bot, target, photo, caption_kw, reply_markup,
                                     thread_kw, news, pending_key=None) -> bool:
    """Одно фото с подписью. При отказе URL — скачиваем байтами и повторяем."""
    try:
        msg = await bot.send_photo(chat_id=target, photo=photo, reply_markup=reply_markup,
                                   **caption_kw, **thread_kw)
        _remember_preview(pending_key, msg)
        logger.info(f"🧵 {news['source']}: {news['title'][:60]} (фото+подпись)")
        return True
    except TelegramError as e:
        logger.debug(f"Фото по URL не ушло ({e}), пробую байтами: {photo[:80]}")
        data = await asyncio.to_thread(_download_image_bytes, photo)
        if data:
            try:
                msg = await bot.send_photo(chat_id=target, photo=data, reply_markup=reply_markup,
                                           **caption_kw, **thread_kw)
                _remember_preview(pending_key, msg)
                logger.info(f"🧵 {news['source']}: {news['title'][:60]} (фото байтами+подпись)")
                return True
            except TelegramError as e2:
                logger.debug(f"И байтами фото не ушло ({e2})")
    return False


async def _send_thread_media_then_text(bot, news, photos, has_inline_video, video_file,
                                       video_url, text, reply_markup, thread_kw, target) -> bool:
    """Резервный режим (текст >1024 или сбой цельной отправки): медиа отдельно,
    затем текст отдельным сообщением. Сохраняет все картинки альбомом."""
    safe_text = fit_to_limit(html.escape(text), TG_TEXT_LIMIT)
    media_sent = False

    if has_inline_video:
        try:
            if video_file:
                with open(video_file, 'rb') as f:
                    await bot.send_video(chat_id=target, video=f,
                                         supports_streaming=True, **thread_kw)
            else:
                await bot.send_video(chat_id=target, video=video_url,
                                     supports_streaming=True, **thread_kw)
            media_sent = True
        except TelegramError as e:
            logger.warning(f"Видео в ветку не отправилось ({e})")

    if photos:
        resolved = await _resolve_photos_for_album(photos)
        if len(resolved) > 1:
            try:
                media = [InputMediaPhoto(media=ph) for ph in resolved[:10]]
                await _tg_call_flood_safe(lambda: bot.send_media_group(
                    chat_id=target, media=media, **thread_kw))
                media_sent = True
            except TelegramError as e:
                logger.debug(f"Альбом не прошёл ({e}), по одной")
        if not media_sent:
            for ph in resolved:
                try:
                    await bot.send_photo(chat_id=target, photo=ph, **thread_kw)
                    media_sent = True
                    break
                except TelegramError:
                    continue

    if settings.require_image and not media_sent:
        logger.info(f"⊘ Медиа не ушло, пост пропущен (require_image): {news['title'][:60]}")
        return False

    try:
        await _tg_call_flood_safe(lambda: bot.send_message(
            chat_id=target, text=safe_text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=reply_markup, **thread_kw))
        logger.info(f"🧵 {news['source']}: {news['title'][:60]} (медиа+текст раздельно)")
        return True
    except TelegramError as e:
        logger.error(f"Текст в ветку не отправился: {e}")
        return media_sent


async def send_news_to_thread(bot: Bot, news: dict) -> str:
    """Отправляет один пост в ветку обсуждения (тему форума).
    Использует дедупликацию через sent_links. Метрики считаются.
    Возвращает те же коды что send_news: 'sent'/'skipped_filter'/'skipped_dup'/'failed'."""
    source = news.get('source', 'unknown')

    if not matches_keywords(news):
        return 'skipped_filter'
    # Fuzzy-дедуп: та же новость с другого источника с иной формулировкой
    if sent_links.has_similar_title(news.get('title', '')):
        logger.info(f"⊘ Похожая новость уже публиковалась: {news.get('title', '')[:60]}")
        await stats.record_skipped('duplicate', news.get('source', 'unknown'))
        return 'skipped_dup'
    if not await sent_links.claim(news['link'], news.get('title', '')):
        await stats.record_skipped('duplicate', source)
        return 'skipped_dup'

    # Общий конвейер: картинка, модель, дедупы. Стоит после claim, чтобы
    # не тратить лимит модели на повторы.
    skip = await _prepare_news_for_send(news, source)
    if skip:
        return skip

    video_file = await _prepare_video_file(news)

    try:
        ok = await _send_post_thread_split(bot, news, video_file)
        if ok:
            _commit_image_fingerprint(news)
            _mark_published()
            await stats.record_published(source)
            return 'sent'
        await sent_links.release(news['link'], news.get('title', ''))
        await stats.record_failed_send(source)
        return 'failed'
    finally:
        if video_file:
            try:
                video_file.unlink(missing_ok=True)
            except Exception:
                pass


# Админы, которым бот не может писать (не нажали /start) — предупреждаем один раз
_unreachable_admins: set[int] = set()


async def notify_admin(bot: Bot, text: str) -> None:
    """Шлёт сообщение всем админам (главному и дополнительным)."""
    for uid in _all_admin_ids():
        try:
            await bot.send_message(chat_id=uid, text=text)
            _unreachable_admins.discard(uid)   # снова доступен — сняли метку
        except TelegramError as e:
            if 'initiate conversation' in str(e) or 'blocked' in str(e).lower():
                if uid not in _unreachable_admins:
                    _unreachable_admins.add(uid)
                    logger.warning(
                        f"Админ {uid} недоступен: он ещё не написал боту /start "
                        f"(или заблокировал его). Уведомления ему копиться не будут — "
                        f"пусть откроет бота и нажмёт /start.")
            else:
                logger.error(f"Не удалось уведомить админа {uid}: {e}")


# ============== СБОР ==============
def _note_source_failure(name: str, reason: str) -> None:
    """Отмечает неудачу источника и, если их накопилось подряд слишком много,
    ставит его на паузу. Уведомление отправит check_news — здесь нет бота."""
    if source_health is None:
        return
    fails = source_health.record_fail(name, reason)
    silent = source_health.silent_hours(name) or 0
    logger.info(f"{name}: молчит {silent:.1f} ч, проверок подряд без новостей: "
                f"{fails} ({reason[:70]})")
    # Пауза по времени, а не по счётчику: источник может законно молчать
    # ночью или в выходной, но сутки тишины — это уже симптом.
    if silent < AUTO_DISABLE_AFTER_HOURS or fails < AUTO_DISABLE_MIN_CHECKS:
        return
    if settings is None or not settings.auto_disable_sources:
        return
    if not settings.is_source_enabled(name):
        return                       # уже на паузе — второй раз не трогаем
    settings.toggle_source(name)     # источник был включён → выключаем
    _auto_disabled_pending.append((name, f'{silent:.0f} ч без новостей. {reason}'))
    logger.warning(f"⏸ Источник {name} выключен: молчит {silent:.0f} ч "
                   f"({fails} проверок), последняя причина: {reason[:80]}")


async def collect_all_news() -> tuple[list[dict], list[str], list[str]]:
    """Собирает свежие новости со всех включённых источников.
    Возвращает (all_news, stats_lines, errors)."""
    all_news: list[dict] = []
    stats_lines: list[str] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    # Сбор идёт параллельно (сеть — самая долгая часть цикла), но результаты
    # обрабатываются в исходном порядке источников: дедуп остаётся предсказуемым.
    enabled = [(n, c) for n, c in SOURCES if settings.is_source_enabled(n)]
    for name, _c in SOURCES:
        if not settings.is_source_enabled(name):
            stats_lines.append(f"{name}: ⏸")

    sem = asyncio.Semaphore(SOURCE_FETCH_CONCURRENCY)

    async def _fetch(collector):
        async with sem:
            return await asyncio.to_thread(collector)

    fetched = await asyncio.gather(*(_fetch(c) for _n, c in enabled),
                                   return_exceptions=True)

    for (name, _collector), result in zip(enabled, fetched):
        try:
            if isinstance(result, BaseException):
                raise result
            items = result
            unique_items = []
            no_image_skipped = 0
            duplicate_skipped = 0
            for item in items:
                norm_url = normalize_url(item.get('link', ''))
                norm_title = normalize_title(item.get('title', ''))
                if norm_url and norm_url in seen_urls:
                    duplicate_skipped += 1
                    continue
                if norm_title and norm_title in seen_titles:
                    logger.info(f"Дубль внутри сбора (заголовок): {item['title'][:60]}")
                    duplicate_skipped += 1
                    continue
                # Фильтр: посты без картинок не публикуем
                if settings.require_image and not item.get('images'):
                    no_image_skipped += 1
                    continue
                seen_urls.add(norm_url)
                if norm_title:
                    seen_titles.add(norm_title)
                unique_items.append(item)

            # Здоровье источника считаем по СЫРОМУ ответу: живой источник может
            # отдать одни дубли, а мёртвый не отдаёт вообще ничего.
            if source_health is not None:
                if items:
                    source_health.record_ok(name, len(items))
                else:
                    _note_source_failure(name, 'вернул 0 постов')

            all_news.extend(unique_items)
            stat_line = f"{name}: {len(unique_items)}"
            if no_image_skipped:
                stat_line += f" (⊘{no_image_skipped} без фото)"
            stats_lines.append(stat_line)
            logger.info(f"{name}: {len(unique_items)} новостей (из {len(items)} собранных, {no_image_skipped} без фото)")

            # === Метрики ===
            if unique_items:
                await stats.record_collected(name, len(unique_items))
            for _ in range(no_image_skipped):
                await stats.record_skipped('no_image', name)
            for _ in range(duplicate_skipped):
                await stats.record_skipped('duplicate', name)
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.error(f"{name} failed: {e}")
            await stats.record_source_error(name)
            _note_source_failure(name, f'{type(e).__name__}: {e}')
    return all_news, stats_lines, errors


# ============== ИНТЕРФЕЙС: КЛАВИАТУРЫ И ПРОВЕРКА ДОСТУПА ==============
# Тексты на reply-кнопках. Используются как идентификаторы (по тексту матчим действие).
BTN_NEWS = "🔍 Свежие новости"
BTN_PREVIEW = "👁 Превью"
BTN_START_AUTO = "▶️ Запустить авто"
BTN_STOP_AUTO = "⏸ Остановить авто"
BTN_STATUS = "📊 Статус"
BTN_SETTINGS = "⚙️ Настройки"

REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_NEWS), KeyboardButton(BTN_PREVIEW)],
        [KeyboardButton(BTN_START_AUTO), KeyboardButton(BTN_STOP_AUTO)],
        [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_SETTINGS)],
    ],
    resize_keyboard=True,
)


def _all_admin_ids() -> set[int]:
    """Главный админ (из env) + дополнительные (из настроек)."""
    extra = list(getattr(settings, 'extra_admins', []) or [])
    return {ADMIN_ID, *extra}


def is_admin(update: Update) -> bool:
    """Проверяет, что отправитель — админ (главный или дополнительный)."""
    user = update.effective_user
    if not user:
        return False
    return user.id in _all_admin_ids()


async def deny_access(update: Update) -> None:
    """Сообщает не-админу, что доступа нет."""
    try:
        if update.callback_query:
            await update.callback_query.answer("Эта кнопка только для админа.", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Этот бот только для администратора.")
    except Exception as e:
        # Отказ в доступе не должен ронять обработчик ни при каких условиях
        logger.debug(f"deny_access: {type(e).__name__}: {e}")


# ============== INLINE-МЕНЮ "НАСТРОЙКИ" ==============
def _sw(value: bool) -> str:
    """Компактный индикатор состояния для кнопки."""
    return '🟢' if value else '⚪️'


SETTINGS_SECTIONS = {
    'posts': '📝 Посты',
    'media': '🎬 Медиа',
    'llm': '🤖 Модель',
    'sources': '📡 Источники',
    'system': '🔧 Система',
}


def build_settings_menu() -> InlineKeyboardMarkup:
    """Главное меню: разделы, а не два десятка кнопок подряд.

    Раньше все настройки лежали одним столбцом на 23 кнопки — с телефона это
    бесконечная прокрутка, где важное перемешано с редко нужным."""
    rows = [[InlineKeyboardButton(SETTINGS_SECTIONS['posts'],
                                  callback_data='settings:sec:posts'),
             InlineKeyboardButton(SETTINGS_SECTIONS['media'],
                                  callback_data='settings:sec:media')],
            [InlineKeyboardButton(SETTINGS_SECTIONS['llm'],
                                  callback_data='settings:sec:llm'),
             InlineKeyboardButton(SETTINGS_SECTIONS['sources'],
                                  callback_data='settings:sec:sources')],
            [InlineKeyboardButton(SETTINGS_SECTIONS['system'],
                                  callback_data='settings:sec:system')],
            [InlineKeyboardButton('✖ Закрыть', callback_data='settings:close')]]
    return InlineKeyboardMarkup(rows)


def _menu_posts() -> InlineKeyboardMarkup:
    """Что и как публикуется."""
    thread = ('🧵 Куда слать: в ветку' if settings.thread_mode
              else '📢 Куда слать: сразу в канал')
    quiet = f'{_sw(settings.quiet_mode)} Тихий режим'
    open_mod = ('👥 Кнопки в ветке: всем' if settings.open_moderation
                else '👤 Кнопки в ветке: админам')
    rows = [
        [InlineKeyboardButton(thread, callback_data='settings:toggle_thread')],
        [InlineKeyboardButton(f'⏰ Свежесть: {settings.post_max_age_hours} ч',
                              callback_data='settings:age'),
         InlineKeyboardButton('🔁 Интервал', callback_data='settings:interval')],
        [InlineKeyboardButton(open_mod, callback_data='settings:toggle_open')],
        [InlineKeyboardButton(quiet, callback_data='settings:toggle_quiet')],
        [InlineKeyboardButton(f'{_sw(settings.dedup_final_text)} Ловить повтор новостей',
                              callback_data='settings:toggle_finaldedup')],
        [InlineKeyboardButton('📦 Очередь', callback_data='settings:queue'),
         InlineKeyboardButton('🧹 История', callback_data='settings:history')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='settings:back')],
    ]
    return InlineKeyboardMarkup(rows)


def _menu_media() -> InlineKeyboardMarkup:
    """Картинки и видео."""
    rows = [
        [InlineKeyboardButton(f'{_sw(settings.video_enabled)} Видео в постах',
                              callback_data='settings:video')],
        [InlineKeyboardButton(f'{_sw(settings.require_image)} Только с картинкой',
                              callback_data='settings:toggle_require_image')],
        [InlineKeyboardButton(f'{_sw(settings.image_dedup)} Ловить повтор картинок',
                              callback_data='settings:toggle_dedup')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='settings:back')],
    ]
    return InlineKeyboardMarkup(rows)


def _menu_llm() -> InlineKeyboardMarkup:
    """Языковая модель и перевод."""
    if settings.translator_engine == 'google':
        tr = '🌐 Перевод: Google'
    elif DEEPL_API_KEY:
        tr = '🌐 Перевод: DeepL'
    else:
        tr = '🌐 Перевод: DeepL (нет ключа → Google)'
    rows = []
    if _llm_configured():
        rows.append([InlineKeyboardButton(
            f'{_sw(settings.llm_enabled)} Языковая модель',
            callback_data='settings:toggle_llm')])
        if settings.llm_enabled:
            rows += [
                [InlineKeyboardButton(f'{_sw(settings.llm_rewrite)} Перевод и текст',
                                      callback_data='settings:toggle_llm_rewrite'),
                 InlineKeyboardButton(f'{_sw(settings.llm_tags)} Хэштеги',
                                      callback_data='settings:toggle_llm_tags')],
                [InlineKeyboardButton(f'{_sw(settings.llm_read_article)} Читать статьи',
                                      callback_data='settings:toggle_llm_article'),
                 InlineKeyboardButton(f'{_sw(settings.llm_filter)} Отсев чужих тем',
                                      callback_data='settings:toggle_llm_filter')],
                [InlineKeyboardButton(f'{_sw(settings.llm_skip_filler)} Отсев подборок',
                                      callback_data='settings:toggle_llm_filler'),
                 InlineKeyboardButton(f'{_sw(settings.llm_dedup_subject)} Ловить повторы',
                                      callback_data='settings:toggle_llm_dedup')],
                [InlineKeyboardButton(f'{_sw(settings.llm_limit_repeats)} Лимит на тайтл',
                                      callback_data='settings:toggle_llm_repeats')],
            ]
    else:
        rows.append([InlineKeyboardButton('🤖 Модель не настроена — /llm',
                                          callback_data='settings:llm_help')])
    rows.append([InlineKeyboardButton(tr, callback_data='settings:toggle_translator')])
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='settings:back')])
    return InlineKeyboardMarkup(rows)


def _menu_sources() -> InlineKeyboardMarkup:
    """Источники новостей."""
    enabled = sum(1 for n, _ in SOURCES if settings.is_source_enabled(n))
    rows = [
        [InlineKeyboardButton(f'📡 Список ({enabled} из {len(SOURCES)})',
                              callback_data='settings:sources')],
        [InlineKeyboardButton(f'{_sw(settings.auto_disable_sources)} Автопауза молчунов',
                              callback_data='settings:toggle_autodis')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='settings:back')],
    ]
    return InlineKeyboardMarkup(rows)


def _menu_system() -> InlineKeyboardMarkup:
    """Служебное."""
    rows = [
        [InlineKeyboardButton(f'{_sw(settings.daily_backup)} Ежедневный бэкап',
                              callback_data='settings:toggle_backup')],
        [InlineKeyboardButton(f'{_sw(settings.startup_report)} Отчёт при запуске',
                              callback_data='settings:toggle_startup')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='settings:back')],
    ]
    return InlineKeyboardMarkup(rows)


_SECTION_BUILDERS = {
    'posts': _menu_posts,
    'media': _menu_media,
    'llm': _menu_llm,
    'sources': _menu_sources,
    'system': _menu_system,
}


_TOGGLE_SECTION = {
    'toggle_thread': 'posts', 'toggle_quiet': 'posts', 'toggle_open': 'posts',
    'toggle_finaldedup': 'posts',
    'age': 'posts', 'interval': 'posts',
    'video': 'media', 'toggle_require_image': 'media', 'toggle_dedup': 'media',
    'toggle_llm': 'llm', 'toggle_llm_rewrite': 'llm', 'toggle_llm_filter': 'llm',
    'toggle_llm_tags': 'llm', 'toggle_llm_article': 'llm', 'toggle_llm_filler': 'llm',
    'toggle_llm_dedup': 'llm', 'toggle_llm_repeats': 'llm',
    'toggle_translator': 'llm',
    'toggle_autodis': 'sources', 'sources': 'sources',
    'toggle_backup': 'system', 'toggle_startup': 'system',
}


def _menu_for(data: str) -> InlineKeyboardMarkup:
    """Клавиатура того раздела, откуда нажали кнопку.

    После переключения тумблера надо остаться на месте: выбрасывать в корень
    каждый раз — значит заставлять заново нырять в раздел ради второй галочки."""
    key = data.split(':', 1)[-1]
    section = _TOGGLE_SECTION.get(key)
    builder = _SECTION_BUILDERS.get(section) if section else None
    return builder() if builder else build_settings_menu()


def _section_view(name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Заголовок и клавиатура раздела."""
    builder = _SECTION_BUILDERS.get(name)
    if builder is None:
        return '⚙️ Настройки', build_settings_menu()
    hints = {
        'posts': 'Куда и как часто уходят посты.',
        'media': 'Картинки и видео в постах.',
        'llm': 'Перевод, текст постов, теги и фильтры.',
        'sources': 'Откуда бот берёт новости.',
        'system': 'Бэкапы и уведомления.',
    }
    return f'{SETTINGS_SECTIONS[name]}\n\n{hints[name]}', builder()



def build_age_menu() -> InlineKeyboardMarkup:
    """Меню выбора максимального возраста поста."""
    options = [12, 24, 36, 48, 72, 168]
    current = settings.post_max_age_hours
    rows = []
    for opt in options:
        marker = "✅ " if opt == current else ""
        if opt < 48:
            label = f"{marker}{opt} ч"
        elif opt < 168:
            label = f"{marker}{opt // 24} дня"
        else:
            label = f"{marker}1 неделя"
        rows.append([InlineKeyboardButton(label, callback_data=f"age:{opt}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(rows)


def build_sources_menu() -> InlineKeyboardMarkup:
    """Меню переключения источников. Каждый источник = отдельная кнопка с текущим состоянием."""
    rows = []
    for name, _ in SOURCES:
        is_on = settings.is_source_enabled(name)
        icon = "🟢" if is_on else "🔴"
        rows.append([InlineKeyboardButton(
            f"{icon} {name}",
            callback_data=f"src:{name}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(rows)


def build_interval_menu() -> InlineKeyboardMarkup:
    """Меню выбора интервала автопроверки."""
    options = [15, 30, 60, 120, 240]
    current = settings.check_interval_min
    rows = []
    for opt in options:
        marker = "✅ " if opt == current else ""
        label = f"{marker}{opt} мин" if opt < 60 else f"{marker}{opt // 60} ч"
        rows.append([InlineKeyboardButton(label, callback_data=f"int:{opt}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")])
    return InlineKeyboardMarkup(rows)


def build_video_menu() -> InlineKeyboardMarkup:
    """Меню вкл/выкл видео."""
    is_on = settings.video_enabled
    label_on = "✅ Включить скачивание" if not is_on else "🟢 Включено (нажмите чтобы выключить)"
    label_off = "❌ Выключить скачивание" if is_on else "🔴 Выключено (нажмите чтобы включить)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label_on if not is_on else label_off, callback_data="video:toggle")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")],
    ])


def build_history_menu() -> InlineKeyboardMarkup:
    """Меню истории ссылок."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Очистить историю", callback_data="hist:clear_confirm")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")],
    ])


def build_history_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, очистить", callback_data="hist:clear_yes"),
            InlineKeyboardButton("✖ Отмена", callback_data="settings:history"),
        ],
    ])


def build_queue_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Отправить пост сейчас", callback_data="queue:send_now")],
        [InlineKeyboardButton("🗑 Очистить очередь", callback_data="queue:clear_confirm")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings:back")],
    ])


def build_queue_clear_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, очистить", callback_data="queue:clear_yes"),
            InlineKeyboardButton("✖ Отмена", callback_data="settings:queue"),
        ],
    ])


# ============== ОБРАБОТЧИКИ INLINE-КНОПОК ==============
_SCHEDULE_HINT = (
    '📅 <b>Во сколько опубликовать?</b>\n'
    'Ответь сообщением, например:\n'
    '• <code>18:30</code> — сегодня (если прошло — завтра)\n'
    '• <code>12.07 18:30</code> — конкретная дата\n'
    '• <code>завтра 10:00</code>\n'
    '• <code>+2ч</code> или <code>+30м</code>\n\n'
    'Сейчас у тебя {now} (UTC{off:+d}). Не тот пояс — /tz\n'
    'Отмена — /cancel'
)

_EDIT_HINT = (
    '✏️ <b>Пришли новый текст поста</b> одним сообщением.\n'
    'Он полностью заменит текущий — и в канал/отложку уйдёт именно он.\n\n'
    'Сейчас:\n<code>{current}</code>\n\n'
    'Отмена — /cancel'
)


async def _ask_in_thread(bot: Bot, message, text: str) -> None:
    """Задаёт админу вопрос ответом на пост в ветке (чтобы было видно, к чему он)."""
    kw = {}
    thread_id = getattr(message, 'message_thread_id', None)
    if thread_id:
        kw['message_thread_id'] = thread_id
    try:
        await bot.send_message(
            chat_id=message.chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id, **kw)
    except TelegramError as e:
        logger.debug(f"подсказка не отправилась: {e}")


async def _mark_post_done(query, suffix: str) -> None:
    """Дописывает пометку к посту в ветке и убирает кнопки.
    Пост бывает и текстом, и медиа с подписью — пробуем оба варианта."""
    try:
        if getattr(query.message, 'text', None) is not None:
            await query.edit_message_text((query.message.text or '') + suffix, reply_markup=None)
            return
    except TelegramError:
        pass
    try:
        if getattr(query.message, 'caption', None) is not None:
            await query.edit_message_caption(
                caption=(query.message.caption or '') + suffix, reply_markup=None)
            return
    except TelegramError:
        pass
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


def _human_delta(when_utc: datetime) -> str:
    """'2 ч 15 мин' — понятная задержка до публикации.
    Округляем до ближайшей минуты, иначе на '+2ч' ответим 'через 1 ч 59 мин'."""
    raw = (when_utc - datetime.now(timezone.utc)).total_seconds()
    secs = int(round(raw / 60.0)) * 60
    if secs < 60:
        return 'меньше минуты'
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f'{days} д')
    if hours:
        parts.append(f'{hours} ч')
    if mins and not days:
        parts.append(f'{mins} мин')
    return ' '.join(parts) or 'меньше минуты'


async def _update_preview_text(bot: Bot, key: str, new_text: str) -> bool:
    """Обновляет текст поста в ветке после правки (best-effort).
    Пост может быть фото с подписью, альбомом или текстом — пробуем по очереди."""
    if pending_posts is None:
        return False
    prev = pending_posts.get_preview(key)
    if not prev:
        return False
    chat_id, message_id = prev.get('chat_id'), prev.get('message_id')
    caption = fit_to_limit(html.escape(new_text), TG_CAPTION_LIMIT)
    markup = _moderation_markup(key)
    attempts = (
        lambda: bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                         caption=caption, parse_mode=ParseMode.HTML,
                                         reply_markup=markup),
        # у элементов альбома inline-кнопки не поддерживаются — пробуем без них
        lambda: bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                         caption=caption, parse_mode=ParseMode.HTML),
        lambda: bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                      text=fit_to_limit(html.escape(new_text), TG_TEXT_LIMIT),
                                      parse_mode=ParseMode.HTML, reply_markup=markup),
    )
    for attempt in attempts:
        try:
            await attempt()
            return True
        except TelegramError:
            continue
    logger.debug(f"превью поста {key} не обновилось — текст всё равно сохранён")
    return False


# Лимит действий гостей: с открытой модерацией любой участник ветки может
# нажимать «В канал». Без ограничения один человек способен за минуту засыпать
# канал десятком постов. Админов лимит не касается.
GUEST_ACTIONS_PER_HOUR = 10
_guest_actions: dict[int, list[float]] = {}


def _guest_rate_ok(user_id: int) -> bool:
    """True, если гостю можно выполнить действие. Считает окно в час."""
    now = time.time()
    hits = [t for t in _guest_actions.get(user_id, []) if now - t < 3600]
    if len(hits) >= GUEST_ACTIONS_PER_HOUR:
        _guest_actions[user_id] = hits
        return False
    hits.append(now)
    _guest_actions[user_id] = hits
    if len(_guest_actions) > 500:            # чистим тех, у кого окно истекло
        for uid in [u for u, ts in _guest_actions.items()
                    if not ts or now - ts[-1] > 3600]:
            _guest_actions.pop(uid, None)
    return True


def _in_moderation_thread(message) -> bool:
    """Нажатие произошло в предназначенной ветке модерации?
    Жёсткая защита: кнопки под постами работают ТОЛЬКО в нашей супергруппе
    и только в нашей теме — пересланное сообщение с кнопками в другом чате
    (или другая ветка) получит отказ."""
    if message is None:
        return False
    chat = getattr(message, 'chat_id', None)
    thread = getattr(message, 'message_thread_id', None)
    return chat == DISCUSSION_CHAT_ID and thread == DISCUSSION_THREAD_ID


def _actor(update: Update) -> tuple[bool, str]:
    """(является_ли_админом, имя_для_уведомлений)."""
    user = update.effective_user
    if not user:
        return False, '?'
    name = user.full_name or user.username or str(user.id)
    return user.id in _all_admin_ids(), name


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик всех callback_data из inline-меню.

    Кнопки модерации под постами (pub/sch/edit/dis) доступны всем участникам
    ветки при open_moderation (по умолчанию ВКЛ) — но строго только в самой
    ветке. Всё остальное (настройки, /scheduled-кнопки) — только админам."""
    query = update.callback_query
    data = query.data or ""

    # === Кнопки модерации под постами в ветке ===
    # 📢 В канал / 📅 В отложку / ✏️ Изменить / ✖ Скрыть
    # Обрабатываем ДО общего query.answer(): ответ callback даётся один раз,
    # и здесь он зависит от результата.
    if data.startswith(('pub:', 'sch:', 'edit:', 'dis:')):
        # Жёсткая защита места: только наша супергруппа и наша тема
        if not _in_moderation_thread(query.message):
            await query.answer('Эти кнопки работают только в ветке модерации.',
                               show_alert=True)
            return
        if user_directory is not None:
            user_directory.remember(update.effective_user)
        actor_is_admin, actor_name = _actor(update)
        if not settings.open_moderation and not actor_is_admin:
            await query.answer('Кнопки доступны только админам.', show_alert=True)
            return
        actor_id = update.effective_user.id if update.effective_user else 0
        if not actor_is_admin and not _guest_rate_ok(actor_id):
            await query.answer(
                f'Слишком много действий подряд (лимит {GUEST_ACTIONS_PER_HOUR} в час). '
                f'Попробуй позже.', show_alert=True)
            logger.warning(f"Гость {actor_name} ({actor_id}) упёрся в лимит действий")
            return
        action, key = data.split(':', 1)

        if action == 'dis':
            hidden = pending_posts.pop(key) if pending_posts is not None else None
            await query.answer('Скрыто')
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            if hidden and not actor_is_admin:
                title = re.sub(r'\s+', ' ', hidden.get('title', ''))[:80]
                await notify_admin(context.bot,
                                   f'👥 {actor_name} скрыл пост в ветке:\n{title}')
            return

        news = pending_posts.get(key) if pending_posts is not None else None
        if not news:
            await query.answer('Пост устарел или уже опубликован', show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            return

        # 📅 В отложку — просим время текстом, ответ поймает awaiting_input_handler
        if action == 'sch':
            context.user_data['await_input'] = _await_ctx('schedule', key, query.message)
            await query.answer('Пришли время публикации')
            await _ask_in_thread(context.bot, query.message, _SCHEDULE_HINT.format(
                now=_local_now().strftime('%d.%m %H:%M'), off=_tz_offset()))
            return

        # ✏️ Изменить — просим новый текст поста
        if action == 'edit':
            context.user_data['await_input'] = _await_ctx('edit', key, query.message)
            await query.answer('Пришли новый текст')
            current = fit_to_limit(format_news_short(news), 700)
            await _ask_in_thread(context.bot, query.message, _EDIT_HINT.format(
                current=html.escape(current)))
            return

        # 📢 В канал — публикуем сразу
        with _PublishGuard(f'pending:{key}') as guard:
            if not guard.acquired:
                await query.answer('Этот пост уже публикуется…')
                return
            ok = await _send_post(context.bot, news, CHANNEL_ID, None)
        if ok:
            pending_posts.pop(key)
            _mark_published()
            await query.answer('📢 Опубликовано в канал!')
            await _mark_post_done(query, '\n\n✅ Опубликовано в канал')
            if not actor_is_admin:
                await notify_admin(
                    context.bot,
                    f'👥 {actor_name} опубликовал в канал пост из ветки:\n\n'
                    f'{_post_card(news, {})}')
        else:
            await query.answer('❌ Не удалось опубликовать — см. /logs', show_alert=True)
        return

    # === Обзор очереди: список / карточка / очистка ===
    if data == 'slist' or data.startswith('sview:') or data in ('sclear', 'sclearyes'):
        if not is_admin(update):
            await deny_access(update)
            return
        if data == 'slist':
            text, markup = _scheduled_overview()
            await query.answer()
            await _safe_edit(query, text, markup)
            return
        if data.startswith('sview:'):
            text, markup = _scheduled_detail(data.split(':', 1)[1])
            await query.answer()
            await _safe_edit(query, text, markup)
            return
        if data == 'sclear':
            total = len(scheduled_posts.all()) if scheduled_posts is not None else 0
            if not total:
                await query.answer('Отложка и так пуста')
                return
            await query.answer()
            await _safe_edit(
                query,
                f'🗑 Снять с отложки все посты ({total})?\n\nЭто необратимо.',
                InlineKeyboardMarkup([[
                    InlineKeyboardButton('Да, очистить', callback_data='sclearyes'),
                    InlineKeyboardButton('Отмена', callback_data='slist'),
                ]]))
            return
        removed = scheduled_posts.clear() if scheduled_posts is not None else 0
        logger.info(f"📅 Отложка очищена вручную: снято {removed}")
        await query.answer(f'Снято постов: {removed}')
        text, markup = _scheduled_overview()
        await _safe_edit(query, text, markup)
        return

    # === Перенос времени публикации ===
    if data.startswith('sedit:'):
        if not is_admin(update):
            await deny_access(update)
            return
        key = data.split(':', 1)[1]
        if scheduled_posts is None or scheduled_posts.get(key) is None:
            await query.answer('Этого поста уже нет в отложке', show_alert=True)
            return
        context.user_data['await_input'] = _await_ctx('reschedule', key, query.message)
        when = scheduled_posts.when(key)
        await query.answer('Пришли новое время')
        await _ask_in_thread(context.bot, query.message, _RESCHEDULE_HINT.format(
            old=_fmt_local(when) if when else '?',
            now=_local_now().strftime('%d.%m %H:%M'), off=_tz_offset()))
        return

    # === Кнопки под карточкой поста ===
    if data.startswith(('snow:', 'scan:')):
        if not is_admin(update):
            await deny_access(update)
            return
        action, key = data.split(':', 1)
        news = scheduled_posts.get(key) if scheduled_posts is not None else None
        if not news:
            await query.answer('Этого поста уже нет в отложке', show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            return
        if action == 'scan':
            scheduled_posts.pop(key)
            await query.answer('Снято с отложки')
            text, markup = _scheduled_overview()
            await _safe_edit(query, text, markup)
            return
        with _PublishGuard(f'sched:{key}') as guard:
            if not guard.acquired:
                await query.answer('Этот пост уже публикуется…')
                return
            ok = await _send_post(context.bot, news, CHANNEL_ID, None)
        if ok:
            scheduled_posts.pop(key)
            _mark_published()
            await query.answer('📢 Опубликовано!')
            text, markup = _scheduled_overview()
            await _safe_edit(query, text, markup)
        else:
            await query.answer('❌ Не удалось опубликовать — см. /logs', show_alert=True)
        return

    # Всё остальное (настройки, кнопки /scheduled) — только для админов
    if not is_admin(update):
        await deny_access(update)
        return

    await query.answer()

    # === Главное меню и разделы ===
    if data == "settings:back":
        await _safe_edit(query, '⚙️ <b>Настройки</b>\n\nВыбери раздел.',
                         build_settings_menu())
        return
    if data.startswith("settings:sec:"):
        text, markup = _section_view(data.split(':', 2)[2])
        await _safe_edit(query, text, markup)
        return
    if data == "settings:llm_help":
        await query.answer(
            'Модель включается двумя переменными на хостинге: '
            'LLM_PROVIDER и LLM_API_KEY. Подробности — команда /llm',
            show_alert=True)
        return
    if data == "settings:close":
        await query.edit_message_text("Меню закрыто.")
        return
    if data == "settings:sources":
        await query.edit_message_text(
            "📡 Источники (нажмите чтобы переключить):",
            reply_markup=build_sources_menu(),
        )
        return
    if data == "settings:interval":
        await query.edit_message_text(
            f"🔁 Интервал автопроверки\n\nТекущий: {settings.check_interval_min} мин",
            reply_markup=build_interval_menu(),
        )
        return
    if data == "settings:age":
        await query.edit_message_text(
            f"⏰ Максимальный возраст поста\n\n"
            f"Посты старше указанного срока не будут публиковаться.\n"
            f"Текущий: {settings.post_max_age_hours} часов",
            reply_markup=build_age_menu(),
        )
        return
    if data.startswith("age:"):
        try:
            new_age = int(data[4:])
        except ValueError:
            return
        settings.post_max_age_hours = new_age
        await query.answer(f"Свежесть: {new_age} часов")
        await query.edit_message_text(
            f"⏰ Максимальный возраст поста\n\n"
            f"Текущий: {settings.post_max_age_hours} часов",
            reply_markup=build_age_menu(),
        )
        return
    if data == "settings:video":
        state = "включено 🟢" if settings.video_enabled else "выключено 🔴"
        await query.edit_message_text(
            f"🎬 Скачивание видео\n\nСостояние: {state}",
            reply_markup=build_video_menu(),
        )
        return
    if data == "settings:history":
        await query.edit_message_text(
            f"🧹 История отправленных ссылок\n\n"
            f"Записей: {len(sent_links._set)}",
            reply_markup=build_history_menu(),
        )
        return

    if data == "settings:toggle_require_image":
        settings.require_image = not settings.require_image
        state = "включено 🟢" if settings.require_image else "выключено 🔴"
        await query.answer(f"Только с картинками: {state}")
        await query.edit_message_text(
            "⚙️ Настройки",
            reply_markup=_menu_for(data),
        )
        return

    if data == "settings:toggle_thread":
        settings.thread_mode = not settings.thread_mode
        if settings.thread_mode:
            await query.answer("Режим ветки включён 🟢")
            text = (
                "⚙️ Настройки\n\n"
                "🧵 Режим ветки ВКЛЮЧЁН.\n"
                "Все найденные новости будут отправляться пачкой "
                "в ветку обсуждения, а не по одной в канал."
            )
        else:
            await query.answer("Режим ветки выключен 🔴")
            text = (
                "⚙️ Настройки\n\n"
                "🧵 Режим ветки ВЫКЛЮЧЕН.\n"
                "Бот снова публикует по одному посту в канал за интервал."
            )
        await query.edit_message_text(text, reply_markup=_menu_for(data))
        return

    if data.startswith("settings:toggle_llm"):
        field = {
            'settings:toggle_llm': ('llm_enabled', 'Языковая модель'),
            'settings:toggle_llm_rewrite': ('llm_rewrite', 'Перевод и текст'),
            'settings:toggle_llm_filter': ('llm_filter', 'Отсев не по теме'),
            'settings:toggle_llm_tags': ('llm_tags', 'Хэштеги'),
            'settings:toggle_llm_article': ('llm_read_article', 'Чтение статей'),
            'settings:toggle_llm_filler': ('llm_skip_filler', 'Отсев подборок'),
            'settings:toggle_llm_dedup': ('llm_dedup_subject', 'Ловля повторов'),
            'settings:toggle_llm_repeats': ('llm_limit_repeats', 'Лимит на тайтл'),
        }.get(data)
        if field:
            attr, human = field
            setattr(settings, attr, not getattr(settings, attr))
            state = 'включено' if getattr(settings, attr) else 'выключено'
            await query.answer(f'{human}: {state}')
            note = f'🤖 {human}: {state.upper()}'
            if attr == 'llm_enabled' and not settings.llm_enabled:
                note += '\n\nБот вернулся к переводу через DeepL/Google.'
            await query.edit_message_text(f"⚙️ Настройки\n\n{note}",
                                          reply_markup=_menu_for(data))
            return

    if data == "settings:toggle_finaldedup":
        settings.dedup_final_text = not settings.dedup_final_text
        state = 'включена' if settings.dedup_final_text else 'выключена'
        await query.answer(f'Проверка повторов {state}')
        note = ('🔁 Готовый текст поста сверяется с недавними — ловит одну новость, '
                'пришедшую с разных сайтов разными формулировками.'
                if settings.dedup_final_text else
                '🔁 Проверка выключена: возможны повторы одной новости.')
        await query.edit_message_text(f"⚙️ Настройки\n\n{note}",
                                      reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_dedup":
        settings.image_dedup = not settings.image_dedup
        await query.answer('Дедуп по картинке ' + ('включён' if settings.image_dedup else 'выключен'))
        note = ('🖼 Посты с уже публиковавшейся картинкой будут отсеиваться — '
                'ловит один и тот же анонс с разных сайтов.'
                if settings.image_dedup else
                '🖼 Проверка картинок выключена: возможны повторы одного кадра.')
        await query.edit_message_text(f"⚙️ Настройки\n\n{note}",
                                      reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_autodis":
        settings.auto_disable_sources = not settings.auto_disable_sources
        await query.answer('Автопауза ' + ('включена' if settings.auto_disable_sources else 'выключена'))
        note = (f'⏸ Источник, не отдающий новостей больше '
                f'{AUTO_DISABLE_AFTER_HOURS} ч, будет ставиться на паузу '
                f'с уведомлением.'
                if settings.auto_disable_sources else
                '⏸ Мёртвые источники останутся включёнными — смотри /health.')
        await query.edit_message_text(f"⚙️ Настройки\n\n{note}",
                                      reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_backup":
        settings.daily_backup = not settings.daily_backup
        await query.answer('Бэкап ' + ('включён' if settings.daily_backup else 'выключен'))
        note = (f'📦 Раз в сутки (после {BACKUP_HOUR}:00) архив данных будет приходить в личку.'
                if settings.daily_backup else
                '📦 Автобэкап выключен. Вручную — /backup.')
        await query.edit_message_text(f"⚙️ Настройки\n\n{note}",
                                      reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_open":
        settings.open_moderation = not settings.open_moderation
        if settings.open_moderation:
            await query.answer('Кнопки в ветке доступны всем 👥')
            text = ("⚙️ Настройки\n\n"
                    "👥 Кнопки под постами в ветке теперь доступны ВСЕМ участникам.\n"
                    "О действиях гостей (публикация/отложка/правка/скрытие) "
                    "админам приходят уведомления.")
        else:
            await query.answer('Кнопки в ветке — только админам 👤')
            text = ("⚙️ Настройки\n\n"
                    "👤 Кнопки под постами теперь работают только у админов.")
        await query.edit_message_text(text, reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_quiet":
        settings.quiet_mode = not settings.quiet_mode
        if settings.quiet_mode:
            await query.answer("Тихий режим включён 🔕")
            text = (
                "⚙️ Настройки\n\n"
                "🔕 Тихий режим ВКЛЮЧЁН.\n"
                "Уведомления о каждой проверке отключены. Бот напишет только "
                "при ошибках + пришлёт одну сводку в день.\n"
                "Всегда доступны: /stats /status /logs"
            )
        else:
            await query.answer("Тихий режим выключен 🔔")
            text = (
                "⚙️ Настройки\n\n"
                "🔔 Тихий режим ВЫКЛЮЧЕН.\n"
                "Бот снова уведомляет о каждой проверке (каждые "
                f"{settings.check_interval_min} мин)."
            )
        await query.edit_message_text(text, reply_markup=_menu_for(data))
        return

    if data == "settings:toggle_translator":
        if settings.translator_engine == 'deepl':
            settings.translator_engine = 'google'
            await query.answer("Переводчик: Google Translate")
            text = (
                "⚙️ Настройки\n\n"
                "🌐 Переводчик переключён на Google Translate.\n"
                "DeepL не используется, даже если ключ задан "
                "(полезно для экономии лимита DeepL)."
            )
        else:
            settings.translator_engine = 'deepl'
            if DEEPL_API_KEY:
                await query.answer("Переводчик: DeepL 🟢")
                text = (
                    "⚙️ Настройки\n\n"
                    "🌐 Переводчик переключён на DeepL.\n"
                    "При ошибке или исчерпании лимита бот автоматически "
                    "откатится на Google Translate."
                )
            else:
                await query.answer("Ключ DeepL не задан!", show_alert=True)
                text = (
                    "⚙️ Настройки\n\n"
                    "🌐 Выбран DeepL, но ключ DEEPL_API_KEY не задан — "
                    "фактически будет работать Google Translate.\n"
                    "Добавь переменную окружения DEEPL_API_KEY и перезапусти бота."
                )
        # Переводы кешируются — очищаем кеш чтобы новый движок применился сразу
        _translation_cache.clear()
        await query.edit_message_text(text, reply_markup=_menu_for(data))
        return

    # === Переключение источника ===
    if data.startswith("src:"):
        name = data[4:]
        new_state = settings.toggle_source(name)
        await query.answer(f"{name}: {'включён' if new_state else 'выключен'}")
        await query.edit_message_reply_markup(reply_markup=build_sources_menu())
        return

    # === Смена интервала ===
    if data.startswith("int:"):
        try:
            new_min = int(data[4:])
        except ValueError:
            return
        settings.check_interval_min = new_min
        # Если автопроверка запущена — перезапустим с новым интервалом
        job_queue = context.application.job_queue
        if job_queue.get_jobs_by_name('anime_news_check'):
            for job in job_queue.get_jobs_by_name('anime_news_check'):
                job.schedule_removal()
            job_queue.run_repeating(
                check_news, interval=settings.check_interval_sec,
                first=5, name='anime_news_check', job_kwargs=JOB_KWARGS,
            )
            extra = " (автопроверка перезапущена)"
        else:
            extra = ""
        await query.answer(f"Интервал: {new_min} мин{extra}")
        await query.edit_message_text(
            f"🔁 Интервал автопроверки\n\nТекущий: {settings.check_interval_min} мин",
            reply_markup=build_interval_menu(),
        )
        return

    # === Переключение видео ===
    if data == "video:toggle":
        settings.video_enabled = not settings.video_enabled
        state = "включено 🟢" if settings.video_enabled else "выключено 🔴"
        await query.answer(f"Видео {state}")
        await query.edit_message_text(
            f"🎬 Скачивание видео\n\nСостояние: {state}",
            reply_markup=build_video_menu(),
        )
        return

    # === История ===
    if data == "hist:clear_confirm":
        await query.edit_message_text(
            f"⚠️ Очистить всю историю отправленных ссылок?\n\n"
            f"Сейчас в истории: {len(sent_links._set)}\n"
            f"После очистки бот может повторно опубликовать уже отправленные новости.",
            reply_markup=build_history_confirm_menu(),
        )
        return
    if data == "hist:clear_yes":
        async with sent_links._lock:
            sent_links._urls.clear()
            sent_links._url_set.clear()
            sent_links._title_set.clear()
            sent_links._save()
        await query.answer("История очищена")
        await query.edit_message_text(
            "✅ История ссылок очищена.",
            reply_markup=_menu_for(data),
        )
        return

    # === Очередь постов ===
    if data == "settings:queue":
        size = await post_queue.peek_size()
        titles = await post_queue.list_titles(limit=10)
        text = f"📦 Очередь постов\n\nВ очереди: {size}"
        if titles:
            text += "\n\nБлижайшие к отправке:\n"
            for i, t in enumerate(titles, 1):
                text += f"{i}. {t}\n"
        await query.edit_message_text(text, reply_markup=build_queue_menu())
        return

    if data == "queue:send_now":
        next_post = await post_queue.pop_next()
        if next_post is None:
            await query.answer("Очередь пуста", show_alert=True)
            size = await post_queue.peek_size()
            await query.edit_message_text(
                f"📦 Очередь постов\n\nВ очереди: {size}",
                reply_markup=build_queue_menu(),
            )
            return
        result = await send_news(context.bot, next_post)
        if result == 'sent':
            await query.answer("✅ Отправлено в канал")
        elif result == 'failed':
            # Реальная ошибка отправки — возвращаем в начало очереди
            async with post_queue._lock:
                post_queue._items.insert(0, {
                    'news': {k: v for k, v in next_post.items() if k != 'published_parsed'},
                    'queued_at': datetime.now().isoformat(),
                })
                post_queue._save()
            await query.answer("Не удалось отправить, пост возвращён в очередь", show_alert=True)
        else:
            # 'skipped_dup' или 'skipped_filter' — пост уже был отправлен или не подходит,
            # в очередь НЕ возвращаем
            await query.answer(f"Пост пропущен ({result})", show_alert=True)
        size = await post_queue.peek_size()
        titles = await post_queue.list_titles(limit=10)
        text = f"📦 Очередь постов\n\nВ очереди: {size}"
        if titles:
            text += "\n\nБлижайшие к отправке:\n"
            for i, t in enumerate(titles, 1):
                text += f"{i}. {t}\n"
        await query.edit_message_text(text, reply_markup=build_queue_menu())
        return

    if data == "queue:clear_confirm":
        size = await post_queue.peek_size()
        await query.edit_message_text(
            f"⚠️ Очистить всю очередь?\n\nВ очереди: {size} постов\n"
            f"После очистки эти посты не будут опубликованы.",
            reply_markup=build_queue_clear_confirm_menu(),
        )
        return

    if data == "queue:clear_yes":
        count = await post_queue.clear()
        await query.answer(f"Удалено {count} постов")
        await query.edit_message_text(
            f"✅ Очередь очищена ({count} постов).",
            reply_markup=_menu_for(data),
        )
        return


# ============== ОБРАБОТЧИК REPLY-КНОПОК ==============
async def reply_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перенаправляет нажатия reply-кнопок на соответствующие команды."""
    if not is_admin(update):
        await deny_access(update)
        return

    text = (update.message.text or "").strip()

    if text == BTN_NEWS:
        await news_command(update, context)
    elif text == BTN_PREVIEW:
        await preview_command(update, context)
    elif text == BTN_START_AUTO:
        await start_auto(update, context)
    elif text == BTN_STOP_AUTO:
        await stop_auto(update, context)
    elif text == BTN_STATUS:
        await status(update, context)
    elif text == BTN_SETTINGS:
        await update.message.reply_text(
            '⚙️ <b>Настройки</b>\n\nВыбери раздел.',
            reply_markup=build_settings_menu(),
            parse_mode=ParseMode.HTML,
        )


# ============== КОМАНДЫ ==============
def admin_only(handler):
    """Декоратор: пускаем в команду только админа."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await deny_access(update)
            return
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


def _await_ctx(mode: str, key: str, message) -> dict:
    """Запоминает, где именно бот ждёт ответ: чат + ветка форума.
    Без этого бот принимал за ответ любое сообщение админа в любой ветке."""
    return {
        'mode': mode,
        'key': key,
        'chat_id': message.chat_id,
        'thread_id': getattr(message, 'message_thread_id', None),
    }


def _same_place(pending: dict, update: Update) -> bool:
    """True, если сообщение пришло из того же чата и той же ветки, где нажали кнопку."""
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    thread_id = getattr(update.message, 'message_thread_id', None)
    return (pending.get('chat_id') == chat_id
            and pending.get('thread_id') == thread_id)


# Посторонние, уже получившие подсказку в ЛС (чтобы не спамить отказом)
_private_denied: set[int] = set()


async def private_gate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Личка бота — только для админов. Посторонний в ЛС получает одну короткую
    подсказку и дальше игнорируется. Кнопки в ветке при этом работают для всех
    (open_moderation) — это единственная точка входа для обычных участников."""
    chat = update.effective_chat
    if chat is None or chat.type != 'private':
        return                       # группы и ветки — не наша зона
    if is_admin(update):
        return                       # админам ЛС полностью доступна
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in _private_denied:
        if len(_private_denied) > 1000:      # не даём множеству расти бесконечно
            _private_denied.clear()
        _private_denied.add(uid)
        try:
            await update.message.reply_text(
                '⛔ Этот бот в личке доступен только администраторам.\n'
                'Кнопки под постами в ветке обсуждения работают для всех.')
        except TelegramError:
            pass
    raise ApplicationHandlerStop


async def awaiting_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит текст, который админ прислал после нажатия 📅 (время) или ✏️ (новый текст).
    Если бот ничего не ждёт — молча пропускает сообщение дальше другим обработчикам."""
    pending = context.user_data.get('await_input') if context.user_data else None
    if not pending:
        return
    # Права не проверяем: состояние await_input появляется только у того, кто
    # нажал кнопку в ветке (гейт места и open_moderation — там). Ввод принимаем
    # от того же пользователя в том же месте (_same_place ниже).
    # Отвечаем только там, где нажали кнопку: сообщения из других веток/чатов
    # пропускаем дальше, чтобы бот не влезал в чужие разговоры.
    if not _same_place(pending, update):
        return
    text = (update.message.text or '').strip()
    if not text:
        return

    key = pending.get('key')
    mode = pending.get('mode')

    # === 🕒 Перенос времени у поста, который уже в отложке ===
    if mode == 'reschedule':
        if scheduled_posts is None or scheduled_posts.get(key) is None:
            context.user_data.pop('await_input', None)
            await update.message.reply_text('Этого поста уже нет в отложке.')
            raise ApplicationHandlerStop
        when = _parse_schedule_time(text)
        if when is None:
            await update.message.reply_text(
                '⏰ Не понял время. Примеры: 18:30 • 12.09 18:30 • завтра 10:00 • +2ч\n'
                'Время должно быть в будущем. Отмена — /cancel')
            raise ApplicationHandlerStop
        scheduled_posts.reschedule(key, when)
        context.user_data.pop('await_input', None)
        logger.info(f"📅 Пост перенесён на {_fmt_local(when)}")
        await update.message.reply_text(
            f'🕒 Перенёс на {_fmt_local(when)} — через {_human_delta(when)}.\n'
            f'Список: /scheduled')
        raise ApplicationHandlerStop

    news = pending_posts.get(key) if pending_posts is not None else None
    if not news:
        context.user_data.pop('await_input', None)
        await update.message.reply_text('Пост уже обработан или устарел.')
        raise ApplicationHandlerStop

    # === 📅 Отложка: разбираем введённое время ===
    if mode == 'schedule':
        when = _parse_schedule_time(text)
        if when is None:
            # состояние не сбрасываем — ждём корректный ввод
            await update.message.reply_text(
                '⏰ Не понял время. Примеры: 18:30 • 12.07 18:30 • завтра 10:00 • +2ч\n'
                'Время должно быть в будущем. Отмена — /cancel')
            raise ApplicationHandlerStop
        if scheduled_posts is None:
            await update.message.reply_text('Отложка недоступна (хранилище не готово).')
            context.user_data.pop('await_input', None)
            raise ApplicationHandlerStop
        user = update.effective_user
        by = {'id': user.id,
              'name': (user.full_name or user.username or str(user.id))} if user else None
        scheduled_posts.add(news, when, by=by)
        context.user_data.pop('await_input', None)
        # Пометку ставим ДО pop: после удаления записи превью уже не найти
        await _update_moderation_done(context.bot, key,
                                      f'\n\n📅 В отложке на {_fmt_local(when)}')
        pending_posts.pop(key)
        logger.info(f"📅 Отложен пост «{news.get('title', '')[:60]}» на {_fmt_local(when)} "
                    f"(отложил: {(by or {}).get('name', '?')})")
        if user and user.id not in _all_admin_ids():
            await notify_admin(
                context.bot,
                f'👥 {(by or {}).get("name", "?")} отложил пост на {_fmt_local(when)}:\n\n'
                f'{_post_card(news, {"by": by, "at": when})}')
        await update.message.reply_text(
            f'📅 Опубликую {_fmt_local(when)} — через {_human_delta(when)}.\n'
            f'Список: /scheduled')
        raise ApplicationHandlerStop

    # === ✏️ Правка текста ===
    if mode == 'edit':
        news['_edited_text'] = text
        pending_posts.update_news(key, news)
        context.user_data.pop('await_input', None)
        editor = update.effective_user
        if editor and editor.id not in _all_admin_ids():
            ed_name = editor.full_name or editor.username or str(editor.id)
            await notify_admin(
                context.bot,
                f'👥 {ed_name} изменил текст поста в ветке:\n\n{fit_to_limit(text, 500)}')
        updated = await _update_preview_text(context.bot, key, text)
        msg = '✏️ Текст обновлён — в канал уйдёт именно он.'
        if not updated:
            msg += '\n(Сообщение в ветке обновить не вышло, но текст сохранён.)'
        await update.message.reply_text(msg)
        raise ApplicationHandlerStop

    context.user_data.pop('await_input', None)


async def _update_moderation_done(bot: Bot, key: str, suffix: str) -> None:
    """Дописывает пометку к посту в ветке и снимает кнопки (например, после отложки).
    Вызывать ДО удаления записи из pending_posts — иначе превью уже не найти."""
    if pending_posts is None:
        return
    prev = pending_posts.get_preview(key)
    news = pending_posts.get(key)
    if not prev or not news:
        return
    chat_id, message_id = prev.get('chat_id'), prev.get('message_id')
    body = fit_to_limit(html.escape(format_news_short(news) + suffix), TG_CAPTION_LIMIT)
    attempts = (
        lambda: bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                         caption=body, parse_mode=ParseMode.HTML,
                                         reply_markup=None),
        lambda: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=body,
                                      parse_mode=ParseMode.HTML, reply_markup=None),
        # Крайний случай: хотя бы убрать кнопки, чтобы их нельзя было нажать повторно
        lambda: bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id,
                                              reply_markup=None),
    )
    for attempt in attempts:
        try:
            await attempt()
            return
        except Exception as e:      # best-effort: пометка не критична для отложки
            logger.debug(f"пометка на посте {key} не поставлена: {e}")
            continue


async def cancel_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет ожидание ввода (времени отложки или нового текста).
    Доступна всем: гость отменяет только своё собственное состояние."""
    if context.user_data and context.user_data.pop('await_input', None):
        await update.message.reply_text('Отменил. Пост остался в ветке с кнопками.')
    else:
        await update.message.reply_text('Нечего отменять.')


@admin_only
async def tz_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает/меняет часовой пояс для отложки."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            f'🕒 Часовой пояс: UTC{_tz_offset():+d}\n'
            f'Сейчас у тебя: {_local_now().strftime("%d.%m %H:%M")}\n\n'
            f'Если время неверное — задай смещение: /tz 3 (Москва), /tz 5 (Екатеринбург)')
        return
    raw = args[0].replace('UTC', '').replace('utc', '').strip()
    try:
        off = int(raw)
    except ValueError:
        await update.message.reply_text('Формат: /tz 3 (смещение от UTC в часах)')
        return
    if not (-12 <= off <= 14):
        await update.message.reply_text('Смещение должно быть от -12 до +14.')
        return
    settings.tz_offset = off
    await update.message.reply_text(
        f'🕒 Часовой пояс: UTC{off:+d}\nСейчас у тебя: {_local_now().strftime("%d.%m %H:%M")}')


_RESCHEDULE_HINT = (
    '🕒 <b>На какое время перенести?</b>\n'
    'Сейчас стоит: {old}\n\n'
    'Ответь сообщением, например:\n'
    '• <code>18:30</code>\n'
    '• <code>12.09 18:30</code>\n'
    '• <code>завтра 10:00</code>\n'
    '• <code>+2ч</code>\n\n'
    'У тебя {now} (UTC{off:+d}). Отмена — /cancel'
)


async def _safe_edit(query, text: str, markup) -> None:
    """Меняет сообщение обзора. Telegram ругается, если текст не изменился —
    для нас это не ошибка."""
    try:
        await query.edit_message_text(text, reply_markup=markup,
                                      parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
    except TelegramError as e:
        if 'not modified' not in str(e).lower():
            logger.debug(f"обзор отложки не обновился: {e}")


SCHEDULED_LIST_MAX = 25         # столько постов показываем в обзоре


def _day_label(when_utc: datetime) -> str:
    """«Сегодня», «Завтра» или «17 августа» — по времени админа."""
    local = _utc_to_local(when_utc).date()
    today = _local_now().date()
    delta = (local - today).days
    if delta == 0:
        return 'Сегодня'
    if delta == 1:
        return 'Завтра'
    if delta == -1:
        return 'Вчера (просрочено)'
    if delta < 0:
        return 'Просрочено'
    months = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
              'августа', 'сентября', 'октября', 'ноября', 'декабря')
    return f'{local.day} {months[local.month - 1]}'


def _short_title(news: dict, limit: int = 46) -> str:
    """Короткий заголовок поста для списка."""
    text = news.get('_llm_text') or news.get('_edited_text') or news.get('title') or ''
    text = re.sub(r'\s+', ' ', text.split('\n')[0]).strip()
    return (text[:limit] + '…') if len(text) > limit else (text or 'без заголовка')


def _scheduled_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Весь список одним сообщением: время, заголовок, кто отложил.

    Раньше на каждый пост уходило отдельное сообщение — двадцать постов
    превращались в двадцать уведомлений, и очередь целиком было не окинуть
    взглядом. Теперь обзор компактный, а подробности открываются по номеру."""
    items = scheduled_posts.all() if scheduled_posts is not None else []
    if not items:
        return ('📅 Отложенных постов нет.\n\n'
                'Отложить: кнопка «📅 В отложку» под постом в ветке.'), None

    now = datetime.now(timezone.utc)
    ripe = sum(1 for _k, _n, when in items if when <= now)
    head = f'📅 <b>В отложке: {len(items)}</b>'
    if ripe:
        head += f' · {ripe} ждёт публикации'
    lines = [head, f'<i>время в UTC{_tz_offset():+d}</i>', '']

    buttons, row = [], []
    current_day = None
    for number, (key, news, when) in enumerate(items[:SCHEDULED_LIST_MAX], 1):
        day = _day_label(when)
        if day != current_day:
            if current_day is not None:
                lines.append('')
            lines.append(f'<b>{day}</b>')
            current_day = day
        meta = scheduled_posts.meta(key)
        who = (meta.get('by') or {}).get('name', '')
        when_local = _utc_to_local(when).strftime('%H:%M')
        mark = '⏳' if when <= now else '🕒'
        tail = f' · через {_human_delta(when)}' if when > now else ' · пора'
        line = f'{number}. {mark} {when_local}{tail}\n    {html.escape(_short_title(news))}'
        if who:
            line += f'\n    👤 {html.escape(who[:24])}'
        tries = meta.get('tries') or 0
        if tries:
            line += f' · ⚠️ попыток: {tries}'
        lines.append(line)

        row.append(InlineKeyboardButton(str(number), callback_data=f'sview:{key}'))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if len(items) > SCHEDULED_LIST_MAX:
        lines.append('')
        lines.append(f'…и ещё {len(items) - SCHEDULED_LIST_MAX}')
    lines.append('')
    lines.append('Нажми номер, чтобы открыть пост целиком.')
    buttons.append([InlineKeyboardButton('🔄 Обновить', callback_data='slist'),
                    InlineKeyboardButton('🗑 Очистить всё', callback_data='sclear')])
    return fit_to_limit('\n'.join(lines), TG_TEXT_LIMIT), InlineKeyboardMarkup(buttons)


def _scheduled_detail(key: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Карточка одного отложенного поста с действиями."""
    news = scheduled_posts.get(key) if scheduled_posts is not None else None
    if not news:
        return 'Этого поста уже нет в отложке.', InlineKeyboardMarkup(
            [[InlineKeyboardButton('⬅️ К списку', callback_data='slist')]])
    meta = scheduled_posts.meta(key)
    card = _post_card(news, meta, countdown=True, with_body=True)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton('📢 Сейчас', callback_data=f'snow:{key}'),
         InlineKeyboardButton('🕒 Перенести', callback_data=f'sedit:{key}')],
        [InlineKeyboardButton('🗑 Отменить', callback_data=f'scan:{key}'),
         InlineKeyboardButton('⬅️ К списку', callback_data='slist')],
    ])
    return fit_to_limit(card, TG_TEXT_LIMIT), markup


@admin_only
async def scheduled_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Очередь отложенных постов: обзор одним сообщением."""
    text, markup = _scheduled_overview()
    await update.message.reply_text(text, reply_markup=markup,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


def _media_summary(news: dict) -> str:
    """Что уйдёт вместе с текстом: '4 фото + видео'.
    Считаем после дедупа размерных вариантов — как при реальной отправке."""
    photos = len(_dedup_image_variants(news.get('images') or []))
    bits = []
    if photos:
        bits.append(f'{photos} фото')
    if news.get('video'):
        bits.append('видео')
    return ' + '.join(bits) if bits else 'нет'


def _post_card(news: dict, meta: dict, *, countdown: bool = False,
               with_body: bool = False) -> str:
    """Карточка поста: что уйдёт, откуда, кто отложил, когда, с каким медиа.

    with_body — показать текст, который реально уйдёт в канал (для /scheduled:
                там подробности нужны ДО публикации, а не после).
    countdown — добавить «через N» к времени публикации."""
    lines = []
    if with_body:
        try:
            body = format_news_short(news).strip()
        except Exception as e:
            logger.debug(f"карточка: текст не собрался ({e})")
            body = (news.get('_edited_text') or news.get('title') or '')
        lines.append(fit_to_limit(body, 600))
        lines.append('')
    else:
        title = re.sub(r'\s+', ' ',
                       (news.get('_edited_text') or news.get('title') or '')).strip()
        lines.append(f'📝 {title[:200]}')
    if news.get('source'):
        lines.append(f'📡 Источник: {news["source"]}')
    who = (meta.get('by') or {}).get('name')
    if who:
        lines.append(f'👤 Отложил: {who}')
    at = meta.get('at')
    if at:
        line = f'🕒 Публикация: {_fmt_local(at)}'
        if countdown:
            if at <= datetime.now(timezone.utc):
                line += ' — ⏳ время наступило, публикуется'
            else:
                line += f' — через {_human_delta(at)}'
        lines.append(line)
    lines.append(f'📎 Медиа: {_media_summary(news)}')
    if news.get('link'):
        lines.append(f'🔗 {news["link"]}')
    if news.get('_edited_text'):
        lines.append('✏️ Текст правился вручную')
    tries = meta.get('tries') or 0
    if tries:
        lines.append(f'⚠️ Неудачных попыток: {tries}')
    return '\n'.join(lines)


_sched_tick_count = 0


async def publish_scheduled(context: ContextTypes.DEFAULT_TYPE):
    """Публикует отложенные посты, время которых наступило. Работает раз в минуту.

    Любая ошибка внутри ловится и уходит админу: раньше исключение молча убивало
    джоб, и посты навсегда зависали в отложке без единого сообщения."""
    global _sched_tick_count
    _sched_tick_count += 1
    # Пульс в лог: первый тик и далее каждые полчаса — видно, что джоб живёт
    if _sched_tick_count == 1 or _sched_tick_count % 30 == 0:
        total = len(scheduled_posts.all()) if scheduled_posts is not None else -1
        logger.info(f"🕰 Джоб отложки: тик #{_sched_tick_count}, постов в очереди: {total}")
    if scheduled_posts is None:
        logger.warning("Отложка: хранилище не инициализировано, пропускаю тик")
        return
    total = len(scheduled_posts.all())
    due = scheduled_posts.due()
    if not due:
        if total:
            logger.debug(f"Отложка: {total} постов, ни один ещё не созрел")
        return
    logger.info(f"📅 Отложка: {len(due)} из {total} постов пора публиковать")

    for key, news in due:
        meta = scheduled_posts.meta(key)      # ДО pop — потом данных не будет
        card = _post_card(news, meta)
        guard = _PublishGuard(f'sched:{key}')
        with guard:
            if not guard.acquired:
                logger.info(f"Пост уже публикуется вручную, пропускаю тик: {key}")
                continue
            try:
                ok = await _send_post(context.bot, news, CHANNEL_ID, None)
                err = None
            except Exception as e:            # не даём джобу умереть молча
                ok = False
                err = f'{type(e).__name__}: {e}'
                logger.exception(
                    f"Отложенный пост упал с ошибкой: {news.get('title', '')[:60]}")

        if ok:
            scheduled_posts.pop(key)
            _mark_published()
            logger.info(f"📅 Опубликован отложенный пост: {news.get('title', '')[:60]}")
            await notify_admin(
                context.bot,
                f'📅 Опубликован отложенный пост\n\n{card}\n\n'
                f'✅ Ушёл в канал {_fmt_local(datetime.now(timezone.utc))}')
        else:
            tries = scheduled_posts.mark_try(key)
            reason = err or 'отправка вернула отказ (см. /logs)'
            if tries >= ScheduledPosts.MAX_TRIES:
                scheduled_posts.pop(key)
                await notify_admin(
                    context.bot,
                    f'⚠️ Отложенный пост снят после {tries} неудачных попыток\n\n'
                    f'{card}\n\n❌ Причина: {reason}')
            else:
                logger.warning(f"Отложенный пост не ушёл (попытка {tries}/"
                               f"{ScheduledPosts.MAX_TRIES}): {reason}")
                await notify_admin(
                    context.bot,
                    f'⚠️ Отложенный пост не опубликовался '
                    f'(попытка {tries}/{ScheduledPosts.MAX_TRIES}, повторю через минуту)\n\n'
                    f'{card}\n\n❌ Причина: {reason}')
        await asyncio.sleep(PAUSE_BETWEEN_SENDS)


@admin_only
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я аниме-новостной бот.\n\n"
        "Используй кнопки внизу или команды:\n"
        "/news — свежие новости\n"
        "/preview — превью постов в личку\n"
        "/start_auto — включить авторассылку\n"
        "/stop_auto — выключить авторассылку\n"
        "/status — статус бота\n"
        "/settings — настройки",
        reply_markup=REPLY_KEYBOARD,
    )


@admin_only
async def settings_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /settings — открыть inline-меню настроек."""
    await update.message.reply_text(
        '⚙️ <b>Настройки</b>\n\nВыбери раздел.',
        reply_markup=build_settings_menu(),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def news_command(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Ищу новости...")
    all_news, stats, _ = await collect_all_news()
    filtered = [
        n for n in all_news
        if matches_keywords(n)
        and n['link'] not in sent_links
        and not sent_links.has_title(n.get('title', ''))
    ]
    if not filtered:
        await msg.edit_text(f"Новых новостей нет.\n\n📊 {' | '.join(stats)}")
        return
    sent = 0
    for news in filtered[:7]:
        result = await send_news(context.bot, news, chat_id=update.effective_chat.id)
        if result == 'sent':
            sent += 1
        await asyncio.sleep(1)
    await msg.edit_text(f"Готово, отправлено: {sent}")


@admin_only
async def preview_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Показать примеры в личку — точно так же как они будут в канале,
    но без публикации в канал и без записи в историю."""
    msg = await update.message.reply_text("🔍 Собираю примеры (видео может качаться долго)...")
    all_news, _, _ = await collect_all_news()
    if not all_news:
        await msg.edit_text("Нет новостей для превью.")
        return
    await msg.edit_text(f"Превью {min(5, len(all_news))} постов (как они будут в канале):")

    chat_id = update.effective_chat.id
    for news in all_news[:5]:
        video_file = None
        if news.get('video'):
            video_file = await _prepare_video_file(news)
        try:
            await _send_post(context.bot, news, chat_id, video_file)
        except Exception as e:
            logger.error(f"Preview ошибка: {e}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f'⚠️ Ошибка для одного из постов: {e}',
                )
            except TelegramError:
                pass
        finally:
            if video_file:
                try:
                    video_file.unlink(missing_ok=True)
                except Exception:
                    pass
        await asyncio.sleep(0.5)


# Гарантия что одновременно идёт максимум одна проверка новостей
_check_news_lock = asyncio.Lock()


def _find_silent_sources(hours: int = 72) -> list[str]:
    """Включённые источники, которые давно (hours+) ничего не отдавали.
    Источники без единой записи в статистике не трогаем (новые, не шумим)."""
    silent: list[str] = []
    cutoff = datetime.now() - timedelta(hours=hours)
    by_source = stats.get_by_source()
    for name, _fn in SOURCES:
        if not settings.is_source_enabled(name):
            continue
        entry = by_source.get(name)
        if not entry:
            continue
        last = entry.get('last_success_at')
        if not last:
            silent.append(name)
            continue
        try:
            if datetime.fromisoformat(last) < cutoff:
                silent.append(name)
        except (ValueError, TypeError):
            continue
    return silent


async def _maybe_send_daily_summary(bot: Bot) -> None:
    """В тихом режиме шлёт админу одну сводку в день (при первой проверке нового дня)."""
    if not settings.quiet_mode:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    if settings.last_daily_summary == today:
        return
    settings.last_daily_summary = today
    day_ago = datetime.now() - timedelta(days=1)
    published = stats.count_events_since(day_ago, 'published')
    failed = stats.count_events_since(day_ago, 'failed_send')
    queue_size = await post_queue.peek_size()
    silent = _find_silent_sources(hours=72)
    silent_line = f"\n🔇 Молчат 3+ дня: {', '.join(silent)}" if silent else ""
    await notify_admin(
        bot,
        f"📅 Ежедневная сводка\n"
        f"📤 Опубликовано за 24ч: {published}\n"
        f"⚠️ Ошибок отправки: {failed}\n"
        f"📦 В очереди: {queue_size}{silent_line}\n\n"
        f"Подробнее: /stats  •  Настройки: /settings",
    )


async def check_news(context: ContextTypes.DEFAULT_TYPE):
    if _check_news_lock.locked():
        logger.info("⏭ Пропускаю автопроверку — предыдущая ещё идёт")
        return
    async with _check_news_lock:
        logger.info("🔁 Автопроверка новостей...")
        cleanup_video_dir()
        # В тихом режиме не спамим "начинаю проверку" каждые полчаса
        if not settings.quiet_mode:
            await notify_admin(context.bot, "🔍 Начинаю проверку новостей...")

        # 1) Собираем свежие новости с источников
        all_news, stats_lines, errors = await collect_all_news()

        _image_bytes_cache.clear()      # цикл закончился, картинки больше не нужны

        # Накопленные предупреждения (квота переводчика и т.п.)
        while _pending_admin_alerts:
            await notify_admin(context.bot, _pending_admin_alerts.pop(0))

        # Источники, которые бот выключил сам — сообщаем админам один раз
        while _auto_disabled_pending:
            src_name, reason = _auto_disabled_pending.pop(0)
            await notify_admin(
                context.bot,
                f'⏸ Источник «{src_name}» выключен автоматически\n\n'
                f'Больше {AUTO_DISABLE_AFTER_HOURS} ч не отдаёт новостей.\n'
                f'{reason[:180]}\n\n'
                f'Включить обратно: /settings → 📡 Источники')
        # Только то, что подходит по фильтру и не было отправлено ранее
        fresh = [
            n for n in all_news
            if matches_keywords(n)
            and n['link'] not in sent_links
            and not sent_links.has_title(n.get('title', ''))
        ]

        # === РЕЖИМ ВЕТКИ: шлём ВСЁ найденное пачкой в тему обсуждения ===
        if settings.thread_mode:
            sent_count = 0
            failed_count = 0
            skipped_count = 0
            for news in fresh:
                result = await send_news_to_thread(context.bot, news)
                if result == 'sent':
                    sent_count += 1
                elif result == 'failed':
                    failed_count += 1
                else:
                    skipped_count += 1
                # Пауза между отправками чтобы не словить флуд-лимит Telegram
                await asyncio.sleep(PAUSE_BETWEEN_SENDS)

            has_problems = bool(errors) or failed_count > 0
            # В тихом режиме отчёт — только если были проблемы
            if not settings.quiet_mode or has_problems:
                message = (
                    f"✅ Проверка завершена (режим ветки).\n"
                    f"📊 Источники: {' | '.join(stats_lines)}\n"
                    f"🧵 Отправлено в ветку: {sent_count}\n"
                )
                if failed_count:
                    message += f"⚠️ Не удалось отправить: {failed_count}\n"
                if errors:
                    message += "⚠️ Ошибки источников:\n" + "\n".join(errors)
                await notify_admin(context.bot, message)
            await _check_silence(context.bot)
            await _maybe_send_daily_summary(context.bot)
            return

        # === РЕЖИМ КАНАЛА (старый): по 1 посту за интервал через очередь ===
        # 2) Кладём в очередь (push_many сам отсеит то, что уже там лежит)
        added_to_queue = await post_queue.push_many(fresh)

        # 3) Достаём ОДИН пост из очереди и отправляем в канал.
        sent_result = None
        post_attempted = None
        for _attempt in range(5):  # макс 5 попыток за один tick
            next_post = await post_queue.pop_next()
            if next_post is None:
                break
            post_attempted = next_post
            sent_result = await send_news(context.bot, next_post)
            if sent_result == 'sent':
                break
            if sent_result == 'failed':
                async with post_queue._lock:
                    post_queue._items.insert(0, {
                        'news': {k: v for k, v in next_post.items() if k != 'published_parsed'},
                        'queued_at': datetime.now().isoformat(),
                    })
                    post_queue._save()
                logger.warning(f"Возвращаю пост в очередь после ошибки отправки: {next_post.get('title', '')[:60]}")
                break
            logger.info(f"Пост из очереди пропущен ({sent_result}): {next_post.get('title', '')[:60]}")

        sent_ok = (sent_result == 'sent')
        queue_size = await post_queue.peek_size()

        has_problems = bool(errors) or sent_result == 'failed'
        # В тихом режиме отчёт — только если были проблемы
        if not settings.quiet_mode or has_problems:
            message = (
                f"✅ Проверка завершена.\n"
                f"📊 Источники: {' | '.join(stats_lines)}\n"
                f"➕ Новых в очереди: {added_to_queue}\n"
                f"📤 Отправлено в канал: {1 if sent_ok else 0}\n"
                f"📦 Осталось в очереди: {queue_size}"
            )
            if errors:
                message += "\n⚠️ Ошибки:\n" + "\n".join(errors)
            await notify_admin(context.bot, message)
        await _maybe_send_daily_summary(context.bot)


@admin_only
async def start_auto(update, context: ContextTypes.DEFAULT_TYPE):
    job_queue = context.application.job_queue
    if job_queue.get_jobs_by_name('anime_news_check'):
        await update.message.reply_text("Авторассылка уже работает.")
        return
    interval = settings.check_interval_sec
    job_queue.run_repeating(
        check_news, interval=interval, first=5, name='anime_news_check',
        job_kwargs=JOB_KWARGS,
    )
    await update.message.reply_text(
        f"✅ Авторассылка включена (каждые {settings.check_interval_min} минут)."
    )
    await notify_admin(context.bot, "🚀 Авторассылка запущена.")


@admin_only
async def stop_auto(update, context: ContextTypes.DEFAULT_TYPE):
    job_queue = context.application.job_queue
    jobs = job_queue.get_jobs_by_name('anime_news_check')
    if not jobs:
        await update.message.reply_text("Авторассылка не была запущена.")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("⏸ Авторассылка остановлена.")
    await notify_admin(context.bot, "🛑 Авторассылка остановлена.")


@admin_only
async def chatinfo_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика: показывает ID текущего чата и ID темы (если вызвано в теме форума).
    Вызови эту команду ВНУТРИ нужной ветки обсуждения, чтобы узнать куда настраивать отправку."""
    msg = update.message
    chat = update.effective_chat

    chat_id = chat.id
    chat_type = chat.type
    chat_title = getattr(chat, 'title', None) or '(без названия)'

    # message_thread_id есть только если сообщение в теме форума
    thread_id = getattr(msg, 'message_thread_id', None)
    is_topic = getattr(msg, 'is_topic_message', False)

    lines = [
        '🔍 <b>Информация о чате</b>',
        '',
        f'<b>Chat ID:</b> <code>{chat_id}</code>',
        f'<b>Тип:</b> {chat_type}',
        f'<b>Название:</b> {html.escape(chat_title)}',
    ]
    if thread_id is not None:
        lines.append(f'<b>Thread ID (тема):</b> <code>{thread_id}</code>')
        lines.append(f'<b>Это сообщение в теме:</b> {"да" if is_topic else "нет"}')
        lines.append('')
        lines.append('✅ Это ветка форума. Для настройки отправки сюда мне нужны:')
        lines.append(f'  • Chat ID: <code>{chat_id}</code>')
        lines.append(f'  • Thread ID: <code>{thread_id}</code>')
    else:
        lines.append('')
        lines.append('⚠️ Это НЕ тема форума (обычный чат или личка).')
        lines.append('Если хочешь отправку в ветку — вызови /chatinfo внутри нужной темы группы обсуждения.')

    await msg.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def status(update, context: ContextTypes.DEFAULT_TYPE):
    job_queue = context.application.job_queue
    is_running = bool(job_queue.get_jobs_by_name('anime_news_check'))
    sources_list = '\n'.join(
        f'  {"🟢" if settings.is_source_enabled(name) else "🔴"} {name}'
        for name, _ in SOURCES
    )
    yt_status = '🟢 готов' if YT_DLP_AVAILABLE else '🔴 не установлен'
    ffmpeg_status = '🟢 найден' if shutil.which('ffmpeg') else '🟡 не найден'
    video_state = '🟢 включено' if settings.video_enabled else '🔴 выключено'
    if settings.translator_engine == 'google':
        translator_name = 'Google Translate (выбран вручную)'
    elif DEEPL_API_KEY:
        translator_name = 'DeepL 🟢'
    else:
        translator_name = 'Google Translate (ключ DeepL не задан)'
    queue_size = await post_queue.peek_size()
    await update.message.reply_text(
        f"Авторассылка: {'🟢 включена' if is_running else '🔴 выключена'}\n"
        f"Интервал: {settings.check_interval_min} мин (1 пост за интервал)\n"
        f"🧵 Режим ветки: {'ВКЛ (всё в ветку)' if settings.thread_mode else 'ВЫКЛ (по 1 в канал)'}\n"
        f"🌐 Переводчик: {translator_name}\n"
        f"⏰ Свежесть постов: {settings.post_max_age_hours} ч\n"
        f"🖼 Только с картинками: {'ВКЛ' if settings.require_image else 'ВЫКЛ'}\n"
        f"📦 В очереди: {queue_size}\n"
        f"{_scheduled_status_block(context)}"
        f"В истории ссылок: {len(sent_links._set)}\n"
        f"Канал: {CHANNEL_ID}\n"
        f"Скачивание видео: {video_state}\n"
        f"yt-dlp: {yt_status}\n"
        f"ffmpeg: {ffmpeg_status}\n\n"
        f"📡 Источники:\n{sources_list}"
    )


def _scheduled_status_block(context) -> str:
    """Строки про отложку для /status: жив ли джоб, когда следующий тик, очередь.
    Главный инструмент самодиагностики «почему отложка молчит»."""
    try:
        jobs = context.application.job_queue.get_jobs_by_name('scheduled_publish')
    except Exception:
        jobs = []
    if jobs:
        nxt = getattr(jobs[0], 'next_t', None)
        when = f", следующий тик {_fmt_local(nxt)}" if nxt else ""
        job_line = f"🕰 Джоб отложки: РАБОТАЕТ (тик #{_sched_tick_count}{when})"
    else:
        job_line = "🕰 Джоб отложки: ⚠️ НЕ ЗАРЕГИСТРИРОВАН — отложка публиковаться не будет!"
    total = len(scheduled_posts.all()) if scheduled_posts is not None else 0
    ripe = len(scheduled_posts.due()) if scheduled_posts is not None else 0
    sched_line = f"📅 В отложке: {total}"
    if ripe:
        sched_line += f" (созрело и ждёт публикации: {ripe})"
    return f"{job_line}\n{sched_line}\n"


# ============== ЯЗЫКОВАЯ МОДЕЛЬ (перевод, текст, фильтр, теги) ==============
# Работаем через формат OpenAI chat/completions — его понимают Mistral, Groq,
# Gemini, OpenRouter, Cerebras, NVIDIA и почти все остальные. Поэтому смена
# провайдера — это две переменные окружения, а не правка кода.
#
# Настройка на хостинге:
#   LLM_PROVIDER=mistral   (или groq / gemini / openrouter / nvidia / cerebras)
#   LLM_API_KEY=<ключ>
# Необязательно: LLM_MODEL, LLM_BASE_URL — если хочется другую модель/адрес.

LLM_PRESETS = {
    'mistral':    ('https://api.mistral.ai/v1', 'mistral-small-latest'),
    'groq':       ('https://api.groq.com/openai/v1', 'llama-3.3-70b-versatile'),
    'gemini':     ('https://generativelanguage.googleapis.com/v1beta/openai',
                   'gemini-2.0-flash'),
    'openrouter': ('https://openrouter.ai/api/v1', 'google/gemma-3-27b-it:free'),
    'nvidia':     ('https://integrate.api.nvidia.com/v1', 'meta/llama-3.3-70b-instruct'),
    'cerebras':   ('https://api.cerebras.ai/v1', 'llama-3.3-70b'),
}

LLM_PROVIDER = _env('LLM_PROVIDER', '').strip().lower()
LLM_API_KEY = _env('LLM_API_KEY', '').strip()
_preset = LLM_PRESETS.get(LLM_PROVIDER, ('', ''))
LLM_BASE_URL = (_env('LLM_BASE_URL', '').strip() or _preset[0]).rstrip('/')
LLM_MODEL = _env('LLM_MODEL', '').strip() or _preset[1]
LLM_TIMEOUT = _env_int('LLM_TIMEOUT', 30)
LLM_MIN_INTERVAL = float(_env('LLM_MIN_INTERVAL', '1.2'))   # сек между запросами
LLM_DAILY_LIMIT = _env_int('LLM_DAILY_LIMIT', 900)          # страховка от лимитов
LLM_MAX_TOKENS = _env_int('LLM_MAX_TOKENS', 700)


def _llm_extra_params() -> dict:
    """Необязательные параметры запроса из LLM_EXTRA_PARAMS (JSON-строка).
    Нужны для моделей с режимом рассуждений: например
    LLM_EXTRA_PARAMS={"reasoning_effort":"none"} у Mistral Small 4 —
    иначе модель тратит лимит токенов на размышления, и JSON не долетает."""
    raw = _env('LLM_EXTRA_PARAMS', '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        logger.warning('LLM_EXTRA_PARAMS: не разобрался как JSON — игнорирую')
        return {}

_llm_lock = asyncio.Lock()          # запросы строго по одному (лимит req/sec)
_llm_last_call = 0.0
_llm_fail_streak = 0                # подряд неудачных вызовов
LLM_FAIL_PAUSE_AFTER = 5            # столько провалов подряд → пауза до рестарта
_llm_disabled_runtime = False       # выключено на лету из-за ошибок/лимита
_llm_json_mode = True               # просить строгий JSON (снимаем, если провайдер против)


def _llm_configured() -> bool:
    """Заданы ли ключ, адрес и модель."""
    return bool(LLM_API_KEY and LLM_BASE_URL and LLM_MODEL)


def _llm_active() -> bool:
    """Можно ли прямо сейчас обращаться к модели."""
    if not _llm_configured() or _llm_disabled_runtime:
        return False
    return bool(settings is not None and settings.llm_enabled)


def _llm_quota_left() -> int:
    """Сколько вызовов осталось на сегодня по нашему счётчику."""
    if settings is None:
        return 0
    today = _local_now().strftime('%Y-%m-%d')
    used = settings.llm_calls_today if settings.llm_day == today else 0
    return max(0, LLM_DAILY_LIMIT - used)


def _llm_count_call() -> None:
    """Прибавляет вызов к дневному счётчику (и обнуляет его в новые сутки)."""
    if settings is None:
        return
    today = _local_now().strftime('%Y-%m-%d')
    if settings.llm_day != today:
        settings.llm_day = today
        settings.llm_calls_today = 0
    settings.llm_calls_today = settings.llm_calls_today + 1


def _llm_request(messages: list, max_tokens: int = LLM_MAX_TOKENS) -> Optional[str]:
    """Синхронный запрос к модели. Возвращает текст ответа или None.

    Никаких исключений наружу: если модель недоступна, бот обязан продолжить
    работать по-старому — через DeepL/Google и обычное форматирование."""
    global _llm_fail_streak, _llm_disabled_runtime, _llm_json_mode
    payload = {
        'model': LLM_MODEL,
        'messages': messages,
        'temperature': 0.2,           # факты важнее фантазии
        'max_tokens': max_tokens,
        **_llm_extra_params(),
    }
    # Строгий JSON поддерживают Mistral, Groq, OpenAI и большинство совместимых.
    # Если провайдер параметр не понял — снимаем его и дальше работаем без него.
    if _llm_json_mode:
        payload.setdefault('response_format', {'type': 'json_object'})
    try:
        r = requests.post(
            f'{LLM_BASE_URL}/chat/completions',
            headers={'Authorization': f'Bearer {LLM_API_KEY}',
                     'Content-Type': 'application/json'},
            json=payload,
            timeout=LLM_TIMEOUT,
        )
    except Exception as e:
        _llm_fail_streak += 1
        logger.warning(f"LLM: запрос не удался ({type(e).__name__}: {e})")
        return None

    if r.status_code == 429:
        _llm_fail_streak += 1
        logger.warning("LLM: провайдер вернул 429 (лимит запросов) — притормаживаю")
        return None
    if r.status_code in (401, 403):
        _llm_disabled_runtime = True
        logger.error(f"LLM: ключ отклонён (HTTP {r.status_code}) — выключаю до рестарта")
        _queue_admin_alert('🤖 Языковая модель отключена: провайдер не принял ключ '
                           f'(HTTP {r.status_code}). Проверь LLM_API_KEY. '
                           'Бот продолжает работать на DeepL/Google.')
        return None
    if r.status_code in (400, 422) and _llm_json_mode:
        # Скорее всего провайдер не знает response_format — пробуем без него
        _llm_json_mode = False
        logger.info("LLM: провайдер не принял строгий JSON — повторяю без него")
        return _llm_request(messages, max_tokens)
    if r.status_code != 200:
        _llm_fail_streak += 1
        logger.warning(f"LLM: HTTP {r.status_code} — {r.text[:150]}")
        return None

    try:
        data = r.json()
        content = data['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as e:
        _llm_fail_streak += 1
        logger.warning(f"LLM: непонятный ответ ({e})")
        return None

    _llm_fail_streak = 0
    return (content or '').strip()


async def _llm_call(messages: list, max_tokens: int = LLM_MAX_TOKENS) -> Optional[str]:
    """Вызов модели с соблюдением лимитов: по одному запросу за раз,
    с паузой между ними и дневным потолком."""
    global _llm_last_call, _llm_disabled_runtime
    if not _llm_active():
        return None
    if _llm_quota_left() <= 0:
        if not _llm_disabled_runtime:
            logger.info(f"LLM: дневной лимит {LLM_DAILY_LIMIT} исчерпан — "
                        f"до завтра работаю без модели")
        return None
    if _llm_fail_streak >= LLM_FAIL_PAUSE_AFTER:
        if not _llm_disabled_runtime:
            _llm_disabled_runtime = True
            logger.error(f"LLM: {_llm_fail_streak} ошибок подряд — выключаю до рестарта")
            _queue_admin_alert('🤖 Языковая модель отключена: слишком много ошибок подряд. '
                               'Бот продолжает работать на DeepL/Google. Подробности — /llm')
        return None

    async with _llm_lock:
        wait = LLM_MIN_INTERVAL - (time.time() - _llm_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _llm_count_call()
        result = await asyncio.to_thread(_llm_request, messages, max_tokens)
        _llm_last_call = time.time()
    return result


def _llm_parse_json(raw: str) -> Optional[dict]:
    """Достаёт JSON из ответа модели (та любит обрамлять его ```json)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r'\{.*\}', text, re.S)      # вдруг вокруг есть болтовня
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


LLM_TOPICS_OK = ('аниме', 'манга', 'игры', 'кино', 'комиксы')
# Типы материалов. Подборки и колонки — это SEO-наполнитель, а не новость.
LLM_KINDS_NEWS = ('новость', 'анонс', 'трейлер', 'релиз', 'слух')
LLM_KINDS_FILLER = ('подборка', 'обзор', 'мнение')
LLM_TOPIC_ANY = LLM_TOPICS_OK + ('прочее',)

LLM_SYSTEM_PROMPT = (
    'Ты — редактор русскоязычного Telegram-канала об аниме, манге, играх, кино '
    'и гик-культуре. Из сырой новости делаешь готовый пост.\n'
    'Ответ — ТОЛЬКО JSON, без markdown и пояснений:\n'
    '{"topic":"аниме|манга|игры|кино|комиксы|прочее",'
    '"kind":"новость|анонс|трейлер|релиз|слух|подборка|обзор|мнение",'
    '"subject":"название тайтла или франшизы",'
    '"title":"...","summary":"...","tags":["#тег"]}\n\n'

    'ГЛАВНОЕ: пост должен читаться сам по себе. После него у человека не должно '
    'остаться вопросов «что это за тайтл?», «когда?», «где смотреть?», '
    '«кто делает?» — если ответ есть в исходном тексте, он обязан быть в посте.\n\n'

    'title — одна фраза с сутью новости. На русском, без эмодзи и кликбейта.\n\n'

    'summary — 2-3 коротких абзаца, разделённых пустой строкой (\\n\\n):\n'
    '  1) Что именно произошло, с конкретикой: дата, платформа, студия, '
    'номер сезона или части, количество серий.\n'
    '  2) Что это значит или чего ждать дальше: когда премьера, что уже известно, '
    'как связано с предыдущими частями.\n'
    '  3) Нужен, только если без него непонятно: одно предложение о самом '
    'произведении — что это, из какого оно первоисточника, чем известно.\n'
    'Если фактов хватает на один абзац — пиши один. Пустые абзацы ради объёма '
    'не нужны. Всего не больше 650 символов.\n\n'

    'tags — 1-3 хэштега строчными русскими буквами. Сразу после # только буква.\n\n'
    'kind — что это за материал. «новость», «анонс», «трейлер», «релиз», «слух» — '
    'сообщение о конкретном событии. «подборка» — список вроде «5 лучших аниме». '
    '«обзор» — рецензия. «мнение» — авторская колонка без нового факта.\n\n'
    'subject — главный тайтл, франшиза или игра, о которых новость, в оригинальном '
    'написании: Bleach, Chainsaw Man, «Атака титанов». Одна короткая строка. '
    'Если новость не про конкретное произведение — пустая строка.\n\n'

    'Правила:\n'
    '1. Факты о новости — даты, числа, имена, названия студий и платформ — '
    'бери ТОЛЬКО из исходного текста.\n'
    '2. Общеизвестный контекст о произведении добавить можно (что это за тайтл, '
    'по какому первоисточнику, какая по счёту часть), но лишь если уверен. '
    'Сомневаешься — пропусти абзац. Лучше короче, чем неверно.\n'
    '3. Никаких оценок, прогнозов, «фанаты в восторге» и призывов подписаться.\n'
    '4. Названия тайтлов, студий, компаний, сервисов и имена людей НЕ переводи: '
    'Bleach, MAPPA, Prime Video, Crunchyroll, Netflix.\n'
    '5. Названия кириллицей — в кавычках-ёлочках: «Атака титанов». '
    'Латиницу оставляй без кавычек.\n'
    '6. Вместо «сегодня», «завтра», «на этой неделе» — конкретная дата из текста. '
    'Даты нет — не упоминай срок вовсе.\n'
    '7. Первый абзац не пересказывает заголовок другими словами, а дополняет его.\n'
    '8. topic — реальная тема. «прочее» ставь, только когда новость вообще '
    'не про гик-культуру.\n\n'

    'Пример.\n'
    'Вход: "Bleach: Thousand-Year Blood War Part 4 opening by jo0ji revealed. '
    'The final cour premieres October 4 on Disney+. Studio Pierrot returns."\n'
    'Выход: {"topic":"аниме","kind":"новость","subject":"Bleach: Thousand-Year '
    'Blood War","title":"Опенинг финальной части Bleach: '
    'Thousand-Year Blood War записал jo0ji","summary":"Заключительный кур выходит '
    '4 октября на Disney+, анимацией снова занимается студия Pierrot.\\n\\n'
    'Это экранизация последней арки манги Тайто Кубо — на ней история '
    'заканчивается.","tags":["#аниме","#опенинг"]}'
)


def _too_similar(a: str, b: str) -> bool:
    """Пересказывает ли summary заголовок вместо того, чтобы дополнять его.

    Сравниваем по общим словам: модель любит перефразировать заголовок, и такой
    пост выглядит как заикание — одно и то же двумя абзацами."""
    def stems(text):
        # Обрезаем до основы: русские окончания меняются («карт»/«карточек»),
        # а пересказ от этого пересказом быть не перестаёт
        return {w[:5] for w in re.findall(r'[а-яёa-z0-9]{4,}', text.lower())}
    wa, wb = stems(a), stems(b)
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= 0.6


LLM_TITLE_MAX = 200         # длиннее — это уже не заголовок
LLM_SUMMARY_MAX = 650       # 2-3 коротких абзаца; вместе с тегами влезает в caption
LLM_MAX_PARAGRAPHS = 3


def _sanity_ok(value: str, limit: int) -> bool:
    """Защита от простыни. Раньше сравнивали с длиной исходника — и это резало
    как раз то, что нужно: пост с вводными длиннее сухой новостной строки.
    Теперь ограничиваем по абсолютной длине, а достоверность держим промптом."""
    return len(value) <= limit


def _trim_paragraphs(text: str, max_paragraphs: int = LLM_MAX_PARAGRAPHS,
                     max_len: int = LLM_SUMMARY_MAX) -> str:
    """Оставляет не больше N абзацев и укладывается в лимит.
    Абзацы сохраняем: посты со структурой читаются легче сплошного текста."""
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text or '') if p.strip()]
    out, total = [], 0
    for para in paragraphs[:max_paragraphs]:
        if total + len(para) > max_len:
            room = max_len - total
            if room > 150:          # обрывок короче смысла не имеет
                out.append(smart_truncate(para, room))
            break
        out.append(para)
        total += len(para) + 2
    return '\n\n'.join(out)


async def _llm_enrich(news: dict) -> str:
    """Прогоняет новость через модель: перевод, чистый текст, тема и теги.

    Возвращает:
      'off'  — модель не используется, работаем как раньше;
      'ok'   — обогатили (результат лежит в news['_llm_*']);
      'skip' — модель считает новость непрофильной, пост публиковать не надо."""
    if not _llm_active():
        return 'off'
    title = (news.get('title') or '').strip()
    if not title:
        return 'off'
    summary = re.sub(r'\s+', ' ', (news.get('summary') or '')).strip()[:1500]

    # В RSS обычно лежит обрезанный тизер в 8-10 слов. Из него нельзя собрать
    # пост с фактами, поэтому при бедном описании читаем саму статью.
    # Статью читаем в двух случаях: описание слишком бедное для поста, либо
    # новость явно про ролик, а ролика в ленте не оказалось.
    need_text = _looks_thin(summary)
    need_video = not news.get('video') and _probably_has_video(news)
    if settings.llm_read_article and news.get('link') and (need_text or need_video):
        article = await asyncio.to_thread(fetch_article, news['link'])
        text = article.get('text') or ''
        if text and len(text.split()) > len(summary.split()):
            summary = text
            news['_article_used'] = True
        # Ролик у новостей про трейлеры лежит в статье, а не в ленте
        if not news.get('video') and article.get('video'):
            news['video'] = article['video']
            news['_video_note'] = 'ролик найден в статье'
            logger.info(f"🎬 Ролик найден на странице статьи: {title[:50]}")

    payload = (f'Источник: {news.get("source", "?")}\n'
               f'Заголовок: {title}\n'
               f'Текст: {summary or "(нет)"}')
    raw = await _llm_call([
        {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
        {'role': 'user', 'content': payload},
    ])
    data = _llm_parse_json(raw or '')
    if not data:
        if raw:
            logger.info(f"LLM: ответ не разобрался, беру обычный путь — {raw[:80]}")
        return 'off'

    # --- Фильтр непрофильного ---
    # Решаем по теме, а не по флагу relevant: модель регулярно противоречила
    # сама себе — ставила topic="игры" (разрешённая тема) и relevant=false.
    topic = str(data.get('topic') or '').strip().lower()
    if topic not in LLM_TOPIC_ANY:
        topic = 'прочее' if data.get('relevant') is False else ''
    if settings.llm_filter and topic == 'прочее':
        logger.info(f"⊘ Модель: новость не по теме канала: {title[:60]}")
        return 'skip'
    news['_llm_topic'] = topic

    # --- Тип материала: подборки и колонки это не новости ---
    kind = str(data.get('kind') or '').strip().lower()
    if kind not in LLM_KINDS_NEWS + LLM_KINDS_FILLER:
        kind = 'новость'
    news['_llm_kind'] = kind
    if settings.llm_skip_filler and kind in LLM_KINDS_FILLER:
        logger.info(f"⊘ Модель: это {kind}, а не новость: {title[:60]}")
        return 'skip'

    # --- Предмет новости: ловим повторы и однообразие ---
    subject = str(data.get('subject') or '').strip()[:120]
    news['_llm_subject'] = subject
    if subject and recent_subjects is not None:
        if settings.llm_dedup_subject and recent_subjects.seen_same_news(subject, kind):
            logger.info(f"⊘ Об этом уже писали ({subject[:40]} / {kind}): {title[:50]}")
            return 'skip'
        same_today = recent_subjects.count_today(subject)
        if settings.llm_limit_repeats and same_today >= SUBJECT_MAX_PER_DAY:
            logger.info(f"⊘ Уже {same_today} поста про «{subject[:40]}» за сутки — "
                        f"пропускаю: {title[:45]}")
            return 'skip'

    new_title = str(data.get('title') or '').strip()
    new_summary = str(data.get('summary') or '').strip()

    # --- Текст: только если он адекватен ---
    if settings.llm_rewrite and new_title:
        if _sanity_ok(new_title, LLM_TITLE_MAX) and _sanity_ok(new_summary, LLM_SUMMARY_MAX * 2):
            # Пересказ заголовка вместо дополнения — выбрасываем, оставляя заголовок
            if new_summary and _too_similar(new_title, new_summary):
                logger.info(f"LLM: текст пересказывает заголовок — оставляю только его "
                            f"({new_title[:45]})")
                new_summary = ''
            parts = [new_title.rstrip('.') + '.' if not new_title.endswith(('.', '!', '?'))
                     else new_title]
            if new_summary:
                parts.append(_trim_paragraphs(new_summary))
            body = '\n\n'.join(p for p in parts if p)

            date_str = extract_release_date_from_text(
                (news.get('title') or '') + ' ' + (news.get('summary') or '')[:600])
            if date_str:
                body += f'\n\n📅 {date_str}'
            news['_llm_text'] = body
        else:
            logger.info(f"LLM: ответ не влез в лимиты, беру обычный путь: {title[:50]}")

    # --- Теги ---
    if settings.llm_tags:
        tags = data.get('tags')
        if isinstance(tags, list):
            clean = []
            for tag in tags[:3]:
                tag = re.sub(r'[^0-9A-Za-zА-Яа-яЁё_]', '', str(tag)).lower()
                # Telegram нестабильно подсвечивает теги, начинающиеся с цифры
                tag = re.sub(r'^[0-9_]+', '', tag)
                if 2 <= len(tag) <= 24:
                    clean.append('#' + tag)
            if clean:
                news['_llm_tags'] = ' '.join(dict.fromkeys(clean))
    return 'ok'


def _tg_channels_available() -> list[tuple[str, str]]:
    """Все подключённые Telegram-каналы: [(канал, метка)]."""
    out = list(TELEGRAM_CHANNELS)
    if custom_sources is not None:
        for item in custom_sources.all():
            if item.get('type') == 'tg':
                pair = (item['value'], item['label'])
                if pair not in out:
                    out.append(pair)
    return out


def _post_number(link: str) -> str:
    """'https://t.me/ch/22342' → '22342'."""
    return (link or '').rstrip('/').split('/')[-1] or '?'


@admin_only
async def llm_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Состояние языковой модели и живая проверка связи."""
    lines = ['🤖 <b>Языковая модель</b>', '']
    if not _llm_configured():
        lines.append('❌ Не настроена.')
        lines.append('')
        lines.append('Задай на хостинге две переменные:')
        lines.append('  <code>LLM_PROVIDER</code> — '
                     + ' / '.join(sorted(LLM_PRESETS)))
        lines.append('  <code>LLM_API_KEY</code> — ключ провайдера')
        lines.append('')
        lines.append('Необязательно: <code>LLM_MODEL</code>, <code>LLM_BASE_URL</code> — '
                     'если нужна другая модель или адрес.')
        lines.append('')
        lines.append('Пока не настроена, бот переводит через DeepL/Google — как раньше.')
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
        return

    used = settings.llm_calls_today if settings.llm_day == _local_now().strftime('%Y-%m-%d') else 0
    lines.append(f'Провайдер: {html.escape(LLM_PROVIDER or "свой адрес")}')
    lines.append(f'Модель: <code>{html.escape(LLM_MODEL)}</code>')
    lines.append(f'Адрес: <code>{html.escape(LLM_BASE_URL)}</code>')
    lines.append('')
    lines.append('Включено: ' + ('ДА' if settings.llm_enabled else 'НЕТ'))
    lines.append('  📝 Перевод и текст: ' + ('ВКЛ' if settings.llm_rewrite else 'ВЫКЛ'))
    lines.append('  🚫 Отсев не по теме: ' + ('ВКЛ' if settings.llm_filter else 'ВЫКЛ'))
    lines.append('  #️⃣ Хэштеги: ' + ('ВКЛ' if settings.llm_tags else 'ВЫКЛ'))
    lines.append('  📄 Читать статьи: ' + ('ВКЛ' if settings.llm_read_article else 'ВЫКЛ'))
    lines.append('  🗑 Отсев подборок: ' + ('ВКЛ' if settings.llm_skip_filler else 'ВЫКЛ'))
    lines.append('  ♻️ Ловить повторы: ' + ('ВКЛ' if settings.llm_dedup_subject else 'ВЫКЛ'))
    lines.append('')
    lines.append(f'Вызовов сегодня: {used} из {LLM_DAILY_LIMIT}')
    if _llm_disabled_runtime:
        lines.append('⚠️ Временно отключена из-за ошибок — вернётся после перезапуска')
    elif _llm_fail_streak:
        lines.append(f'⚠️ Ошибок подряд: {_llm_fail_streak}')
    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)

    # Живая проверка. С аргументом — на своём тексте: /llm <заголовок>
    custom = ' '.join(context.args or []).strip()
    await update.message.reply_text('Проверяю…')
    if custom:
        probe = {'title': custom[:300], 'summary': '', 'source': 'проверка', 'lang': 'ru'}
    else:
        probe = {
            'title': "Bleach: Thousand-Year Blood War Part 4 opening by jo0ji revealed",
            'summary': 'The final cour premieres October 4 on Disney+. '
                       'Studio Pierrot returns for the last part.',
            'source': 'проверка', 'lang': 'en',
        }
    before_disabled = _llm_disabled_runtime
    result = await _llm_enrich(probe)
    if result == 'off':
        await update.message.reply_text(
            '❌ Ответа нет.\n\n'
            + ('Ключ отклонён провайдером — проверь LLM_API_KEY.'
               if _llm_disabled_runtime and not before_disabled
               else 'Причина в логах: /logs LLM'))
        return
    if result == 'skip':
        await update.message.reply_text(
            '🚫 Модель посчитала это непрофильным и отсеяла бы такой пост.\n'
            'Если это ошибка — выключи отсев: /settings → 🚫 Отсев не по теме')
        return

    out = ['✅ <b>Модель отвечает</b>', '', '<u>Было</u>',
           f'<i>{html.escape(probe["title"])}</i>']
    if probe.get('summary'):
        out.append(f'<i>{html.escape(probe["summary"])}</i>')
    out += ['', '<u>Стало</u>']
    out.append(html.escape(probe.get('_llm_text') or '(текст не заменён)'))
    if probe.get('_llm_tags'):
        out.append('')
        out.append(html.escape(probe['_llm_tags']))
    out += ['', f'Тема: {html.escape(probe.get("_llm_topic") or "?")}',
            f'Строгий JSON: {"да" if _llm_json_mode else "нет"}']
    if not custom:
        out.append('')
        out.append('Проверить на своём тексте: <code>/llm заголовок новости</code>')
    await update.message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


@admin_only
async def videocheck_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Живая проверка: почему у постов канала не прикрепляется видео.

    Отвечает прямо в чат, а не в лог: диагностика видео пишется в начале цикла
    проверки, и в хвосте /logs её уже не видно."""
    channels = _tg_channels_available()
    arg = (context.args or [''])[0].strip().lstrip('@')
    if not arg:
        listing = '\n'.join(f'  • {ch}' for ch, _lbl in channels[:20]) or '  (нет)'
        await update.message.reply_text(
            '🎬 Проверка видео в канале\n\n'
            'Формат: /videocheck <канал>\n\n'
            f'Подключённые каналы:\n{listing}')
        return

    match = next(((ch, lbl) for ch, lbl in channels if ch.lower() == arg.lower()), None)
    channel, label = match if match else (arg, f'TG: {arg}')

    # Настройки важнее всего: при выключенном видео остальное не имеет значения
    head = ['🎬 <b>Проверка видео</b>', f'Канал: @{html.escape(channel)}', '']
    if not settings.video_enabled:
        head.append('⚠️ <b>Настройка «🎬 Видео» ВЫКЛЮЧЕНА</b>')
        head.append('Пока она выключена, ролики не прикрепляются ни при каких условиях.')
        head.append('Включить: /settings → 🎬 Видео')
    else:
        head.append('🎬 Видео: ВКЛ')
    head.append(f'Лимит длительности: {TG_VIDEO_MAX_SECONDS // 60} мин')
    head.append(f'Лимит размера файла: {TG_VIDEO_MAX_MB} МБ')
    head.append('')
    await update.message.reply_text('\n'.join(head), parse_mode=ParseMode.HTML)

    await update.message.reply_text('Забираю посты канала…')
    try:
        posts = await asyncio.to_thread(get_telegram_channel, channel, label)
    except Exception as e:
        await update.message.reply_text(f'❌ Канал не прочитался: {type(e).__name__}: {e}')
        return
    if not posts:
        await update.message.reply_text(
            '❌ Постов не получено. Канал закрыт, переименован или Telegram '
            'не отдал страницу серверу.')
        return

    video_posts = [p for p in posts if p.get('_video_note')]
    lines = [f'Постов получено: {len(posts)}, из них с видео: {len(video_posts)}', '']
    # Показываем ВСЕ посты: так видно, если бот не заметил видео там, где оно есть
    lines.append('<b>Все посты канала</b>')
    for post in posts:
        num = _post_number(post.get('link', ''))
        media = _media_summary(post)
        mark = '📹' if post.get('_video_note') else '🖼'
        title = re.sub(r'\s+', ' ', post.get('title', ''))[:38]
        lines.append(f'{mark} {html.escape(num)}: {media} — {html.escape(title)}…')
    lines.append('')
    lines.append('🖼 = видео не обнаружено. Если у такого поста в канале '
                 'на самом деле ролик — пришли мне его номер.')
    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)

    if not video_posts:
        await update.message.reply_text(
            'В свежих постах видео не найдено. Если в канале оно есть — '
            'значит бот его не распознал, пришли номер поста из списка выше.')
        return
    lines = []

    for post in video_posts[:5]:
        num = _post_number(post.get('link', ''))
        note = post.get('_video_note', '')
        block = ['', f'📹 <b>Пост {html.escape(num)}</b>',
                 f'  Разбор: {html.escape(note)}']
        url = post.get('video')
        if not url:
            block.append('  Итог: ❌ ролика нет — уйдёт кадр-превью')
        else:
            block.append(f'  Ссылка: <code>{html.escape(url[:60])}…</code>')
            resolved = await _resolve_video(url)
            if resolved is None:
                block.append('  Скачивание: ❌ файл не отдался '
                             '(см. строку «Медиа не скачалось» в /logs)')
                block.append('  Итог: ❌ уйдёт кадр-превью')
            elif isinstance(resolved, (bytes, bytearray)):
                mb = len(resolved) / (1024 * 1024)
                block.append(f'  Скачивание: ✅ {mb:.1f} МБ')
                block.append('  Итог: ✅ видео прикрепится')
            else:
                block.append('  Скачивание: не требуется (обычный хост)')
                block.append('  Итог: ✅ видео прикрепится')
        lines.extend(block)

    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)

    if all(not p.get('video') for p in video_posts):
        await update.message.reply_text(
            'ℹ️ Ни у одного поста нет прямой ссылки на файл.\n\n'
            'Это значит, что Telegram не отдаёт видео веб-странице канала — '
            'чаще всего из-за включённой в канале защиты контента. '
            'Обойти это можно только через пользовательский аккаунт '
            '(Telethon), а не через бота. Кадр-превью остаётся рабочим '
            'компромиссом.')


@admin_only
async def logs_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Присылает последние строки лога. С аргументом — только строки с этим словом.

    Фильтр нужен потому, что интересное (диагностика видео, ошибки источников)
    пишется в начале цикла, а в хвост последних 50 строк попадает уже только
    публикация — искомого там просто нет. Пример: /logs видео"""
    if not LOG_FILE.exists():
        await update.message.reply_text("📝 Лог-файла нет (бот, видимо, недавно запущен).")
        return

    try:
        # Читаем последние N строк. Для эффективности на больших файлах
        # читаем с конца через seek, но для простоты — целиком.
        # Если файл большой, ограничим чтение хвоста.
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open('r', encoding='utf-8', errors='replace') as f:
            # Хвост: читаем не больше 200 КБ
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()  # отбрасываем неполную первую строку
            lines = f.readlines()
    except OSError as e:
        await update.message.reply_text(f"⚠️ Не удалось прочитать лог: {e}")
        return

    needle = ' '.join(context.args or []).strip().lower()
    if needle:
        lines = [ln for ln in lines if needle in ln.lower()]
        if not lines:
            await update.message.reply_text(
                f"📝 Строк со словом «{needle}» в логе нет.\n\n"
                f"Подсказки: /logs видео • /logs ошибка • /logs отложк")
            return

    tail = lines[-LOG_TAIL_LINES:] if len(lines) > LOG_TAIL_LINES else lines
    if not tail:
        await update.message.reply_text("📝 Лог пуст.")
        return

    text = ''.join(tail)
    # Telegram message limit = 4096 chars. Обрезаем с начала если не влезает.
    header = (f"📝 Последние {len(tail)} строк"
              + (f" со словом «{needle}»" if needle else "")
              + f" ({LOG_FILE.name}):\n\n")
    body_limit = 4096 - len(header) - 10  # запас
    if len(text) > body_limit:
        text = '…\n' + text[-(body_limit - 2):]

    await update.message.reply_text(
        f"{header}<pre>{html.escape(text)}</pre>",
        parse_mode=ParseMode.HTML,
    )


def _format_age(ts_iso: Optional[str]) -> str:
    """Превращает iso-timestamp в относительное «N мин/ч/д назад»."""
    if not ts_iso:
        return 'никогда'
    try:
        ts = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return 'неизвестно'
    delta = datetime.now() - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f'{seconds}с назад'
    if seconds < 3600:
        return f'{seconds // 60}м назад'
    if seconds < 86400:
        return f'{seconds // 3600}ч назад'
    return f'{seconds // 86400}д назад'


@admin_only
async def deepl_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает использование месячного лимита DeepL."""
    if not DEEPL_API_KEY:
        await update.message.reply_text(
            "🌐 Ключ DeepL не задан (переменная DEEPL_API_KEY).\n"
            "Перевод работает через Google Translate."
        )
        return
    usage, err = await asyncio.to_thread(_deepl_usage)
    if not usage:
        # Статистика не пришла — проверяем живым тестовым переводом, работает ли ключ вообще
        test = await asyncio.to_thread(_deepl_translate, 'Hello')
        if test:
            await update.message.reply_text(
                f"⚠️ Статистика лимита недоступна: {err}.\n"
                f"Но сам перевод через DeepL РАБОТАЕТ (тест прошёл).\n\n"
                f"Похоже, WAF DeepL блокирует usage-запросы с IP хостинга — "
                f"на работу перевода это не влияет.\n"
                f"Лимит можно посмотреть в личном кабинете:\n"
                f"https://www.deepl.com/account/usage"
            )
        else:
            await update.message.reply_text(
                f"🔴 DeepL не отвечает: {err}. Тестовый перевод тоже не прошёл.\n\n"
                f"Скорее всего ключ неверный. Частые причины:\n"
                f"• ключ пересоздавался (после утечки), а в Bothost остался старый — "
                f"обнови DEEPL_API_KEY и перезапусти бота\n"
                f"• пробел/кавычки в значении переменной\n\n"
                f"Пока DeepL недоступен, перевод тихо идёт через Google Translate. "
                f"Подробности: /logs"
            )
        return
    used = usage.get('character_count', 0)
    limit = usage.get('character_limit', 0)
    pct = (used / limit * 100) if limit else 0
    left = limit - used
    # Простой прогресс-бар из 10 клеток
    filled = min(10, round(pct / 10))
    bar = '█' * filled + '░' * (10 - filled)
    engine = settings.translator_engine
    lines = [
        '🌐 <b>DeepL — месячный лимит</b>',
        '',
        f'{bar} {pct:.1f}%',
        f'Использовано: {used:,} из {limit:,} символов'.replace(',', ' '),
        f'Осталось: {left:,} символов'.replace(',', ' '),
        '',
        f'Выбранный движок: {"DeepL" if engine == "deepl" else "Google (вручную)"}',
    ]
    if pct >= 90:
        lines.append('')
        lines.append('⚠️ Лимит почти исчерпан! Скоро бот перейдёт на Google Translate.')
    elif pct >= 100:
        lines.append('')
        lines.append('🔴 Лимит исчерпан — работает Google Translate (до сброса лимита).')
    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def sources_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Список динамических источников (добавленных через /addsource)."""
    items = custom_sources.all() if custom_sources else []
    if not items:
        await update.message.reply_text(
            "Динамических источников нет.\n\n"
            "Добавить:\n"
            "/addsource https://site.com/feed/ Название\n"
            "/addsource @канал — Telegram-канал\n\n"
            "Встроенные источники включаются/выключаются в /settings → Источники."
        )
        return
    lines = ['📡 Динамические источники:', '']
    for it in items:
        kind = 'TG' if it['type'] == 'tg' else 'RSS'
        lines.append(f"• {it['label']} [{kind}] — {it['value']}")
    lines.append('')
    lines.append('Удалить: /delsource Название')
    await update.message.reply_text('\n'.join(lines))


@admin_only
async def addsource_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет источник: RSS-ленту или публичный Telegram-канал."""
    parsed = _parse_addsource_args(context.args or [])
    if not parsed:
        await update.message.reply_text(
            "Форматы:\n"
            "/addsource https://site.com/feed/ Название — RSS-лента\n"
            "/addsource @канал — Telegram-канал\n"
            "/addsource t.me/канал Название"
        )
        return
    src_type, value, label = parsed
    if any(name.lower() == label.lower() for name, _ in SOURCES):
        await update.message.reply_text(f"⚠️ Источник с именем «{label}» уже есть.")
        return
    await update.message.reply_text(f"Проверяю «{label}»…")
    # Живая проверка: сколько записей отдаёт прямо сейчас
    fn = _make_source_fn(src_type, value, label)
    try:
        found = await asyncio.to_thread(fn)
        count = len(found or [])
    except Exception as e:
        logger.warning(f"Проверка источника {label}: {e}")
        count = -1
    custom_sources.add(src_type, value, label)
    _attach_custom_source({'type': src_type, 'value': value, 'label': label})
    if count > 0:
        msg = f"✅ «{label}» добавлен — прямо сейчас отдаёт {count} записей.\nУчаствует со следующей проверки."
    elif count == 0:
        msg = (f"⚠️ «{label}» добавлен, но сейчас отдал 0 записей "
               f"(возможно, пусто или фильтры всё отсеяли). Следи за /stats.")
    else:
        msg = f"⚠️ «{label}» добавлен, но проверка не удалась (см. /logs). Следи за /stats."
    await update.message.reply_text(msg)


@admin_only
async def delsource_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет динамический источник по имени."""
    name = ' '.join(context.args or []).strip()
    if not name:
        await update.message.reply_text("Формат: /delsource Название\nСписок: /sources")
        return
    removed = custom_sources.remove(name) if custom_sources else None
    if not removed:
        builtin = any(n.lower() == name.lower() for n, _ in SOURCES)
        if builtin:
            await update.message.reply_text(
                f"«{name}» — встроенный источник, удалить нельзя.\n"
                f"Отключи его: /settings → 📡 Источники."
            )
        else:
            await update.message.reply_text(f"Источник «{name}» не найден. Список: /sources")
        return
    for i, (n, _fn) in enumerate(SOURCES):
        if n.lower() == name.lower():
            SOURCES.pop(i)
            break
    await update.message.reply_text(f"🗑 «{removed['label']}» удалён.")


USER_DIRECTORY_FILE = DATA_DIR / 'known_users.json'
USER_DIRECTORY_MAX = 3000


class UserDirectory:
    """Кто и под каким именем писал боту или в ветку модерации.

    Нужен, потому что Bot API не умеет искать пользователя по @имени: ботам
    доступны только те, кого они уже видели. Поэтому запоминаем всех, кто
    что-то написал или нажал кнопку, и по этому справочнику выдаём права."""

    SAVE_EVERY_SEC = 60          # чаще на диск не пишем

    def __init__(self, path: Path):
        self.path = path
        self._by_id: dict[str, dict] = {}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    self._by_id = data
        except (OSError, ValueError) as e:
            logger.warning(f"known_users не загружен: {e}")

    def _save(self, force: bool = False) -> None:
        """Пишет на диск не чаще раза в минуту.

        Справочник пополняется на каждое сообщение в ветке, а полная перезапись
        файла на каждого пользователя — лишняя нагрузка. Потерять пару записей
        при перезапуске не страшно: человек напишет снова и вернётся в список."""
        self._dirty = True
        now = time.time()
        if not force and now - self._last_save < self.SAVE_EVERY_SEC:
            return
        try:
            self.path.write_text(json.dumps(self._by_id, ensure_ascii=False),
                                 encoding='utf-8')
            self._dirty = False
            self._last_save = now
        except OSError as e:
            logger.error(f"known_users не сохранён: {e}")

    def flush(self) -> None:
        """Принудительно сбрасывает накопленное на диск."""
        if self._dirty:
            self._save(force=True)

    def remember(self, user) -> None:
        """Запоминает пользователя. Тихо: вызывается на каждое сообщение."""
        if user is None or getattr(user, 'is_bot', False):
            return
        raw_id = getattr(user, 'id', None)
        if not isinstance(raw_id, int):
            return
        uid = str(raw_id)
        entry = {
            'username': (getattr(user, 'username', '') or '').lower(),
            'name': getattr(user, 'full_name', '') or '',
            'seen': datetime.now(timezone.utc).isoformat(),
        }
        if self._by_id.get(uid) == entry:
            return                       # ничего не изменилось — не пишем на диск
        old = self._by_id.get(uid)
        self._by_id[uid] = entry
        if len(self._by_id) > USER_DIRECTORY_MAX:
            oldest = sorted(self._by_id, key=lambda k: self._by_id[k].get('seen', ''))
            for key in oldest[:len(self._by_id) - USER_DIRECTORY_MAX]:
                del self._by_id[key]
        # На диск пишем только при реальном изменении данных, а не каждый раз
        if old is None or old.get('username') != entry['username'] \
                or old.get('name') != entry['name']:
            self._save()

    def remember_now(self, user) -> None:
        """Запомнить и сразу записать — для случаев, когда это важно
        (например, человеку только что выдали права)."""
        self.remember(user)
        self.flush()

    def find_by_username(self, username: str) -> Optional[tuple[int, str]]:
        """(id, отображаемое имя) по @имени или None."""
        key = (username or '').strip().lstrip('@').lower()
        if not key:
            return None
        for uid, entry in self._by_id.items():
            if entry.get('username') == key:
                try:
                    return int(uid), entry.get('name') or f'@{key}'
                except ValueError:
                    return None
        return None

    def describe(self, user_id: int) -> str:
        """Человекочитаемое имя по id: «Вася Пупкин (@vasya)»."""
        entry = self._by_id.get(str(user_id))
        if not entry:
            return str(user_id)
        name = entry.get('name') or ''
        username = entry.get('username') or ''
        if name and username:
            return f'{name} (@{username})'
        return name or (f'@{username}' if username else str(user_id))

    def __len__(self) -> int:
        return len(self._by_id)


user_directory: Optional['UserDirectory'] = None


async def remember_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тихо запоминает всех, кто пишет боту или в ветку. Ничего не блокирует."""
    if user_directory is None:
        return
    try:
        user_directory.remember(update.effective_user)
        msg = getattr(update, 'message', None)
        if msg is not None and getattr(msg, 'reply_to_message', None):
            user_directory.remember(msg.reply_to_message.from_user)
    except Exception as e:
        logger.debug(f"пользователь не запомнился: {e}")


async def _resolve_user(update, context) -> tuple[Optional[int], str, str]:
    """Кому адресована команда: (id, имя, пояснение при неудаче).

    Три способа, в порядке надёжности:
      1) команда отправлена ответом на сообщение — берём автора;
      2) @имя — ищем среди тех, кого бот уже видел;
      3) числовой id — как есть."""
    message = update.message
    reply = getattr(message, 'reply_to_message', None)
    if reply is not None and getattr(reply, 'from_user', None):
        user = reply.from_user
        if user_directory is not None:
            user_directory.remember(user)
        return user.id, (user.full_name or f'@{user.username}' if user.username
                         else str(user.id)), ''

    args = context.args or []
    if not args:
        return None, '', ('Ответь этой командой на сообщение человека — так надёжнее всего.\n'
                          'Либо укажи @имя или числовой id.')

    arg = args[0].strip()
    if arg.lstrip('-').isdigit():
        return int(arg), (user_directory.describe(int(arg)) if user_directory
                          else arg), ''

    if arg.startswith('@') or re.fullmatch(r'[A-Za-z0-9_]{4,}', arg):
        found = user_directory.find_by_username(arg) if user_directory else None
        if found:
            return found[0], found[1], ''
        # Пробуем спросить у Telegram — сработает редко, но попробовать стоит
        try:
            chat = await context.bot.get_chat(arg if arg.startswith('@') else f'@{arg}')
            uid = getattr(chat, 'id', None)
            if isinstance(uid, int) and uid > 0:
                return uid, (getattr(chat, 'full_name', None)
                             or getattr(chat, 'title', None) or arg), ''
        except Exception:
            pass          # Telegram почти всегда отказывает — это ожидаемо
        return None, '', (
            f'Не знаю пользователя {html.escape(arg)}.\n\n'
            'Telegram не позволяет ботам искать людей по @имени — я знаю только тех, '
            'кто уже что-то писал в ветку или мне.\n\n'
            'Надёжный способ: ответь этой командой на любое сообщение этого человека '
            'в ветке модерации.')

    return None, '', 'Не понял, кому. Ответь на сообщение человека или укажи @имя.'


async def admins_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Список админов с именами. Управление — только у главного админа."""
    if update.effective_user.id != ADMIN_ID:
        await deny_access(update)
        return
    who = user_directory.describe if user_directory else str
    lines = ['👥 <b>Администраторы</b>', '',
             f'👑 {html.escape(who(ADMIN_ID))} — главный']
    extra = settings.extra_admins
    for uid in extra:
        lines.append(f'• {html.escape(who(uid))}')
    if not extra:
        lines.append('Дополнительных админов нет.')
    lines += ['', '<b>Выдать права</b>',
              'Ответь на сообщение человека: <code>/addadmin</code>',
              'Или по имени: <code>/addadmin @username</code>',
              '', '<b>Забрать права</b>',
              '<code>/deladmin</code> ответом на сообщение или '
              '<code>/deladmin @username</code>']
    if user_directory is not None:
        lines.append('')
        lines.append(f'Знакомых пользователей: {len(user_directory)}')
    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)


async def addadmin_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Выдаёт права админа. Ответом на сообщение, по @имени или по id."""
    if update.effective_user.id != ADMIN_ID:
        await deny_access(update)
        return
    uid, name, problem = await _resolve_user(update, context)
    if uid is None:
        await update.message.reply_text(problem, parse_mode=ParseMode.HTML)
        return
    if uid == ADMIN_ID:
        await update.message.reply_text('Это ты и есть — главный админ.')
        return
    label = html.escape(name or str(uid))
    if settings.add_admin(uid):
        if user_directory is not None:
            user_directory.flush()
        logger.info(f"👥 Выдана админка: {name} ({uid})")
        await update.message.reply_text(
            f'✅ <b>{label}</b> теперь админ.\n\n'
            f'Доступны команды бота и кнопки модерации в ветке.\n'
            f'Чтобы получать уведомления, ему нужно открыть бота и нажать /start.',
            parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f'{label} уже админ.', parse_mode=ParseMode.HTML)


async def deladmin_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Забирает права админа. Ответом на сообщение, по @имени или по id."""
    if update.effective_user.id != ADMIN_ID:
        await deny_access(update)
        return
    uid, name, problem = await _resolve_user(update, context)
    if uid is None:
        await update.message.reply_text(problem, parse_mode=ParseMode.HTML)
        return
    if uid == ADMIN_ID:
        await update.message.reply_text(
            'Себя разжаловать нельзя — иначе управлять ботом станет некому.')
        return
    label = html.escape(name or str(uid))
    if settings.remove_admin(uid):
        logger.info(f"👥 Забрана админка: {name} ({uid})")
        await update.message.reply_text(f'🗑 <b>{label}</b> больше не админ.',
                                        parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f'{label} и не был админом.',
                                        parse_mode=ParseMode.HTML)


# ============== САМОДИАГНОСТИКА: ТИШИНА, СТАРТ, КВОТА ПЕРЕВОДЧИКА ==============

WATCHDOG_SILENCE_HOURS = 8      # столько без единой публикации → сигнал тревоги
DEEPL_MONTHLY_LIMIT = 1_000_000  # символов в месяц (свой тариф — поправь тут)
DEEPL_WARN_AT = (0.8, 0.95)     # на каких долях лимита предупреждать

# Разовые сообщения админам, которые накопились вне контекста бота
# (в парсерах и джобах бота под рукой нет) — check_news их разошлёт.
_pending_admin_alerts: list[str] = []
_silence_reported = False       # чтобы не повторять тревогу каждый цикл


def _queue_admin_alert(text: str) -> None:
    """Ставит сообщение админам в очередь. Дубли не копим."""
    if text not in _pending_admin_alerts:
        _pending_admin_alerts.append(text)
    if len(_pending_admin_alerts) > 20:
        del _pending_admin_alerts[:-20]


def _mark_published() -> None:
    """Отмечает факт успешной публикации — питание для сторожа тишины."""
    global _silence_reported
    _silence_reported = False
    if settings is not None:
        settings.last_publish_at = datetime.now(timezone.utc).isoformat()


def _silence_hours() -> Optional[float]:
    """Сколько часов не было ни одной публикации. None — если отметки ещё нет."""
    raw = getattr(settings, 'last_publish_at', '') if settings else ''
    if not isinstance(raw, str) or not raw:
        return None                  # пусто или мусор в настройках — считаем «нет отметки»
    try:
        last = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 3600


async def _check_silence(bot: Bot) -> None:
    """Сторож: если бот давно ничего не опубликовал, значит что-то тихо сломалось.
    Ровно этот сценарий повторялся раньше — отложка, видео и картинки отваливались
    молча, и узнавали мы об этом только глазами."""
    global _silence_reported
    hours = _silence_hours()
    if hours is None:
        _mark_published()            # первая отметка — точка отсчёта
        return
    if hours < WATCHDOG_SILENCE_HOURS:
        return
    if _silence_reported:
        return
    _silence_reported = True
    enabled = sum(1 for n, _ in SOURCES if settings.is_source_enabled(n))
    await notify_admin(
        bot,
        f'🔇 Тревога: {int(hours)} ч без единой публикации\n\n'
        f'Источников включено: {enabled}\n'
        f'В отложке: {len(scheduled_posts.all()) if scheduled_posts else 0}\n'
        f'Ждут решения в ветке: {len(pending_posts._items) if pending_posts else 0}\n\n'
        f'Обычно это значит: источники перестали отдавать новости, всё уходит '
        f'в дубли или отправка падает. Диагностика — /health и /logs.')


def _deepl_usage_local() -> tuple[int, str]:
    """Наш локальный счётчик символов DeepL за текущий месяц: (символы, месяц)."""
    if settings is None:
        return 0, ''
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    try:
        if settings.deepl_month != month:
            return 0, month
        return int(settings.deepl_chars), month
    except (TypeError, ValueError):
        return 0, month


def _count_deepl_chars(text: str) -> None:
    """Прибавляет символы к месячному счётчику и предупреждает у порогов.

    Нужно, чтобы упёршийся лимит не превратился в очередную тихую поломку:
    переводы просто перестали бы приходить, а посты пошли бы на итальянском."""
    if settings is None or not text:
        return
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    if settings.deepl_month != month:
        settings.deepl_month = month
        settings.deepl_chars = 0
    before = settings.deepl_chars
    after = before + len(text)
    settings.deepl_chars = after
    for share in DEEPL_WARN_AT:
        edge = int(DEEPL_MONTHLY_LIMIT * share)
        if before < edge <= after:
            _queue_admin_alert(
                f'📝 DeepL израсходован на {int(share * 100)}%: '
                f'{after:,} из {DEEPL_MONTHLY_LIMIT:,} символов за {month}.\n'
                f'При исчерпании лимита бот сам переключится на Google Translate — '
                f'посты продолжат переводиться, но качеством пониже.'.replace(',', ' '))


def _deepl_usage_remote() -> Optional[tuple[int, int]]:
    """Реальные цифры из DeepL (/v2/usage): (использовано, лимит). None — если
    ключа нет или запрос не удался. Дёргается только по команде /health."""
    if not DEEPL_API_KEY:
        return None
    endpoint = ('https://api-free.deepl.com/v2/usage'
                if DEEPL_API_KEY.endswith(':fx')
                else 'https://api.deepl.com/v2/usage')
    try:
        r = requests.get(endpoint,
                         headers={'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}'},
                         timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        used = int(data.get('character_count', 0))
        limit = int(data.get('character_limit', 0))
        return used, limit
    except Exception as e:
        logger.debug(f"DeepL usage недоступен: {e}")
        return None


def _optional_deps_report() -> list[str]:
    """Что из необязательных зависимостей доступно на этом хостинге."""
    ffmpeg = shutil.which('ffmpeg') is not None
    return [
        f"  {'🟢' if Image is not None else '🟡'} Pillow: "
        + ('есть (перцептивный дедуп картинок)' if Image is not None
           else 'нет (дедуп только по точным копиям)'),
        f"  {'🟢' if YT_DLP_AVAILABLE else '🔴'} yt-dlp: "
        + ('есть (запасной способ добычи видео из Telegram работает)' if YT_DLP_AVAILABLE
           else 'НЕТ — часть видео из Telegram достать не получится'),
        f"  {'🟢' if ffmpeg else '🟡'} ffmpeg: "
        + ('есть' if ffmpeg else 'нет (для видео из Telegram не нужен)'),
    ]


async def _check_channel_access(bot) -> tuple[bool, str]:
    """Может ли бот публиковать в канал. Проверяем на старте: промах с правами
    иначе виден только по молчанию канала."""
    try:
        chat = await bot.get_chat(CHANNEL_ID)
    except Exception as e:
        return False, f'канал недоступен ({type(e).__name__}: {e})'
    title = getattr(chat, 'title', None) or str(CHANNEL_ID)
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(CHANNEL_ID, me.id)
    except Exception as e:
        return False, f'{title} — права не проверить ({type(e).__name__})'
    status = getattr(member, 'status', '?')
    if status not in ('administrator', 'creator'):
        return False, f'{title} — бот не администратор (сейчас: {status})'
    if getattr(member, 'can_post_messages', None) is False:
        return False, f'{title} — у бота нет права публиковать'
    return True, title


async def send_startup_report(app) -> None:
    """Короткий отчёт админам при запуске: что поднялось и что настроено.

    Деплой идёт через GitHub, и раньше единственным способом узнать результат
    было ждать, появятся ли посты. Теперь бот сам говорит, что он живой."""
    if settings is None or not settings.startup_report:
        return
    problems: list[str] = []
    if not DISCUSSION_CHAT_ID or not DISCUSSION_THREAD_ID:
        problems.append('не задан чат/ветка обсуждения — кнопки модерации работать не будут')
    if not DEEPL_API_KEY:
        problems.append('нет ключа DeepL — перевод идёт через Google Translate')
    enabled = [n for n, _ in SOURCES if settings.is_source_enabled(n)]
    paused = [n for n, _ in SOURCES if not settings.is_source_enabled(n)]
    if not enabled:
        problems.append('все источники на паузе — новостей не будет')

    ok_channel, channel_note = await _check_channel_access(app.bot)
    if not ok_channel:
        problems.append(f'публикация в канал не сработает: {channel_note}')
    if CHANNEL_FROM_ENV and CHANNEL_ID != MAIN_CHANNEL_ID:
        problems.append(
            f'посты уходят в {CHANNEL_ID} — это задано переменной CHANNEL_ID '
            f'на хостинге, а не значением в коде ({MAIN_CHANNEL_ID}). '
            f'Если канал не тот, поправь переменную или удали её.')

    lines = ['🚀 <b>Бот запущен</b>', '']
    lines.append(('📢 Канал: ' if ok_channel else '⚠️ Канал: ')
                 + html.escape(channel_note))
    lines.append(f'   ID {CHANNEL_ID} '
                 + ('(из переменной CHANNEL_ID)' if CHANNEL_FROM_ENV
                    else '(из кода)'))
    lines.append(f'📡 Источников: {len(enabled)} вкл' + (f', {len(paused)} на паузе' if paused else ''))
    if paused:
        lines.append(f'   ⏸ {html.escape(", ".join(paused[:8]))}')
    lines.append(f'🧵 Режим: ' + ('ветка обсуждения' if settings.thread_mode else 'сразу в канал'))
    lines.append('🎬 Видео: ' + ('ВКЛ' if settings.video_enabled else 'ВЫКЛ')
                 + f' (до {TG_VIDEO_MAX_SECONDS // 60} мин)')
    lines.append('🖼 Дедуп по картинке: ' + ('ВКЛ' if settings.image_dedup else 'ВЫКЛ'))
    if _llm_configured():
        lines.append(f'🤖 Модель: {html.escape(LLM_MODEL)} '
                     + ('(вкл)' if settings.llm_enabled else '(выкл)'))
    else:
        lines.append('🤖 Модель: не настроена — перевод через DeepL/Google')
    lines.append('👥 Кнопки в ветке: ' + ('для всех' if settings.open_moderation else 'только админы'))
    lines.append(f'🕒 Часовой пояс: UTC{_tz_offset():+d}, сейчас {_local_now():%d.%m %H:%M}')
    lines.append('')
    lines.append('<b>Хранилища</b>')
    lines.append(f'  📅 Отложка: {len(scheduled_posts.all()) if scheduled_posts else 0}')
    lines.append(f'  🗂 Ждут решения: {len(pending_posts._items) if pending_posts else 0}')
    lines.append(f'  🔗 История ссылок: {len(sent_links._set) if sent_links else 0}')
    lines.append(f'  🧠 Помню тем за {SUBJECT_MEMORY_HOURS} ч: '
                 f'{len(recent_subjects) if recent_subjects else 0}')
    lines.append(f'  📄 Статей в кэше: {len(_article_cache)}')
    lines.append(f'  🖼 Отпечатков картинок: {len(image_hashes) if image_hashes else 0}')
    lines.append('')
    lines.append('<b>Окружение</b>')
    lines.extend(_optional_deps_report())
    rss = _rss_mb()
    if rss is not None:
        lines.append(f'  🧠 Память: {rss} МБ')
    if problems:
        lines.append('')
        lines.append('⚠️ <b>Обратить внимание</b>')
        lines.extend(f'  • {html.escape(p)}' for p in problems)
    lines.append('')
    lines.append('Подробности в любой момент — /health')

    text = '\n'.join(lines)
    for uid in _all_admin_ids():
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning(f"Стартовый отчёт не ушёл админу {uid}: {e}")


BACKUP_HOUR = 5                 # во сколько по времени админа делать бэкап


def _rss_mb() -> Optional[int]:
    """Сколько памяти занимает процесс, МБ (Linux). None — если не прочиталось."""
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _data_files() -> list[Path]:
    """Все файлы данных бота (для бэкапа и отчёта о размерах)."""
    try:
        return sorted(p for p in DATA_DIR.glob('*.json') if p.is_file())
    except OSError:
        return []


def _fmt_size(num: int) -> str:
    return f"{num / 1024:.0f} КБ" if num >= 1024 else f"{num} Б"


def _job_line(context, name: str, human: str) -> str:
    try:
        jobs = context.application.job_queue.get_jobs_by_name(name)
    except Exception:
        jobs = []
    if not jobs:
        return f"  ⚠️ {human}: НЕ ЗАРЕГИСТРИРОВАН"
    nxt = getattr(jobs[0], 'next_t', None)
    return f"  ✅ {human}" + (f" — следующий запуск {_fmt_local(nxt)}" if nxt else "")


@admin_only
async def health_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Сводка о состоянии бота: джобы, источники, данные, память, диск."""
    lines = ['🩺 <b>Состояние бота</b>', '', '<b>Фоновые задачи</b>']
    lines.append(_job_line(context, 'anime_news_check', 'Автопроверка новостей'))
    lines.append(_job_line(context, 'scheduled_publish', 'Публикация отложки'))
    lines.append(_job_line(context, 'daily_backup', 'Ежедневный бэкап'))

    # --- Источники ---
    enabled = [n for n, _ in SOURCES if settings.is_source_enabled(n)]
    disabled = [n for n, _ in SOURCES if not settings.is_source_enabled(n)]
    lines.append('')
    lines.append(f'<b>Источники: {len(enabled)} вкл / {len(disabled)} на паузе</b>')
    problem = []
    if source_health is not None:
        for name in enabled:
            info = source_health.info(name)
            if int(info.get('fails', 0)):
                problem.append((name, source_health.silent_hours(name) or 0,
                                info.get('last_error', '')))
    problem.sort(key=lambda x: -x[1])
    if problem:
        for name, silent, err in problem[:8]:
            left = AUTO_DISABLE_AFTER_HOURS - silent
            tail = (f' (до паузы {left:.0f} ч)' if left > 0
                    else ' — пора на паузу')
            lines.append(f'  ⚠️ {name}: молчит {silent:.1f} ч{tail}')
            if err:
                lines.append(f'      {html.escape(err[:70])}')
    else:
        lines.append('  ✅ Все включённые источники отвечают')
    if disabled:
        lines.append(f'  ⏸ На паузе: {html.escape(", ".join(disabled[:10]))}')

    # --- Медиа ---
    lines.append('')
    lines.append('<b>Медиа</b>')
    lines.append('  🎬 Видео: ' + ('ВКЛ' if settings.video_enabled
                                   else '⚠️ ВЫКЛ — ролики не прикрепляются!'))
    lines.append(f'     до {TG_VIDEO_MAX_SECONDS // 60} мин, до {TG_VIDEO_MAX_MB} МБ')
    lines.append('  🔧 Запасная добыча видео (yt-dlp): '
                 + ('есть' if YT_DLP_AVAILABLE else '⚠️ НЕТ'))
    lines.append('  🖼 Только с картинками: '
                 + ('ВКЛ' if settings.require_image else 'ВЫКЛ'))
    ok_channel, channel_note = await _check_channel_access(context.bot)
    lines.append(('  📢 Канал: ' if ok_channel else '  ⚠️ Канал: ')
                 + html.escape(channel_note))
    lines.append(f'     ID {CHANNEL_ID} '
                 + ('(переменная CHANNEL_ID)' if CHANNEL_FROM_ENV else '(из кода)'))
    if CHANNEL_FROM_ENV and CHANNEL_ID != MAIN_CHANNEL_ID:
        lines.append(f'     ⚠️ не совпадает с основным {MAIN_CHANNEL_ID}')

    # --- Очереди ---
    lines.append('')
    lines.append('<b>Очереди</b>')
    sched_total = len(scheduled_posts.all()) if scheduled_posts is not None else 0
    sched_ripe = len(scheduled_posts.due()) if scheduled_posts is not None else 0
    lines.append(f'  📅 В отложке: {sched_total}'
                 + (f' (созрело: {sched_ripe})' if sched_ripe else ''))
    lines.append(f'  🗂 Ждут решения в ветке: {len(pending_posts._items) if pending_posts else 0}')
    lines.append(f'  🔗 История ссылок: {len(sent_links._set)}')
    lines.append(f'  🖼 Отпечатков картинок: {len(image_hashes) if image_hashes else 0}'
                 + ('' if Image is not None else ' (без Pillow — только точные копии)'))

    # --- Данные и система ---
    files = _data_files()
    total = sum(p.stat().st_size for p in files if p.exists())
    lines.append('')
    lines.append('<b>Данные и система</b>')
    lines.append(f'  💾 Файлов данных: {len(files)}, всего {_fmt_size(total)}')
    biggest = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:3]
    for p in biggest:
        lines.append(f'      {p.name}: {_fmt_size(p.stat().st_size)}')
    rss = _rss_mb()
    if rss is not None:
        lines.append(f'  🧠 Память процесса: {rss} МБ')
    try:
        usage = shutil.disk_usage(DATA_DIR)
        lines.append(f'  🗄 Диск: свободно {usage.free // (1024 * 1024)} МБ '
                     f'из {usage.total // (1024 * 1024)} МБ')
    except OSError:
        pass
    last_backup = settings.last_backup_date or 'ещё не делался'
    lines.append(f'  📦 Последний бэкап: {last_backup}')
    silence = _silence_hours()
    if silence is not None:
        mark = '⚠️' if silence >= WATCHDOG_SILENCE_HOURS else '🕓'
        lines.append(f'  {mark} Последняя публикация: {silence:.1f} ч назад')

    # --- Переводчик ---
    used, month = _deepl_usage_local()
    lines.append('')
    lines.append('<b>Переводчик</b>')
    if _llm_configured():
        today = _local_now().strftime('%Y-%m-%d')
        llm_used = settings.llm_calls_today if settings.llm_day == today else 0
        if not settings.llm_enabled:
            lines.append('  🤖 Модель: выключена в настройках')
        elif _llm_disabled_runtime:
            lines.append('  🤖 Модель: ⚠️ отключена из-за ошибок (см. /llm)')
        else:
            lines.append(f'  🤖 Модель {html.escape(LLM_MODEL)}: '
                         f'{llm_used} из {LLM_DAILY_LIMIT} вызовов сегодня')
    if DEEPL_API_KEY:
        share = used / DEEPL_MONTHLY_LIMIT * 100 if DEEPL_MONTHLY_LIMIT else 0
        lines.append(f'  📝 DeepL за {month}: {used} симв. (~{share:.0f}% лимита)')
        remote = await asyncio.to_thread(_deepl_usage_remote)
        if remote:
            r_used, r_limit = remote
            lines.append(f'      по данным DeepL: {r_used} из {r_limit}')
    else:
        lines.append('  📝 Google Translate (ключ DeepL не задан)')

    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)


def _build_backup_archive() -> Optional[tuple[bytes, str]]:
    """Собирает все файлы данных в zip в памяти. (bytes, имя файла) или None."""
    files = _data_files()
    if not files:
        return None
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                try:
                    zf.write(path, arcname=path.name)
                except OSError as e:
                    logger.warning(f"Бэкап: {path.name} не добавлен ({e})")
    except Exception as e:
        logger.error(f"Бэкап: архив не собрался ({e})")
        return None
    data = buf.getvalue()
    if len(data) < 30:                      # пустой архив
        return None
    stamp = _local_now().strftime('%Y-%m-%d')
    return data, f'anime_bot_backup_{stamp}.zip'


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в сутки присылает архив данных в личку админам.

    Именно в личку, а не в канал: внутри история ссылок, ID админов и настройки —
    в публичном канале им не место. Задача крутится ежечасно и срабатывает один
    раз за календарный день, после BACKUP_HOUR по времени админа."""
    if settings is None or not settings.daily_backup:
        return
    today = _local_now().strftime('%Y-%m-%d')
    if settings.last_backup_date == today:
        return
    if _local_now().hour < BACKUP_HOUR:
        return
    # Дату ставим сразу: даже если отправка сорвётся, повторять весь день не будем
    settings.last_backup_date = today
    archive = await asyncio.to_thread(_build_backup_archive)
    if not archive:
        logger.warning("Ежедневный бэкап: нечего архивировать")
        return
    data, filename = archive
    caption = (f'📦 Ежедневный бэкап данных бота\n'
               f'{filename} — {_fmt_size(len(data))}\n'
               f'Сохрани: при сбросе диска хостинга отсюда восстанавливается всё.')
    sent = 0
    for uid in _all_admin_ids():
        try:
            await context.bot.send_document(chat_id=uid, document=data,
                                            filename=filename, caption=caption)
            sent += 1
        except TelegramError as e:
            logger.warning(f"Бэкап не ушёл админу {uid}: {e}")
    logger.info(f"📦 Ежедневный бэкап отправлен ({sent} получателей, {_fmt_size(len(data))})")


@admin_only
async def backup_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Присылает админу все файлы данных бота (страховка на случай проблем с хостингом)."""
    files = [SENT_LINKS_FILE, QUEUE_FILE, SETTINGS_FILE, STATS_FILE, ANILIST_CACHE_FILE]
    await update.message.reply_text("📦 Собираю бэкап...")
    sent, skipped = 0, []
    for path in files:
        try:
            if not path.exists() or path.stat().st_size == 0:
                skipped.append(path.name)
                continue
            with path.open('rb') as f:
                await context.bot.send_document(
                    chat_id=ADMIN_ID, document=f, filename=path.name,
                )
            sent += 1
            await asyncio.sleep(0.3)
        except (TelegramError, OSError) as e:
            logger.warning(f"Бэкап {path.name} не отправился: {e}")
            skipped.append(path.name)
    msg = f"✅ Бэкап готов: отправлено {sent} файлов."
    if skipped:
        msg += f"\nПропущено (нет/пусто/ошибка): {', '.join(skipped)}"
    msg += "\n\nСохрани файлы — при переезде или сбросе данных их можно будет вернуть."
    await update.message.reply_text(msg)


@admin_only
async def stats_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Метрики бота: накопительные + за сутки/неделю + разбивка по источникам."""
    totals = stats.get_totals()
    by_source = stats.get_by_source()
    started_at = stats.get_started_at()

    now = datetime.now()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    published_24h = stats.count_events_since(day_ago, 'published')
    published_7d = stats.count_events_since(week_ago, 'published')
    failed_24h = stats.count_events_since(day_ago, 'failed_send')

    bot_age = ''
    if started_at:
        delta = now - started_at
        days = delta.days
        if days >= 1:
            bot_age = f'{days} дн.'
        else:
            hours = int(delta.total_seconds() / 3600)
            bot_age = f'{hours} ч.'

    # Общая сводка
    lines = [f'📊 <b>Метрики бота</b>']
    if bot_age:
        lines.append(f'⏱ Работает: {bot_age}')
    lines.append('')
    lines.append(f'<b>За всё время:</b>')
    lines.append(f'  📥 Собрано: {totals.get("collected", 0)}')
    lines.append(f'  📤 Опубликовано: {totals.get("published", 0)}')
    skipped_total = (
        totals.get('skipped_no_image', 0)
        + totals.get('skipped_too_old', 0)
        + totals.get('skipped_duplicate', 0)
        + totals.get('skipped_spam', 0)
        + totals.get('skipped_filtered', 0)
    )
    lines.append(f'  ⊘ Отброшено: {skipped_total}')
    lines.append(f'      без фото: {totals.get("skipped_no_image", 0)}')
    lines.append(f'      дубли: {totals.get("skipped_duplicate", 0)}')
    lines.append(f'  ⚠️ Ошибок отправки: {totals.get("failed_send", 0)}')
    lines.append(f'  💥 Ошибок источников: {totals.get("source_errors", 0)}')

    lines.append('')
    lines.append(f'<b>За последние:</b>')
    lines.append(f'  24 часа: 📤 {published_24h} опубликовано, ⚠️ {failed_24h} ошибок')
    lines.append(f'  7 дней:  📤 {published_7d} опубликовано')

    # Топ источников по публикациям
    if by_source:
        ranked = sorted(
            by_source.items(),
            key=lambda kv: -kv[1].get('published', 0),
        )
        lines.append('')
        lines.append(f'<b>📡 По источникам:</b>')
        for name, data in ranked:
            collected = data.get('collected', 0)
            published = data.get('published', 0)
            errors = data.get('errors', 0)
            last = _format_age(data.get('last_success_at'))
            err_str = f' ⚠️{errors}' if errors else ''
            lines.append(f'  • <b>{html.escape(name)}</b>: 📤{published} / 📥{collected}{err_str} ({last})')

    text = '\n'.join(lines)
    # Запас на 4096 — если будет очень много источников
    if len(text) > 4000:
        text = text[:4000] + '\n…'

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def blacklist_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий blacklist слов."""
    if not BLACKLIST:
        await update.message.reply_text(
            "📛 Blacklist пуст.\n\n"
            "Список редактируется в коде (константа BLACKLIST в начале файла). "
            "После изменения нужно перезапустить бота."
        )
        return
    lines = [f'📛 <b>Blacklist ({len(BLACKLIST)} слов):</b>\n']
    lines.append('Посты, содержащие эти слова, не публикуются.\n')
    for w in BLACKLIST:
        lines.append(f'  • {html.escape(w)}')
    lines.append('\nСписок редактируется в коде (константа <code>BLACKLIST</code>). '
                 'После изменения — перезапуск.')
    await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)


# ============== ТОЧКА ВХОДА ==============
def check_video_deps():
    """Проверяет наличие yt-dlp и ffmpeg, выводит предупреждения."""
    if not YT_DLP_AVAILABLE:
        logger.warning("⚠️  yt-dlp не установлен. Без него: не качаются видео с YouTube "
                       "и не работает запасной способ добычи видео из Telegram-постов.")
        logger.warning("    Добавь yt-dlp в requirements.txt и передеплой.")
    else:
        logger.info("✓ yt-dlp найден")

    if shutil.which('ffmpeg'):
        logger.info("✓ ffmpeg найден")
    else:
        logger.warning("⚠️  ffmpeg не найден. Для видео из Telegram он НЕ нужен — "
                       "нужен только для склейки раздельных дорожек с YouTube.")


async def setup_bot_commands(app: Application) -> None:
    """post_init: меню команд + джоб отложки. Джоб регистрируем здесь (после
    initialize) — канонично для PTB и сразу видно в логах, что он поднялся."""
    # Публикация отложенных постов: проверяем раз в минуту
    app.job_queue.run_repeating(
        publish_scheduled, interval=60, first=15, name='scheduled_publish',
        job_kwargs=JOB_KWARGS,
    )
    # Ежедневный бэкап: проверяем раз в час, срабатывает один раз в сутки
    app.job_queue.run_repeating(
        daily_backup_job, interval=3600, first=120, name='daily_backup',
        job_kwargs=JOB_KWARGS,
    )

    # Отчёт о запуске: сразу видно, поднялся ли деплой и что настроено
    try:
        await send_startup_report(app)
    except Exception as e:                     # отчёт не должен мешать старту
        logger.warning(f"Стартовый отчёт не отправлен: {e}")
    total = len(scheduled_posts.all()) if scheduled_posts is not None else 0
    ripe = len(scheduled_posts.due()) if scheduled_posts is not None else 0
    logger.info(f"🕰 Джоб отложки зарегистрирован (тик раз в 60с). "
                f"В отложке: {total}, из них созрело: {ripe}")
    print(f"Джоб отложки: зарегистрирован | постов в отложке: {total}", flush=True)
    # В синем меню Telegram держим то, чем пользуются часто. Остальные команды
    # (/deepl, /blacklist, /addsource, /delsource, /tz, /preview, /backup)
    # работают по-прежнему, просто не засоряют список из двух десятков строк.
    commands = [
        BotCommand("settings", "⚙️ Настройки"),
        BotCommand("scheduled", "📅 Отложенные посты"),
        BotCommand("news", "🔍 Проверить новости сейчас"),
        BotCommand("status", "📊 Что сейчас происходит"),
        BotCommand("health", "🩺 Состояние бота"),
        BotCommand("stats", "📈 Статистика"),
        BotCommand("sources", "📡 Источники"),
        BotCommand("llm", "🤖 Модель: статус и проверка"),
        BotCommand("admins", "👥 Администраторы"),
        BotCommand("logs", "📝 Логи"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        logger.info("✓ Команды установлены в меню Telegram")
    except TelegramError as e:
        logger.warning(f"Не удалось установить команды: {e}")


def _init_globals() -> None:
    """Инициализирует все глобальные инстансы (хранилища, кеши).
    Вызывается из main() при запуске бота. В тестах не вызывается —
    позволяет тестам создавать свои инстансы с временными файлами,
    не затрагивая реальные данные пользователя."""
    global sent_links, translator, post_queue, settings, stats, anilist, pending_posts
    if sent_links is None:
        sent_links = SentLinksStore(SENT_LINKS_FILE)
    if pending_posts is None:
        pending_posts = PendingPosts(PENDING_POSTS_FILE)
    global published_texts
    if published_texts is None:
        published_texts = PublishedTexts(PUBLISHED_TEXTS_FILE)
    global user_directory
    if user_directory is None:
        user_directory = UserDirectory(USER_DIRECTORY_FILE)
        if len(user_directory):
            logger.info(f"Знакомых пользователей: {len(user_directory)}")
    global source_health, image_hashes, recent_subjects
    if recent_subjects is None:
        recent_subjects = RecentSubjects(SUBJECT_MEMORY_FILE)
    if source_health is None:
        source_health = SourceHealth(SOURCE_HEALTH_FILE)
    if image_hashes is None:
        image_hashes = ImageHashes(IMAGE_HASHES_FILE)
        logger.info(f"Отпечатков картинок в базе: {len(image_hashes)}"
                    + ("" if Image is not None else " (Pillow нет — только точные копии)"))
    global scheduled_posts
    if scheduled_posts is None:
        scheduled_posts = ScheduledPosts(SCHEDULED_POSTS_FILE)
        if scheduled_posts.all():
            logger.info(f"Отложенных постов в очереди: {len(scheduled_posts.all())}")
    global custom_sources
    if custom_sources is None:
        custom_sources = CustomSources(CUSTOM_SOURCES_FILE)
        for _item in custom_sources.all():
            _attach_custom_source(_item)
        if custom_sources.all():
            logger.info(f"Динамических источников подключено: {len(custom_sources.all())}")
    if translator is None:
        translator = GoogleTranslator(source='auto', target='ru')
    if post_queue is None:
        post_queue = PostQueue(QUEUE_FILE)
    if settings is None:
        settings = BotSettings(SETTINGS_FILE)
    if stats is None:
        stats = BotStats(STATS_FILE)
    if anilist is None:
        anilist = AniListClient(ANILIST_CACHE_FILE)


def main():
    # Самый первый вывод — чтобы в логах хостинга было видно что процесс стартовал
    print("=== Запуск anime_news_bot ===", flush=True)
    print(f"DATA_DIR = {DATA_DIR}", flush=True)
    print(f"TOKEN задан: {'да' if TOKEN else 'НЕТ'}", flush=True)
    print(f"Переводчик: {'DeepL' if DEEPL_API_KEY else 'Google Translate'}", flush=True)

    try:
        _setup_file_logging()
    except Exception as e:
        print(f"Файловый лог не настроен (не критично): {e}", flush=True)

    # Проверка токена — на хостинге переменная окружения BOT_TOKEN обязательна
    if not TOKEN or TOKEN == '':
        print("❌ Токен бота не задан! Установите переменную окружения BOT_TOKEN.", flush=True)
        raise SystemExit("BOT_TOKEN не задан")

    _init_globals()
    check_video_deps()

    print("Создаю Application...", flush=True)
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(setup_bot_commands).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("preview", preview_command))
    app.add_handler(CommandHandler("start_auto", start_auto))
    app.add_handler(CommandHandler("stop_auto", stop_auto))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("chatinfo", chatinfo_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("deepl", deepl_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("videocheck", videocheck_command))
    app.add_handler(CommandHandler("llm", llm_command))
    app.add_handler(CommandHandler("scheduled", scheduled_command))
    app.add_handler(CommandHandler("tz", tz_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("sources", sources_command))
    app.add_handler(CommandHandler("addsource", addsource_command))
    app.add_handler(CommandHandler("delsource", delsource_command))
    app.add_handler(CommandHandler("admins", admins_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("deladmin", deladmin_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("blacklist", blacklist_command))
    app.add_handler(CommandHandler("settings", settings_command))

    # Inline-кнопки (callback_query)
    app.add_handler(CallbackQueryHandler(settings_callback))

    # Reply-кнопки (обычные текстовые сообщения с конкретным текстом)
    reply_button_texts = [BTN_NEWS, BTN_PREVIEW, BTN_START_AUTO, BTN_STOP_AUTO, BTN_STATUS, BTN_SETTINGS]
    reply_filter = filters.TEXT & filters.Regex(
        f"^({'|'.join(re.escape(t) for t in reply_button_texts)})$"
    )
    # Наблюдатель: тихо запоминает, кто пишет. Группа -3 — раньше всех, чтобы
    # успеть запомнить человека даже перед отказом в личке.
    app.add_handler(MessageHandler(filters.ALL, remember_user_handler), group=-3)
    # ЛС-гейт: посторонним в личке бот не отвечает (group=-2 — самый первый)
    app.add_handler(MessageHandler(filters.ALL, private_gate_handler), group=-2)
    # Ввод времени отложки / нового текста поста. Группа -1 = проверяется раньше
    # остальных; когда бот ничего не ждёт, сообщение уходит дальше по цепочке.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, awaiting_input_handler),
        group=-1,
    )
    app.add_handler(MessageHandler(reply_filter, reply_button_handler))

    print("✅ Бот запущен, начинаю polling...", flush=True)
    logger.info("✅ Бот запущен...")
    app.run_polling()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        print("❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ:", flush=True)
        traceback.print_exc()
        raise
