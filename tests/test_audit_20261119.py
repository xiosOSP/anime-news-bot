"""Аудит 19 ноября 2026: журнал публикаций при отказах диска и отмене.

Внешний аудит нашёл: отказ записи журнала засчитывался как «дубль», и
новость уходила из очереди; файл истории с `null` давал пустую историю без
паузы и затирал проверенную копию; отмена задачи во время отказа диска
пропускала откат, и расхождение памяти с диском уезжало на диск следующей
записью; /historyok при отказе записи снимал паузу в памяти. Все сценарии —
на настоящих временных файлах.
"""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


def _published(path, link='https://e.com/1', title='Опубликованная новость'):
    store = bot.SentLinksStore(path)

    async def publish():
        assert await store.claim(link, title) is True
        assert await store.commit(link, title) is True
    asyncio.run(publish())
    return store


def _on_disk(path):
    return json.loads(path.read_text(encoding='utf-8'))


# ---------- отмена во время записи ----------

def _cancel_during_write(store, operation, *, write_ok: bool):
    """Отменяет транзакцию, пока поток-писатель держит запись, и отпускает её."""
    gate = threading.Event()
    real = store._write_snapshot

    def held(snapshot):
        gate.wait(5)
        return real(snapshot) if write_ok else False
    store._write_snapshot = held

    async def main():
        task = asyncio.create_task(operation())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    store._write_snapshot = real


def test_cancelled_clear_with_a_failed_write_is_rolled_back(tmp_path):
    path = tmp_path / 'sent.json'
    store = _published(path)
    _cancel_during_write(store, store.clear, write_ok=False)
    assert store._urls == ['https://e.com/1']             # откат прошёл, отмена — после
    # Следующая удачная запись не уносит на диск опустевшую историю.
    asyncio.run(store.claim('https://e.com/3', 'Новая новость'))
    assert 'https://e.com/1' in _on_disk(path)['urls']


def test_cancelled_claim_with_a_failed_write_is_rolled_back(tmp_path):
    path = tmp_path / 'sent.json'
    store = _published(path)
    _cancel_during_write(store, lambda: store.claim('https://e.com/2', 'Другая новость'),
                         write_ok=False)
    assert 'https://e.com/2' not in store and not store._reservations
    assert _on_disk(path)['urls'] == ['https://e.com/1']


def test_cancelled_claim_with_a_good_write_stays_durable(tmp_path):
    """Запись удалась — резерв и в памяти, и на диске; задача всё равно отменена."""
    path = tmp_path / 'sent.json'
    store = _published(path)
    _cancel_during_write(store, lambda: store.claim('https://e.com/2', 'Другая новость'),
                         write_ok=True)
    assert 'https://e.com/2' in store
    assert 'https://e.com/2' in _on_disk(path)['reservations']


def test_second_cancel_during_the_write_does_not_skip_the_rollback(tmp_path):
    path = tmp_path / 'sent.json'
    store = _published(path)
    gate = threading.Event()
    store._write_snapshot = lambda snapshot: (gate.wait(5), False)[1]

    async def main():
        task = asyncio.create_task(store.claim('https://e.com/2', 'Другая новость'))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert 'https://e.com/2' not in store and not store._reservations


def test_next_transaction_is_not_cancelled_by_a_stale_flag(tmp_path):
    path = tmp_path / 'sent.json'
    store = _published(path)
    _cancel_during_write(store, lambda: store.claim('https://e.com/2', 'Другая новость'),
                         write_ok=True)
    assert asyncio.run(store.claim('https://e.com/4', 'Четвёртая новость')) is True
    # Транзакция, упавшая с ошибкой после записи, оставила бы флаг висеть:
    # следующая, ни в чём не виноватая, не должна из-за него отмениться.
    store._cancel_after_write = True
    assert asyncio.run(store.claim('https://e.com/5', 'Пятая новость')) is True


# ---------- отказ диска — не дубль ----------

def _broken_writes(store):
    store._write_snapshot = lambda snapshot: False


def test_failed_claim_is_not_a_duplicate(tmp_path):
    store = bot.SentLinksStore(tmp_path / 'sent.json')
    _broken_writes(store)
    result = asyncio.run(store.claim('https://e.com/5', 'Свежая новость'))
    assert result is bot.LEDGER_UNAVAILABLE and not result
    assert 'https://e.com/5' not in store


@pytest.fixture
def broken_ledger(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'sent.json')
    _broken_writes(store)
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda news: True)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    alerts = []
    monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)
    monkeypatch.setattr(bot, '_ledger_unavailable_alert_at', 0.0)
    return alerts


def _fresh_news(i=1):
    return {'title': f'Анонсирован сезон аниме номер {i}', 'link': f'https://e.com/news/{i}',
            'summary': 'Текст новости.', 'source': 'TG: A'}


def test_send_paths_report_storage_not_duplicate(broken_ledger):
    telegram = MagicMock()
    assert asyncio.run(bot.send_news(telegram, _fresh_news(1))) == 'storage'
    assert asyncio.run(bot.send_news_to_thread(telegram, _fresh_news(2))) == 'storage'
    bot.stats.record_skipped.assert_not_awaited()          # в «дубли» не записано
    assert len(broken_ledger) == 1                          # админ узнал — один раз, а не на каждую


def test_queue_keeps_the_news_while_the_ledger_cannot_write(broken_ledger, tmp_path, monkeypatch):
    """Сценарий аудита: очередь пишется, журнал — нет. Новость остаётся в
    очереди, попытки не тратятся, в Telegram ничего не уходит."""
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.require_image = False
    monkeypatch.setattr(bot, 'settings', settings)
    queue = bot.PostQueue(tmp_path / 'post_queue.json')
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, '_llm_prefetch_queue_head', AsyncMock(return_value=0))
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock())
    telegram = MagicMock()
    for name in ('send_message', 'send_photo', 'send_video', 'send_media_group'):
        setattr(telegram, name, AsyncMock())

    async def main():
        assert await queue.push_many([_fresh_news(7)]) == 1
        result, _post = await bot._publish_one_from_queue_locked(telegram)
        return result, await queue.peek_size()
    result, size = asyncio.run(main())
    assert (result, size) == ('storage', 1)
    for name in ('send_message', 'send_photo', 'send_video', 'send_media_group'):
        getattr(telegram, name).assert_not_awaited()
    stored = _on_disk(tmp_path / 'post_queue.json')
    row = (stored.get('items') or [{}])[0].get('news') or {}
    assert not row.get('_queue_send_failures')              # попытка не потрачена


# ---------- файл истории неожиданного вида ----------

@pytest.fixture
def alerts(monkeypatch):
    seen = []
    monkeypatch.setattr(bot, '_queue_admin_alert', seen.append)
    return seen


def test_null_history_is_restored_from_the_good_copy(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    store = _published(path)
    store._snapshot_lastgood()
    copy = store._lastgood_path().read_bytes()
    path.write_text('null', encoding='utf-8')
    again = bot.SentLinksStore(path)
    assert again._urls == ['https://e.com/1'] and not again.history_lost
    assert again._lastgood_path().read_bytes() == copy       # копию не затёрло


@pytest.mark.parametrize('content', ['null', '42', '"строка"'])
def test_non_history_without_a_copy_pauses_publication(tmp_path, alerts, content):
    path = tmp_path / 'sent_links.json'
    path.write_text(content, encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert store.history_lost                                # пауза, а не пустая история
    assert not store._lastgood_path().exists()


def test_newer_format_is_left_untouched_and_blocks_publication(tmp_path, alerts):
    """Откат версии бота: файл новее этого кода. Ни сам файл, ни копия не
    переписываются, публикации стоят, админ знает."""
    path = tmp_path / 'sent_links.json'
    store = _published(path)
    store._snapshot_lastgood()
    copy = store._lastgood_path().read_bytes()
    future = {'schema_version': bot.SENT_LINKS_SCHEMA + 1, 'urls': ['https://e.com/1'],
              'titles': [], 'new_field': {'kept': True}}
    raw = json.dumps(future, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    again = bot.SentLinksStore(path)
    assert asyncio.run(again.claim('https://e.com/9', 'Новость девять')) is bot.LEDGER_UNAVAILABLE
    assert path.read_bytes() == raw                           # байт в байт
    assert again._lastgood_path().read_bytes() == copy
    assert any('новее' in text or 'более новой версией' in text for text in alerts)


@pytest.mark.parametrize('version', [1, 0, None])
def test_current_or_old_format_still_loads(tmp_path, alerts, version):
    path = tmp_path / 'sent_links.json'
    data = {'urls': ['https://e.com/1'], 'titles': []}
    if version is not None:
        data['schema_version'] = version
    path.write_text(json.dumps(data), encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert 'https://e.com/1' in store and not getattr(store, '_read_failed', False)


# ---------- /historyok ----------

def test_failed_historyok_keeps_the_pause_in_memory(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert store.history_lost
    _broken_writes(store)
    assert asyncio.run(store.accept_lost_history()) is False
    assert store.history_lost                                # ответ команды и память совпадают


def test_historyok_lifts_the_pause_for_good(tmp_path, alerts):
    path = tmp_path / 'sent_links.json'
    path.write_text('{broken', encoding='utf-8')
    store = bot.SentLinksStore(path)
    assert asyncio.run(store.accept_lost_history()) is True
    assert not store.history_lost and not bot.SentLinksStore(path).history_lost


# ---------- разбор Filmix: адрес со страницы — не часть селектора ----------

def _filmix(monkeypatch, html_text):
    from conftest import FakeHTTPResponse
    monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: FakeHTTPResponse(text=html_text))
    return bot.get_filmix()


def test_filmix_preview_is_found_by_address(monkeypatch):
    page = ('<a href="/mnews/1-x.html"><img src="/a.jpg"></a>'
            '<a href="/mnews/2-y.html"><span>без картинки</span></a>'
            '<h2><a href="/mnews/1-x.html">Первая новость про трейлер сезона</a></h2>'
            '<h2><a href="/mnews/2-y.html">Вторая новость без превью вообще</a></h2>')
    posts = _filmix(monkeypatch, page)
    assert [p['images'] for p in posts] == [['https://filmix.gg/a.jpg'], []]


@pytest.mark.parametrize('href', [
    '/mnews/1-x"] img, a[href*="',                     # кавычка дописывает к селектору своё
    '/mnews/1-x"]' + ':is(' * 300 + 'a' + ')' * 300,   # глубокая вложенность
    '/mnews/1-x' + '\\9' * 2000,                       # серия экранирований
])
def test_filmix_address_cannot_inject_a_selector(monkeypatch, href):
    """Раньше адрес шёл в a[href="{href}"]: кавычка ломала разбор всего
    источника или подсовывала парсеру тяжёлый селектор."""
    import html as html_lib
    import time as time_lib
    quoted = html_lib.escape(href, quote=True)
    page = (f'<a href="{quoted}"><img src="/evil.jpg"></a>'
            f'<h2><a href="{quoted}">Новость с подозрительным адресом ссылки</a></h2>'
            '<a href="/mnews/2-y.html"><img src="/b.jpg"></a>'
            '<h2><a href="/mnews/2-y.html">Обычная новость с картинкой превью</a></h2>')
    started = time_lib.perf_counter()
    posts = _filmix(monkeypatch, page)
    assert time_lib.perf_counter() - started < 1.0
    assert posts[-1]['images'] == ['https://filmix.gg/b.jpg']   # соседняя новость цела
    assert posts[0]['images'] == ['https://filmix.gg/evil.jpg']  # и у этой — своя картинка, не чужая


# ---------- бэкап: опись и проверка восстановления ----------

@pytest.fixture
def data_dir(tmp_path, monkeypatch, alerts):
    root = tmp_path / 'data'
    root.mkdir()
    monkeypatch.setattr(bot, 'DATA_DIR', root)
    monkeypatch.setattr(bot, 'settings', MagicMock(tz_offset=3))
    _published(root / 'sent_links.json')
    (root / 'bot_settings.json').write_text('{"schema_version": 1}', encoding='utf-8')
    (root / 'stats.json').write_text('{"sources": {}}', encoding='utf-8')
    return root


def _selftest_of_archive():
    data, _name = bot._build_backup_archive()
    return bot._backup_restore_selftest(data)


def test_good_backup_passes_and_lists_its_files(data_dir):
    result = _selftest_of_archive()
    assert result['ok'] is True, result['errors']
    assert 'sent_links' in result['stores'] and 'settings' in result['stores']


def test_unreadable_file_fails_the_backup(data_dir, monkeypatch):
    """Раньше файл, который не прочитался, просто не попадал в архив, и
    архив без истории публикаций проходил проверку."""
    real = bot.Path.read_bytes

    def read_bytes(self):
        if self.name == 'sent_links.json':
            raise PermissionError('нет доступа')
        return real(self)
    monkeypatch.setattr(bot.Path, 'read_bytes', read_bytes)
    result = _selftest_of_archive()
    assert result['ok'] is False
    assert any('sent_links.json' in error for error in result['errors'])


def test_archive_without_a_manifest_does_not_pass(tmp_path):
    """Сценарий аудита: ZIP с одним вспомогательным JSON."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('stats.json', '{}')
    result = bot._backup_restore_selftest(buf.getvalue())
    assert result['ok'] is False and 'в архиве нет описи файлов' in result['errors']


@pytest.mark.parametrize('damage', ['drop', 'change', 'extra'])
def test_archive_that_disagrees_with_its_manifest_fails(data_dir, damage):
    import io
    import zipfile
    data, _name = bot._build_backup_archive()
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name in src.namelist():
            raw = src.read(name)
            if name == 'sent_links.json':
                if damage == 'drop':
                    continue
                if damage == 'change':
                    raw = raw.replace(b'e.com/1', b'e.com/X')
            zf.writestr(name, raw)
        if damage == 'extra':
            zf.writestr('surprise.json', '{}')
    check = bot._verify_backup_archive(buf.getvalue())
    assert check['ok'] is False and check['errors']


def test_broken_history_in_the_archive_fails_the_restore_check(data_dir):
    (data_dir / 'sent_links.json').write_text('null', encoding='utf-8')
    result = _selftest_of_archive()
    assert result['ok'] is False
    assert any('sent_links' in error or 'история' in error for error in result['errors'])


def test_history_that_loads_only_from_a_copy_fails_the_restore_check(data_dir, monkeypatch):
    """Второй рубеж после миграции: история, которая поднялась бы на паузе
    или из копии, — это не восстановление."""
    monkeypatch.setattr(bot, '_migrate_runtime_schemas', lambda root: {'ok': True, 'changed': []})
    (data_dir / 'sent_links.json').write_text('null', encoding='utf-8')
    result = _selftest_of_archive()
    assert result['ok'] is False
    assert 'история публикаций в архиве не читается' in result['errors']


def test_restored_manifest_is_not_backed_up_again(data_dir):
    (data_dir / bot.BACKUP_MANIFEST_NAME).write_text('{"files": {}}', encoding='utf-8')
    assert bot.BACKUP_MANIFEST_NAME not in [p.name for p in bot._data_files()]
    assert _selftest_of_archive()['ok'] is True


# ---------- частичная доставка альбома ----------

def _fallback(telegram, photos, news):
    return asyncio.run(bot._send_post_fallback(
        telegram, news, -100, None, None, photos, 'подпись', 'текст', False))


def test_lost_extra_photo_is_counted_but_the_post_is_sent(monkeypatch):
    from telegram.error import BadRequest
    monkeypatch.setattr(bot.asyncio, 'sleep', AsyncMock())
    telegram = MagicMock()
    telegram.send_photo = AsyncMock(side_effect=[None, BadRequest('wrong file'), None])
    telegram.send_message = AsyncMock()
    news = {'title': 'Альбом из трёх кадров', 'source': 'TG: A'}
    assert _fallback(telegram, ['a', 'b', 'c'], news) is True     # пост вышел — не повторять
    assert news['_media_partial'] == {'planned': 3, 'sent': 2}
    telegram.send_message.assert_not_awaited()


def test_complete_album_is_not_marked_partial(monkeypatch):
    monkeypatch.setattr(bot.asyncio, 'sleep', AsyncMock())
    telegram = MagicMock()
    telegram.send_photo = AsyncMock()
    news = {'title': 'Альбом из двух кадров', 'source': 'TG: A'}
    assert _fallback(telegram, ['a', 'b'], news) is True
    assert '_media_partial' not in news


# ---------- модель модерации: http только на локальный адрес ----------

@pytest.mark.parametrize('url, allowed', [
    ('https://api.mistral.ai/v1', True),
    ('http://api.mistral.ai/v1', False),          # ключ и реплики чата — открытым текстом
    ('http://192.168.1.10:8080/v1', False),       # сеть хостинга — тоже чужая
    ('http://localhost:11434/v1', True),          # свой локальный сервер модели
    ('http://127.0.0.1:8080/v1', True),
    ('http://[::1]:8080/v1', True),
])
def test_moderation_model_sends_the_key_over_http_only_locally(tmp_path, url, allowed):
    from moderation_llm import ChatModelClient
    client = ChatModelClient(base_url=url, api_key='k', model='m', state_path=tmp_path / 'q.json')
    assert client.configured is allowed


# ---------- без блокировки экземпляра бот сам не публикует ----------

@pytest.fixture
def unguarded(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_instance_lock_handle', None)
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    real_open = type(tmp_path).open

    def deny(self, *args, **kwargs):
        if self.name == '.anime_news_bot.lock':
            raise OSError(30, 'Read-only file system')
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(type(tmp_path), 'open', deny)
    bot._acquire_instance_lock(wait_seconds=0)        # запуск не обрывается
    return bot


def test_lock_failure_stops_autopublication_but_not_the_bot(unguarded, monkeypatch):
    from types import SimpleNamespace as NS
    assert 'Read-only' in bot._instance_unguarded_reason()
    cycle = AsyncMock()
    monkeypatch.setattr(bot, '_check_news_cycle', cycle)
    asyncio.run(bot.check_news(NS(bot=None)))
    cycle.assert_not_awaited()
    send = AsyncMock()
    monkeypatch.setattr(bot, '_publish_one_from_queue_locked', send)
    monkeypatch.setattr(bot, 'settings', NS(auto_enabled=True, publish_mode='channel', thread_mode=False))
    monkeypatch.setattr(bot, 'post_queue', object())
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: True)
    asyncio.run(bot.publisher_tick(NS(bot=None)))
    send.assert_not_awaited()
    monkeypatch.setattr(bot, 'pending_posts', object())
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    assert asyncio.run(bot._autopost_one_from_thread(None)) is None


def test_taken_lock_leaves_publication_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_instance_lock_handle', None)
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    bot._acquire_instance_lock(wait_seconds=0)
    try:
        assert bot._instance_unguarded_reason() == ''
    finally:
        bot._release_instance_lock()


# ---------- бюджет модели модерации на границе суток ----------

@pytest.mark.parametrize('used', [400, 50])
def test_late_answer_does_not_change_the_next_days_budget(tmp_path, monkeypatch, used):
    """Резерв 200 токенов, полночь по UTC, чтение статистики и запоздалый ответ:
    раньше новый день получал разницу без единого запроса."""
    import datetime as dt
    import moderation_llm
    from moderation_llm import ChatModelClient
    client = ChatModelClient(base_url='https://m.example/v1', api_key='k', model='m',
                             state_path=tmp_path / 'q.json')
    day = client._reserve(200)
    assert day

    class Tomorrow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.now(tz) + dt.timedelta(days=1)
    monkeypatch.setattr(moderation_llm, 'datetime', Tomorrow)   # часы перешли полночь
    assert client.snapshot()['tokens'] == 0                      # статистика открыла новый день
    client._reconcile(200, used, day)                            # и тут пришёл ответ
    state = client.snapshot()
    assert (state['requests'], state['tokens']) == (0, 0)


def test_answer_on_the_same_day_is_still_reconciled(tmp_path):
    from moderation_llm import ChatModelClient
    client = ChatModelClient(base_url='https://m.example/v1', api_key='k', model='m',
                             state_path=tmp_path / 'q.json')
    day = client._reserve(200)
    client._reconcile(200, 50, day)
    assert client.snapshot()['tokens'] == 50


# ---------- окружение дочерних процессов ----------

SECRETS = {'BOT_TOKEN': '1:fake', 'TG_ACCOUNT_SESSION': 'fake-session', 'TG_ACCOUNT_API_HASH': 'fakehash',
           'TG_ACCOUNT_API_ID': '12345', 'GROQ_API_KEY': 'fake', 'LLM_API_KEY': 'fake',
           'DENO_AUTH_TOKENS': 'fake@host', 'SOME_NEW_PASSWORDLESS_THING': 'x'}


@pytest.fixture
def secret_env(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy:3128')
    monkeypatch.setenv('LC_ALL', 'C.UTF-8')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')


@pytest.mark.parametrize('network', [False, True])
def test_child_env_keeps_only_what_children_need(secret_env, network):
    from child_env import child_env
    env = child_env(network=network, extra={'OMP_NUM_THREADS': '1'}, keep=('KEEP_ME',))
    assert not set(SECRETS) & set(env)
    assert env['PATH'] == '/usr/bin:/bin' and env['LC_ALL'] == 'C.UTF-8' and env['OMP_NUM_THREADS'] == '1'
    assert ('HTTPS_PROXY' in env) is network                # прокси — только тем, кто ходит в сеть


def test_moderation_worker_gets_no_secrets(secret_env, monkeypatch):
    import moderation_media as media
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs.get('env') or {})
        return type('R', (), {'returncode': 0, 'stdout': '{"status": "checked"}', 'stderr': ''})()
    monkeypatch.setattr(media.subprocess, 'run', fake_run)
    media.run_worker('file', 'image', 5, .8, .85, 2048)
    assert not set(SECRETS) & set(seen) and 'HTTPS_PROXY' not in seen
    assert seen[media.WORKER_MEMORY_ENV] == '2048' and seen['PATH']


def test_video_downloader_gets_no_secrets(secret_env, monkeypatch, tmp_path):
    import video_download
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen.update(kwargs.get('env') or {})
        raise OSError('не запускаем')
    monkeypatch.setattr(video_download.asyncio, 'create_subprocess_exec', fake_exec)
    with pytest.raises(OSError):
        asyncio.run(video_download.download_isolated(
            'https://example.com/v', tmp_path, fmt='b', max_mb=10, max_duration=0,
            extensions=('.mp4',), timeout=5))
    assert seen and not set(SECRETS) & set(seen)
    assert seen['HTTPS_PROXY'] == 'http://proxy:3128'        # сеть загрузчику нужна
