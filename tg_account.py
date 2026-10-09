"""Чтение публичных Telegram-каналов через аккаунт (MTProto, библиотека Telethon).

Зачем: веб-страница t.me/s/<канал> отдаёт ролик, только пока он маленький.
Трейлер на полторы минуты в хорошем качестве она показывает как «Media is too
big», и бот публиковал пост без видео — с чёрным кадром или вовсе без медиа.
Аккаунт видит канал так же, как приложение Telegram: ролик любого размера,
настоящую обложку и полный текст поста.

Модуль ничего не знает о боте. Он отдаёт посты как простые записи
(:class:`AccountPost`) и скачивает медиа по ссылкам вида
``tgacc://канал/номер/вид``. Превращение записей в новости, фильтры и отправка
остаются в боте — у поста из аккаунта тот же путь, что у поста с веб-страницы.

Клиент живёт в том же цикле событий, что и бот. Сбор источников идёт в
отдельных потоках, поэтому для них есть :meth:`AccountReader.run_sync`:
корутина уходит в цикл бота, а поток ждёт ответа с таймаутом.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger('anime_news_bot.tg_account')

SCHEME = 'tgacc://'
MEDIA_KINDS = ('photo', 'thumb', 'video')
_REF_RE = re.compile(r'^tgacc://([A-Za-z0-9_]{3,64})/(\d{1,12})/(photo|thumb|video)$')
_URL_RE = re.compile(r'https?://[^\s<>"\']+')


def media_ref(channel: str, msg_id: int, kind: str) -> str:
    """Ссылка на медиа поста: её понимают загрузчики картинок и видео бота."""
    if kind not in MEDIA_KINDS:
        raise ValueError(f'неизвестный вид медиа: {kind}')
    return f'{SCHEME}{channel}/{int(msg_id)}/{kind}'


def parse_media_ref(ref: str) -> Optional[tuple[str, int, str]]:
    """«tgacc://канал/12/photo» → ('канал', 12, 'photo'); чужая строка → None.

    Строгий разбор: ссылка лежит в очереди и в JSON-хранилищах, и всё, что на
    неё не похоже, не должно превращаться в запрос к Telegram.
    """
    match = _REF_RE.match(str(ref or ''))
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


def is_media_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SCHEME)


@dataclass
class AccountPost:
    """Пост канала в том виде, в каком он нужен боту."""
    channel: str
    msg_id: int
    date: datetime
    text: str
    links: list[str] = field(default_factory=list)
    photo_refs: list[str] = field(default_factory=list)
    video_ref: str = ''
    video_thumb_ref: str = ''
    video_duration: Optional[int] = None
    video_size: Optional[int] = None


# ---------------------------------------------------------------- разбор постов

def _message_links(msg) -> list[str]:
    """Ссылки поста: из текста, скрытые под словами и на кнопках.

    По ним бот узнаёт рекламу (кнопка «Играть» на бота с ?start=) и анонс
    собственного выпуска канала — так же, как по ссылкам с веб-страницы.
    """
    links: list[str] = []
    for entity in getattr(msg, 'entities', None) or ():
        url = getattr(entity, 'url', None)
        if isinstance(url, str) and url:
            links.append(url)
    links.extend(_URL_RE.findall(str(getattr(msg, 'message', '') or '')))
    markup = getattr(msg, 'reply_markup', None)
    for row in getattr(markup, 'rows', None) or ():
        for button in getattr(row, 'buttons', None) or ():
            url = getattr(button, 'url', None)
            if isinstance(url, str) and url:
                links.append(url)
    return list(dict.fromkeys(links))[:20]


def _is_video(msg) -> bool:
    # Кружки и гифки — не ролики для поста: кружок обрезан в круг, гифка без звука.
    return bool(getattr(msg, 'video', None)) and not getattr(msg, 'video_note', None) \
        and not getattr(msg, 'gif', None)


def _video_numbers(msg) -> tuple[Optional[int], Optional[int]]:
    file = getattr(msg, 'file', None)
    duration = getattr(file, 'duration', None)
    size = getattr(file, 'size', None)
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return duration, size


def posts_from_messages(channel: str, messages) -> list[AccountPost]:
    """Сообщения канала (новые сначала) → посты, альбомы склеены в один пост.

    Альбом в Telegram — несколько сообщений с общим grouped_id; текст обычно
    только у одного из них. Ссылкой поста служит сообщение с текстом: на нём
    стоит подпись, и его же показывает веб-страница канала.
    """
    groups: dict = {}
    order: list = []
    for msg in messages or ():
        if msg is None or getattr(msg, 'action', None) is not None:
            continue                       # служебное: закрепление, смена аватарки
        key = getattr(msg, 'grouped_id', None) or ('single', getattr(msg, 'id', 0))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(msg)
    posts: list[AccountPost] = []
    for key in order:
        items = sorted(groups[key], key=lambda m: int(getattr(m, 'id', 0) or 0))
        with_text = [m for m in items if str(getattr(m, 'message', '') or '').strip()]
        if not with_text:
            continue                       # альбом без подписи: новости в нём нет
        head = with_text[0]
        post = AccountPost(channel=channel, msg_id=int(head.id), date=head.date,
                           text=str(head.message), links=_message_links(head))
        for msg in items:
            if getattr(msg, 'photo', None):
                post.photo_refs.append(media_ref(channel, msg.id, 'photo'))
            elif _is_video(msg) and not post.video_ref:
                post.video_ref = media_ref(channel, msg.id, 'video')
                post.video_thumb_ref = media_ref(channel, msg.id, 'thumb')
                post.video_duration, post.video_size = _video_numbers(msg)
        posts.append(post)
    return posts


# ---------------------------------------------------------------- клиент

class AccountUnavailable(RuntimeError):
    """Аккаунт не подключён или запрос к нему не удался."""


class AccountReader:
    """Подключение к Telegram от имени аккаунта и чтение публичных каналов."""

    def __init__(self, api_id: int, api_hash: str, session: str, *,
                 client_factory: Optional[Callable[[], Any]] = None):
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.session = str(session)
        self._client_factory = client_factory
        self.client = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        self.error = ''
        self.account_name = ''
        self._titles: dict[str, str] = {}

    @property
    def ready(self) -> bool:
        client = self.client
        if client is None or self.loop is None or self.loop.is_closed():
            return False
        try:
            return bool(client.is_connected())
        except Exception:
            return False

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        # Обновления не нужны: бот только читает каналы по запросу. Без них
        # клиент не держит в памяти состояние всех диалогов аккаунта.
        return TelegramClient(StringSession(self.session), self.api_id, self.api_hash,
                              receive_updates=False, flood_sleep_threshold=60,
                              request_retries=3, connection_retries=3)

    async def start(self, timeout: float = 40.0) -> bool:
        """Подключается. False — с понятной причиной в ``self.error``."""
        self.loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        try:
            client = self._make_client()
            await asyncio.wait_for(client.connect(), timeout)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout):
                self.error = ('строка входа TG_ACCOUNT_SESSION не подходит: '
                              'вход сброшен или строка скопирована не целиком')
                await self._disconnect(client)
                return False
            me = await asyncio.wait_for(client.get_me(), timeout)
        except asyncio.TimeoutError:
            self.error = 'Telegram не ответил на подключение'
            return False
        except Exception as e:
            self.error = f'{type(e).__name__}: {e}'
            return False
        self.client = client
        self.error = ''
        name = ' '.join(x for x in (getattr(me, 'first_name', ''), getattr(me, 'last_name', '')) if x)
        self.account_name = name or str(getattr(me, 'username', '') or 'аккаунт')
        return True

    @staticmethod
    async def _disconnect(client) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception:
            pass

    async def stop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await self._disconnect(client)

    def _require(self):
        if not self.ready:
            raise AccountUnavailable(self.error or 'аккаунт не подключён')
        return self.client

    async def fetch(self, channel: str, limit: int = 20) -> tuple[str, list[AccountPost]]:
        """Название канала и его последние посты (новые сначала)."""
        client = self._require()
        if channel not in self._titles:
            # Имя канала разрешаем один раз: у Telegram на это жёсткий лимит,
            # а сами сообщения читаются по уже известному каналу.
            entity = await client.get_entity(channel)
            self._titles[channel] = str(getattr(entity, 'title', '') or '')
        # Альбом занимает несколько сообщений, поэтому берём с запасом.
        messages = await client.get_messages(channel, limit=max(1, int(limit)) * 2)
        return self._titles[channel], posts_from_messages(channel, messages)

    async def _message(self, channel: str, msg_id: int):
        client = self._require()
        msg = await client.get_messages(channel, ids=int(msg_id))
        if msg is None:
            raise AccountUnavailable(f'сообщение {channel}/{msg_id} не найдено')
        return msg

    async def download_bytes(self, ref: str, max_bytes: int) -> Optional[bytes]:
        """Фото или обложка ролика байтами; None — если медиа нет или оно больше лимита."""
        parsed = parse_media_ref(ref)
        if parsed is None or parsed[2] == 'video':
            return None
        channel, msg_id, kind = parsed
        msg = await self._message(channel, msg_id)
        client = self._require()
        if kind == 'thumb':
            data = await client.download_media(msg, file=bytes, thumb=-1)
        else:
            if not getattr(msg, 'photo', None):
                return None
            data = await client.download_media(msg, file=bytes)
        if not isinstance(data, (bytes, bytearray)) or not data or len(data) > max_bytes:
            return None
        return bytes(data)

    async def download_video(self, ref: str, dest_dir: Path, max_bytes: int) -> tuple[Optional[Path], str]:
        """Ролик файлом в ``dest_dir``. Вторым значением — что случилось, для /logs."""
        parsed = parse_media_ref(ref)
        if parsed is None or parsed[2] != 'video':
            return None, 'ссылка не на ролик'
        channel, msg_id, _kind = parsed
        msg = await self._message(channel, msg_id)
        if not _is_video(msg):
            return None, 'в сообщении больше нет ролика'
        _duration, size = _video_numbers(msg)
        if size is not None and size > max_bytes:
            return None, f'ролик {size // (1024 * 1024)} МБ больше лимита {max_bytes // (1024 * 1024)} МБ'
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f'tgacc-{channel}-{msg_id}.mp4'
        path = await self._require().download_media(msg, file=str(target))
        if not path or not Path(path).exists() or Path(path).stat().st_size <= 0:
            return None, 'ролик не скачался'
        return Path(path), f'скачан через аккаунт ({Path(path).stat().st_size // 1024} КБ)'

    def run_sync(self, factory: Callable[[], Awaitable], timeout: float):
        """Выполняет корутину в цикле бота и ждёт результат из рабочего потока.

        Из самого цикла так ждать нельзя — он бы встал навсегда, поэтому там
        это ошибка, а не тихое зависание.
        """
        loop = self.loop
        if loop is None or loop.is_closed() or not self.ready:
            raise AccountUnavailable(self.error or 'аккаунт не подключён')
        if threading.get_ident() == self._loop_thread:
            raise AccountUnavailable('синхронный вызов из цикла событий')
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        try:
            return future.result(timeout)
        # На Python 3.10 это не встроенный TimeoutError, а свой класс модуля.
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise AccountUnavailable(f'Telegram не ответил за {timeout:.0f} с') from None
