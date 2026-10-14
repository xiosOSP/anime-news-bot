"""Аккаунт Telegram переживает обрывы, перезапуски и сброс входа.

Аудит октября 2026 нашёл, что после обрыва сети подольше аккаунт так и
оставался отключённым до перезапуска бота; каждый перезапуск заново узнавал
все каналы по имени (а на это у Telegram суточный лимит); пост с фото из
аккаунта при его недоступности отдавал Bot API ссылку tgacc:// и ронял альбом,
а отложенный пост за три минуты снимался с отложки. Сеть и Telegram подменены.
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot
import tg_account

ROOT = Path(__file__).resolve().parent.parent


def _error(name: str) -> Exception:
    """Исключение с именем класса Telethon: модуль узнаёт их по имени."""
    return type(name, (Exception,), {})('telegram says no')


class Client:
    """Клиент Telethon в миниатюре: что спросили и что ответить."""

    def __init__(self, *, me=None, entity=None, messages=(), fail_get_me=None, fail_peer=None):
        self.me = me if me is not None else NS(id=1001, first_name='Ридер', last_name='', username='r')
        self.entity = entity if entity is not None else NS(title='Канал', id=55, access_hash=777)
        self.messages = list(messages)
        self.fail_get_me = fail_get_me
        self.fail_peer = fail_peer          # (peer_id, hash) — на него ответить ошибкой
        self.connected = False
        self.resolved = []
        self.peers = []

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        if self.fail_get_me:
            raise self.fail_get_me
        return self.me

    async def disconnect(self):
        self.connected = False

    async def get_entity(self, channel):
        self.resolved.append(channel)
        return self.entity

    async def get_messages(self, peer, limit=None, ids=None):
        self.peers.append(peer)
        if self.fail_peer and (getattr(peer, 'channel_id', None), getattr(peer, 'access_hash', None)) \
                == self.fail_peer:
            raise _error('ChannelInvalidError')
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        return self.messages[:limit]


def _reader(client, cache=None):
    return tg_account.AccountReader(1, 'hash', 'session', client_factory=lambda: client,
                                    peer_cache_path=cache)


# ---------------------------------------------------------------- подключение

@pytest.mark.asyncio
async def test_failed_start_closes_the_client():
    client = Client(fail_get_me=RuntimeError('network'))
    reader = _reader(client)
    assert await reader.start() is False
    assert client.connected is False, 'неудачный клиент остался висеть в цикле бота'


@pytest.mark.asyncio
async def test_revoked_login_is_final_and_a_timeout_is_not():
    revoked = _reader(Client(fail_get_me=_error('AuthKeyUnregisteredError')))
    assert await revoked.start() is False and revoked.fatal
    slow = _reader(Client(fail_get_me=asyncio.TimeoutError()))
    assert await slow.start() is False and not slow.fatal


@pytest.mark.asyncio
async def test_login_revoked_while_working_takes_the_reader_down():
    reader = _reader(Client())
    assert await reader.start()

    async def revoked():
        raise _error('SessionRevokedError')
    box = {}

    def worker():
        try:
            reader.run_sync(revoked, 5)
        except Exception as e:
            box['error'] = e
    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert type(box['error']).__name__ == 'SessionRevokedError'
    assert reader.fatal and not reader.ready
    assert 'SessionRevokedError' in reader.error


@pytest.mark.asyncio
async def test_reader_is_down_right_after_the_login_is_revoked():
    """Отключение идёт задачей в цикле; до неё каналы уже не должны ждать аккаунт."""
    reader = _reader(Client())
    assert await reader.start()
    reader._note_failure(_error('AuthKeyDuplicatedError'))
    assert not reader.ready


# ---------------------------------------------------------------- каналы на диске

@pytest.mark.asyncio
async def test_channel_is_resolved_once_across_restarts(tmp_path):
    cache = tmp_path / 'peers.json'
    first = Client()
    reader = _reader(first, cache)
    await reader.start()
    title, _posts = await reader.fetch('Nexvlsz')
    assert title == 'Канал' and first.resolved == ['Nexvlsz']

    again = Client()
    reader = _reader(again, cache)
    await reader.start()
    title, _posts = await reader.fetch('nexvlsz')
    assert again.resolved == [], 'после перезапуска канал опять узнавали по имени'
    assert title == 'Канал'
    peer = again.peers[0]
    assert (peer.channel_id, peer.access_hash) == (55, 777)


@pytest.mark.asyncio
async def test_other_accounts_hashes_are_not_used(tmp_path):
    cache = tmp_path / 'peers.json'
    cache.write_text(json.dumps({'account': 1001, 'channels': {
        'chan': {'id': 55, 'hash': 777, 'title': 'Канал', 'at': time.time()}}}), encoding='utf-8')
    client = Client(me=NS(id=2002, first_name='Другой', last_name='', username='o'))
    reader = _reader(client, cache)
    await reader.start()
    await reader.fetch('chan')
    assert client.resolved == ['chan']


@pytest.mark.asyncio
async def test_old_entry_is_refreshed(tmp_path):
    cache = tmp_path / 'peers.json'
    stale = time.time() - tg_account.PeerCache.TTL_SEC - 60
    cache.write_text(json.dumps({'account': 1001, 'channels': {
        'chan': {'id': 55, 'hash': 777, 'title': 'Старое имя', 'at': stale}}}), encoding='utf-8')
    client = Client()
    reader = _reader(client, cache)
    await reader.start()
    title, _ = await reader.fetch('chan')
    assert client.resolved == ['chan'] and title == 'Канал'


@pytest.mark.asyncio
async def test_stale_hash_is_resolved_again_once(tmp_path):
    cache = tmp_path / 'peers.json'
    cache.write_text(json.dumps({'account': 1001, 'channels': {
        'chan': {'id': 55, 'hash': 111, 'title': 'Канал', 'at': time.time()}}}), encoding='utf-8')
    client = Client(fail_peer=(55, 111))
    reader = _reader(client, cache)
    await reader.start()
    await reader.fetch('chan')
    assert client.resolved == ['chan']
    saved = json.loads(cache.read_text(encoding='utf-8'))
    assert saved['channels']['chan']['hash'] == 777


@pytest.mark.asyncio
async def test_unreadable_cache_is_not_fatal(tmp_path):
    cache = tmp_path / 'peers.json'
    cache.write_text('{oops', encoding='utf-8')
    client = Client()
    reader = _reader(client, cache)
    assert await reader.start()
    await reader.fetch('chan')
    assert client.resolved == ['chan']


# ---------------------------------------------------------------- недокачанный ролик

@pytest.mark.asyncio
async def test_partial_video_is_removed_when_download_breaks(tmp_path):
    clip = NS(id=9, date=None, message='трейлер', video=object(), video_note=None, gif=None,
              file=NS(duration=90, size=50 * 1024 * 1024))
    client = Client(messages=[clip])

    async def broken(msg, file=None, thumb=None):
        Path(file).write_bytes(b'half')
        raise ConnectionError('обрыв')
    client.download_media = broken
    reader = _reader(client)
    await reader.start()
    with pytest.raises(ConnectionError):
        await reader.download_video('tgacc://chan/9/video', tmp_path, 300 * 1024 * 1024)
    assert list(tmp_path.iterdir()) == [], 'недокачанный ролик остался на диске'


# ---------------------------------------------------------------- сторож в боте

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(bot, 'TG_ACCOUNT_API_ID', 1)
    monkeypatch.setattr(bot, 'TG_ACCOUNT_API_HASH', 'h')
    monkeypatch.setattr(bot, 'TG_ACCOUNT_SESSION', 's')
    monkeypatch.setattr(bot, '_tg_account_start_task', None)
    monkeypatch.setattr(bot, '_tg_account_alert_sent', False)
    monkeypatch.setattr(bot, '_tg_account_down_since', 0.0)
    alerts = []
    monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)
    return alerts


def _down_account(**kw):
    async def stop():
        return None
    base = dict(ready=False, fatal=False, error='', account_name='Ридер', stop=stop)
    base.update(kw)
    return NS(**base)


@pytest.mark.asyncio
async def test_watchdog_reconnects_a_dropped_account(configured, monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', _down_account())
    calls = []
    gate = asyncio.Event()

    async def start():
        calls.append(1)
        await gate.wait()
    monkeypatch.setattr(bot, '_tg_account_start', start)
    bot._tg_account_watchdog()
    await asyncio.sleep(0)
    bot._tg_account_watchdog()                 # подключение ещё идёт — второго не надо
    await asyncio.sleep(0)
    gate.set()
    await bot._tg_account_start_task
    assert calls == [1]


@pytest.mark.asyncio
async def test_watchdog_does_not_retry_a_revoked_login(configured, monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', _down_account(fatal=True, error='SessionRevokedError'))
    monkeypatch.setattr(bot, '_tg_account_start', lambda: pytest.fail('вход сброшен — не переподключать'))
    bot._tg_account_watchdog()
    bot._tg_account_watchdog()
    assert len(configured) == 1 and 'SessionRevokedError' in configured[0]


@pytest.mark.asyncio
async def test_outage_gives_one_warning_and_one_all_clear(configured, monkeypatch):
    results = iter([False, False, True])

    class Reader:
        def __init__(self, *a, **k):
            self.error, self.account_name, self.fatal = 'Telegram не ответил', 'Ридер', False

        async def start(self, timeout):
            return next(results)

        async def stop(self):
            return None
    monkeypatch.setattr(bot.tg_account_mod, 'AccountReader', Reader)
    monkeypatch.setattr(bot, 'tg_account', None)
    for _ in range(3):
        await bot._tg_account_start()
    assert len(configured) == 2
    assert 'не подключился' in configured[0] and 'снова подключён' in configured[1]


@pytest.mark.asyncio
async def test_old_client_is_closed_before_reconnecting(configured, monkeypatch):
    stopped = []

    async def stop():
        stopped.append(1)
    monkeypatch.setattr(bot, 'tg_account', _down_account(stop=stop))

    class Reader:
        def __init__(self, *a, **k):
            self.error, self.account_name, self.fatal = '', 'Ридер', False

        async def start(self, timeout):
            return True
    monkeypatch.setattr(bot.tg_account_mod, 'AccountReader', Reader)
    await bot._tg_account_start()
    assert stopped == [1]


# ---------------------------------------------------------------- медиа без аккаунта

NEWS = {'title': 'Тайтл получил второй сезон', 'link': 'https://t.me/chan/123',
        'images': ['tgacc://chan/123/photo'], 'video': None}


def test_post_waits_while_the_account_reconnects(configured, monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', _down_account())
    monkeypatch.setattr(bot, '_tg_account_down_since', time.time() - 60)
    assert bot._tg_account_media_waits(dict(NEWS))
    assert not bot._tg_account_media_waits(dict(NEWS, images=['https://x.test/a.jpg']))


def test_post_stops_waiting_after_a_long_outage_or_a_revoked_login(configured, monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', _down_account())
    monkeypatch.setattr(bot, '_tg_account_down_since',
                        time.time() - bot.TG_ACCOUNT_MEDIA_WAIT_SEC - 60)
    assert not bot._tg_account_media_waits(dict(NEWS))
    monkeypatch.setattr(bot, '_tg_account_down_since', time.time())
    monkeypatch.setattr(bot, 'tg_account', _down_account(fatal=True))
    assert not bot._tg_account_media_waits(dict(NEWS))


def test_autopost_skips_a_post_waiting_for_the_account(configured, monkeypatch):
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    monkeypatch.setattr(bot, '_work_repeat_blocks', lambda news: False)
    monkeypatch.setattr(bot, 'tg_account', _down_account())
    assert bot._autopost_skip(dict(NEWS))
    monkeypatch.setattr(bot, 'tg_account', _down_account(ready=True))
    assert not bot._autopost_skip(dict(NEWS))


@pytest.mark.asyncio
async def test_health_probe_runs_the_account_watchdog(monkeypatch):
    runs = []
    monkeypatch.setattr(bot, '_tg_account_watchdog', lambda: runs.append(1))
    await bot.health_probe_job(NS(application=None, bot=None))
    assert runs == [1]


@pytest.mark.asyncio
async def test_waiting_post_is_deferred_before_its_pictures_are_touched(configured, monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', _down_account())

    async def touched(news):
        pytest.fail('картинки качались без аккаунта и выпали бы из поста')
    monkeypatch.setattr(bot, '_improve_thumb', touched)
    assert await bot._prepare_news_for_send(dict(NEWS), 'TG: chan') == 'deferred'


@pytest.mark.asyncio
async def test_scheduled_post_waits_without_spending_a_try(configured, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(bot, 'tg_account', _down_account())
    store = bot.ScheduledPosts(tmp_path / 'scheduled.json')
    key = store.add(dict(NEWS), datetime.now(timezone.utc) - timedelta(seconds=5))
    monkeypatch.setattr(bot, 'scheduled_posts', store)

    async def send(*a, **k):
        pytest.fail('пост ушёл без медиа')
    monkeypatch.setattr(bot, '_prepare_and_send_channel_post', send)
    await bot.publish_scheduled(NS(bot=object()))
    await bot.publish_scheduled(NS(bot=object()))
    assert store.get(key) is not None
    assert int(store.meta(key).get('tries') or 0) == 0


@pytest.mark.asyncio
async def test_account_photo_that_did_not_download_never_reaches_bot_api(monkeypatch):
    pictures = {'https://x.test/a.jpg': b'jpeg-a'}
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: pictures.get(url))
    resolved = await bot._resolve_photos_for_album(['tgacc://chan/1/photo', 'https://x.test/a.jpg'])
    assert resolved == ['https://x.test/a.jpg']


@pytest.mark.asyncio
async def test_same_picture_is_not_sent_twice_in_one_album(monkeypatch):
    pictures = {'tgacc://chan/1/photo': b'same', 'tgacc://chan/2/photo': b'same',
                'tgacc://chan/3/photo': b'other'}
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: pictures.get(url))
    resolved = await bot._resolve_photos_for_album(list(pictures))
    assert resolved == [b'same', b'other']


def test_web_page_of_the_post_gives_the_picture_when_the_account_is_down(monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', None)
    pages = []

    def og(page):
        pages.append(page)
        return 'https://cdn4.telesco.pe/file/photo.jpg'
    monkeypatch.setattr(bot, 'fetch_og_image', og)
    monkeypatch.setattr(bot, '_download_image_bytes',
                        lambda url: b'jpeg' if url == 'https://cdn4.telesco.pe/file/photo.jpg' else None)
    assert bot._tg_account_image_bytes('tgacc://chan/7/photo') == b'jpeg'
    assert pages == ['https://t.me/chan/7?single']
    assert bot._tg_account_image_bytes('tgacc://chan/7/video') is None


def test_deleted_post_gives_no_picture_not_the_channel_avatar(monkeypatch):
    def deleted(factory, timeout):
        raise tg_account.MessageMissing('сообщение chan/7 не найдено')
    monkeypatch.setattr(bot, 'tg_account', NS(ready=True, run_sync=deleted, download_bytes=lambda r, m: None))
    monkeypatch.setattr(bot, 'fetch_og_image', lambda page: pytest.fail('у удалённого поста там аватарка'))
    assert bot._tg_account_image_bytes('tgacc://chan/7/photo') is None


def test_slow_account_falls_back_to_the_web_page(monkeypatch):
    def slow(factory, timeout):
        raise tg_account.AccountUnavailable('Telegram не ответил за 40 с')
    monkeypatch.setattr(bot, 'tg_account', NS(ready=True, run_sync=slow, download_bytes=lambda r, m: None))
    monkeypatch.setattr(bot, 'fetch_og_image', lambda page: 'https://cdn4.telesco.pe/file/p.jpg')
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: b'jpeg')
    assert bot._tg_account_image_bytes('tgacc://chan/7/photo') == b'jpeg'


@pytest.mark.asyncio
async def test_reader_tells_a_deleted_message_apart():
    reader = _reader(Client(messages=[]))
    await reader.start()
    with pytest.raises(tg_account.MessageMissing):
        await reader.download_bytes('tgacc://chan/7/photo', 10 ** 6)


# ---------------------------------------------------------------- автопостинг из ветки

def test_autopost_stops_retrying_a_post_that_keeps_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=48))
    store = bot.PendingPosts(tmp_path / 'pending.json')
    older = store.add({'title': 'Старый пост', 'link': 'https://a.test/1'})
    time.sleep(0.01)
    fresh = store.add({'title': 'Свежий, но не уходит', 'link': 'https://a.test/2'})
    for _ in range(bot.AUTOPOST_MAX_FAILS):
        key, _news = store.next_for_autopost()
        assert key == fresh
        assert store.mark_channel_sending(key)
        assert store.mark_channel_failed(key)
    key, _news = store.next_for_autopost()
    assert key == older, 'неуходящий пост держал автопостинг'
    assert store.channel_state(fresh) == 'pending'      # кнопкой его опубликовать можно


# ---------------------------------------------------------------- хостинг

def test_hosting_smoke_checks_fast_crypto():
    sys.path.insert(0, str(ROOT / 'tools'))
    try:
        import hosting_smoke
    finally:
        sys.path.remove(str(ROOT / 'tools'))
    assert 'cryptg' in hosting_smoke.cryptg_ready()
