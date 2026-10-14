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
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger('anime_news_bot.tg_account')

SCHEME = 'tgacc://'
MEDIA_KINDS = ('photo', 'thumb', 'video')
_REF_RE = re.compile(r'^tgacc://([A-Za-z0-9_]{3,64})/(\d{1,12})/(photo|thumb|video)$')
_URL_RE = re.compile(r'https?://[^\s<>"\']+')

# Ошибки, после которых переподключаться бесполезно: вход сброшен, строка
# входа использована с двух адресов сразу, аккаунт удалён, неверный api_id.
# Без новой строки входа и перезапуска бота они повторятся каждый раз, а
# попытки каждые пять минут только мешали бы Telegram и забивали журнал.
FATAL_ERRORS = frozenset({
    'AuthKeyUnregisteredError', 'AuthKeyDuplicatedError', 'AuthKeyInvalidError',
    'AuthKeyNotFound', 'SessionRevokedError', 'SessionExpiredError',
    'UserDeactivatedError', 'UserDeactivatedBanError', 'PhoneNumberBannedError',
    'ApiIdInvalidError',
})
# Канал по сохранённому id и хэшу не находится: хэш чужого аккаунта или канал
# пересоздан. Тогда один раз узнаём канал по имени заново.
_STALE_PEER_ERRORS = frozenset({'ChannelInvalidError', 'PeerIdInvalidError'})


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


# ---------------------------------------------------------------- где лежат каналы

class PeerCache:
    """id и хэш доступа каналов на диске, чтобы не спрашивать Telegram заново.

    Узнать канал по имени (contacts.ResolveUsername) Telegram даёт редко:
    после пары сотен таких запросов в сутки — FLOOD_WAIT на часы. Строка входа
    узнанные каналы не хранит, и каждый перезапуск бота спрашивал все каналы
    заново, а перезапусков при выкладке бывает несколько за день. Хэш доступа
    у каждого аккаунта свой, поэтому файл помнит, чей он, и чужой не берёт.
    Через неделю запись обновляется: канал мог сменить название.
    """
    TTL_SEC = 7 * 24 * 3600

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.account = 0
        self.items: dict[str, dict] = {}

    def load(self, account_id: int) -> None:
        self.account = int(account_id or 0)
        self.items = {}
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            logger.warning('Кэш каналов аккаунта не прочитан (%s): узнаю каналы заново',
                           type(e).__name__)
            return
        if not isinstance(data, dict) or int(data.get('account') or 0) != self.account:
            return                         # другой аккаунт: его хэши нам не подходят
        for name, row in (data.get('channels') or {}).items():
            try:
                self.items[str(name).lower()] = {
                    'id': int(row['id']), 'hash': int(row['hash']),
                    'title': str(row.get('title') or ''), 'at': float(row.get('at') or 0)}
            except (KeyError, TypeError, ValueError):
                continue

    def get(self, channel: str) -> Optional[dict]:
        row = self.items.get(channel.lower())
        if row is None or time.time() - row['at'] > self.TTL_SEC:
            return None
        return row

    def put(self, channel: str, peer_id: int, access_hash: int, title: str) -> None:
        self.items[channel.lower()] = {'id': int(peer_id), 'hash': int(access_hash),
                                       'title': str(title or ''), 'at': time.time()}
        self._save()

    def drop(self, channel: str) -> None:
        if self.items.pop(channel.lower(), None) is not None:
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        body = json.dumps({'account': self.account, 'channels': self.items},
                          ensure_ascii=False, indent=1)
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding='utf-8')
            os.replace(tmp, self.path)
        except OSError as e:
            # Кэш — не данные: без него каналы просто узнаются заново.
            logger.warning('Кэш каналов аккаунта не записан: %s', type(e).__name__)


def _input_channel(peer_id: int, access_hash: int):
    from telethon.tl.types import InputPeerChannel
    return InputPeerChannel(channel_id=int(peer_id), access_hash=int(access_hash))


# ---------------------------------------------------------------- клиент

class AccountUnavailable(RuntimeError):
    """Аккаунт не подключён или запрос к нему не удался."""


class AccountReader:
    """Подключение к Telegram от имени аккаунта и чтение публичных каналов."""

    def __init__(self, api_id: int, api_hash: str, session: str, *,
                 client_factory: Optional[Callable[[], Any]] = None,
                 peer_cache_path: Optional[Path] = None):
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.session = str(session)
        self._client_factory = client_factory
        self.client = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        self.error = ''
        # True — переподключение не поможет (см. FATAL_ERRORS): сторож бота
        # тогда не дёргает Telegram каждые пять минут.
        self.fatal = False
        self.account_name = ''
        self.peers = PeerCache(peer_cache_path)
        self._resolved: dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        client = self.client
        if client is None or self.fatal or self.loop is None or self.loop.is_closed():
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
        self.fatal = False
        client = None
        me = None
        try:
            client = self._make_client()
            await asyncio.wait_for(client.connect(), timeout)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout):
                self.error = ('строка входа TG_ACCOUNT_SESSION не подходит: '
                              'вход сброшен или строка скопирована не целиком')
                self.fatal = True
                return False
            me = await asyncio.wait_for(client.get_me(), timeout)
        except asyncio.TimeoutError:
            self.error = 'Telegram не ответил на подключение'
            return False
        except Exception as e:
            self.error = f'{type(e).__name__}: {e}'
            self.fatal = type(e).__name__ in FATAL_ERRORS
            return False
        finally:
            # Неудачный клиент закрываем: его фоновые задачи иначе жили бы в
            # цикле бота, и каждое переподключение сторожа добавляло бы новые.
            if me is None and client is not None:
                await self._disconnect(client)
        self.client = client
        self.error = ''
        name = ' '.join(x for x in (getattr(me, 'first_name', ''), getattr(me, 'last_name', '')) if x)
        self.account_name = name or str(getattr(me, 'username', '') or 'аккаунт')
        self.peers.load(getattr(me, 'id', 0) or 0)
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

    async def _peer(self, client, channel: str) -> tuple[Any, str]:
        """Канал для запросов и его название: из кэша или, один раз, у Telegram.

        По имени канал узнаём только если его нет в кэше (см. :class:`PeerCache`);
        сами сообщения читаются по id и хэшу.
        """
        key = channel.lower()
        if key in self._resolved:
            return self._resolved[key]
        row = self.peers.get(channel)
        if row is not None:
            found = (_input_channel(row['id'], row['hash']), row['title'])
        else:
            entity = await client.get_entity(channel)
            title = str(getattr(entity, 'title', '') or '')
            peer_id, access_hash = getattr(entity, 'id', None), getattr(entity, 'access_hash', None)
            if isinstance(peer_id, int) and isinstance(access_hash, int):
                self.peers.put(channel, peer_id, access_hash, title)
                found = (_input_channel(peer_id, access_hash), title)
            else:
                found = (channel, title)   # не канал с хэшем — читаем по имени
        self._resolved[key] = found
        return found

    async def _with_peer(self, channel: str, call: Callable[[Any, Any], Awaitable]):
        """Запрос к каналу; устаревший хэш — узнать канал заново и повторить один раз."""
        client = self._require()
        peer, title = await self._peer(client, channel)
        try:
            return await call(client, peer), title
        except Exception as e:
            if type(e).__name__ not in _STALE_PEER_ERRORS or isinstance(peer, str):
                raise
            logger.info('Канал %s по сохранённому хэшу не нашёлся (%s), узнаю заново',
                        channel, type(e).__name__)
            self._resolved.pop(channel.lower(), None)
            self.peers.drop(channel)
            peer, title = await self._peer(client, channel)
            return await call(client, peer), title

    async def fetch(self, channel: str, limit: int = 20) -> tuple[str, list[AccountPost]]:
        """Название канала и его последние посты (новые сначала)."""
        # Альбом занимает несколько сообщений, поэтому берём с запасом.
        count = max(1, int(limit)) * 2
        messages, title = await self._with_peer(
            channel, lambda client, peer: client.get_messages(peer, limit=count))
        return title, posts_from_messages(channel, messages)

    async def _message(self, channel: str, msg_id: int):
        msg, _title = await self._with_peer(
            channel, lambda client, peer: client.get_messages(peer, ids=int(msg_id)))
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
        try:
            path = await self._require().download_media(msg, file=str(target))
        except BaseException:
            # Обрыв или таймаут посреди загрузки: Telethon оставляет недокачанный
            # файл, а ролик бывает в сотни мегабайт — диск хостинга не резиновый.
            target.unlink(missing_ok=True)
            raise
        if not path or not Path(path).exists() or Path(path).stat().st_size <= 0:
            target.unlink(missing_ok=True)
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
        except Exception as e:
            self._note_failure(e)
            raise

    def _note_failure(self, exc: BaseException) -> None:
        """Вход сбросили, пока бот работал: отключиться и больше не пробовать.

        Иначе клиент оставался «подключённым», каждый канал сначала ловил
        ошибку аккаунта и лишь потом шёл на веб-страницу, а в /status висело 🟢.
        """
        if type(exc).__name__ not in FATAL_ERRORS or self.fatal:
            return
        self.fatal = True
        self.error = f'{type(exc).__name__}: {exc}'
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: loop.create_task(self.stop()))
