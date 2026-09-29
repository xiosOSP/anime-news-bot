"""Второй полный аудит, надёжность и безопасность: что чинилось и почему."""
import os
import time
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import Conflict, TelegramError

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def clean_alerts(monkeypatch):
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    monkeypatch.setattr(bot, '_auto_disabled_pending', [])


# ------------------------------------------------- конфликт polling (409)

@pytest.mark.asyncio
async def test_polling_conflict_reaches_the_owner_once_an_hour(monkeypatch):
    # PTB не поднимает Conflict из run_polling, а отдаёт его обработчику
    # ошибок. Прежняя ветка «except Conflict» не срабатывала ни разу.
    monkeypatch.setattr(bot, '_polling_conflict_count', 0)
    monkeypatch.setattr(bot, '_polling_conflict_alert_at', 0.0)
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    marked = []
    monkeypatch.setattr(bot, '_mark_polling_conflict', lambda d, n: marked.append((d, n)))
    monkeypatch.setitem(bot._runtime_health, 'last_error', '')
    context = NS(bot=MagicMock(), error=Conflict('terminated by other getUpdates request'))
    for _ in range(3):
        await bot._global_error_handler(None, context)
    assert bot.notify_admin.await_count == 1
    assert 'BOT_TOKEN' in bot.notify_admin.await_args.args[1]
    assert [n for _, n in marked] == [1, 2, 3]
    assert bot._runtime_health['last_error'].startswith('polling conflict')
    bot._polling_conflict_alert_at -= bot.POLLING_CONFLICT_ALERT_EVERY_SEC + 1
    await bot._global_error_handler(None, context)
    assert bot.notify_admin.await_count == 2


@pytest.mark.asyncio
async def test_other_errors_still_go_through_the_generic_path(monkeypatch):
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock())
    await bot._global_error_handler(None, NS(bot=MagicMock(), error=TelegramError('boom')))
    bot.notify_admin.assert_not_awaited()
    assert 'TelegramError' in bot._runtime_health['last_error']


# ------------------------------------------------ предупреждения админам

@pytest.mark.asyncio
async def test_alerts_are_sent_by_the_health_watchdog_not_only_by_the_news_cycle(monkeypatch):
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, '_check_channel_access', AsyncMock(return_value=(True, '')))
    monkeypatch.setattr(bot, 'settings', NS(auto_enabled=False))
    bot._queue_admin_alert('🛡 Файл модерации повреждён')
    bot._auto_disabled_pending.append(('Источник', 'молчит'))
    await bot.health_probe_job(NS(bot=MagicMock(), application=NS(job_queue=None)))
    texts = [call.args[1] for call in bot.notify_admin.await_args_list]
    assert any('Файл модерации повреждён' in t for t in texts)
    assert any('Источник «Источник» выключен автоматически' in t for t in texts)
    assert bot._pending_admin_alerts == [] and bot._auto_disabled_pending == []


@pytest.mark.asyncio
async def test_a_failing_alert_does_not_break_the_watchdog(monkeypatch):
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(side_effect=RuntimeError('нет сети')))
    monkeypatch.setattr(bot, '_check_channel_access', AsyncMock(return_value=(True, '')))
    monkeypatch.setattr(bot, 'settings', NS(auto_enabled=False))
    bot._queue_admin_alert('что-то важное')
    await bot.health_probe_job(NS(bot=MagicMock(), application=NS(job_queue=None)))
    assert bot._runtime_health['telegram_ok'] is True


# -------------------------------------- битые и нечитаемые файлы данных

CORRUPT = '{"urls": ["https://a.test/1", "https:'


def made(kind, path):
    return {'links': bot.SentLinksStore, 'queue': bot.PostQueue, 'settings': bot.BotSettings}[kind](path)


@pytest.mark.parametrize('kind', ['links', 'queue', 'settings'])
def test_corrupt_file_is_set_aside_and_reported(tmp_path, kind):
    path = tmp_path / f'{kind}.json'
    path.write_text(CORRUPT, encoding='utf-8')
    store = made(kind, path)
    copies = list(tmp_path.glob(f'{kind}.json.corrupt-*'))
    assert len(copies) == 1 and copies[0].read_text(encoding='utf-8') == CORRUPT
    assert not path.exists()
    assert any('повреждён' in text and path.name in text for text in bot._pending_admin_alerts)
    assert store is not None


@pytest.mark.parametrize('kind', ['links', 'queue', 'settings', 'moderation'])
def test_unreadable_file_is_never_overwritten(tmp_path, monkeypatch, kind):
    # Файл цел, но прочесть его не вышло (права, сбой диска): затирать его
    # пустым состоянием нельзя — иначе пропадёт история или настройки.
    path = tmp_path / f'{kind}.json'
    original = '{"schema_version": 1}'
    path.write_text(original, encoding='utf-8')
    real_open = type(path).open

    def flaky(self, mode='r', *args, **kwargs):
        if self.name == path.name and mode.startswith('r'):
            raise PermissionError(13, 'Permission denied')
        return real_open(self, mode, *args, **kwargs)
    monkeypatch.setattr(type(path), 'open', flaky)
    real_read = type(path).read_text

    def flaky_read(self, *args, **kwargs):
        if self.name == path.name:
            raise PermissionError(13, 'Permission denied')
        return real_read(self, *args, **kwargs)
    monkeypatch.setattr(type(path), 'read_text', flaky_read)
    store = bot.ChatModerationStore(path) if kind == 'moderation' else made(kind, path)
    assert getattr(store, '_read_failed', False) is True
    if kind == 'settings':
        store.save()
    elif kind == 'moderation':
        assert store._save() is False
    else:
        assert store._save() is False
    with open(str(path), encoding='utf-8') as handle:      # builtin: Path.open подменён выше
        assert handle.read() == original


def test_moderation_store_keeps_an_unreadable_file(tmp_path):
    path = tmp_path / 'moderation.json'
    path.mkdir()
    store = bot.ChatModerationStore(path)
    assert store.load_error and store._save() is False
    assert any('Файл модерации не прочитан' in t for t in bot._pending_admin_alerts)


def test_settings_are_written_while_the_lock_is_held(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    held = []
    real = bot._atomic_write_json

    def spy(path, data, **kwargs):
        held.append(settings._lock._is_owned())
        return real(path, data, **kwargs)
    monkeypatch.setattr(bot, '_atomic_write_json', spy)
    settings.save()
    assert held == [True]


def test_moderation_store_is_written_compactly(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.set_chat(-100, True)
    text = (tmp_path / 'moderation.json').read_text(encoding='utf-8')
    assert '\n' not in text.strip()
    assert bot.ChatModerationStore(tmp_path / 'moderation.json').is_enabled(-100)


def test_old_reviews_are_dropped_but_fresh_ones_stay(tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'moderation.json')
    store.ensure_review('old', -100, 1, 'spam', 'warn', 'модель', 'x', 'текст')
    store.ensure_review('fresh', -100, 1, 'spam', 'warn', 'модель', 'x', 'текст')
    store._data['reviews']['old']['at'] = time.time() - bot.MODERATION_REVIEW_TTL_SEC - 10
    store.ensure_review('new', -100, 1, 'spam', 'warn', 'модель', 'x', 'текст')
    assert store.review('old') == {} and store.review('fresh') and store.review('new')


def test_read_only_data_dir_does_not_crash_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_instance_lock_handle', None)
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    real_open = type(tmp_path).open

    def deny(self, *args, **kwargs):
        if self.name == '.anime_news_bot.lock':
            raise OSError(30, 'Read-only file system')
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(type(tmp_path), 'open', deny)
    bot._acquire_instance_lock(wait_seconds=0)          # без исключения
    assert bot._instance_lock_handle is None


def test_orphan_temp_files_are_swept_but_fresh_ones_are_not(tmp_path):
    (tmp_path / 'models').mkdir()
    old_tmp, fresh_tmp = tmp_path / '.moderation.json.abc.tmp', tmp_path / '.queue.json.xyz.tmp'
    old_part, keep = tmp_path / 'models' / 'model.onnx.part', tmp_path / 'settings.json'
    for path in (old_tmp, fresh_tmp, old_part, keep):
        path.write_text('x')
    past = time.time() - 7200
    for path in (old_tmp, old_part, keep):
        os.utime(path, (past, past))
    assert bot._sweep_orphan_temp_files(tmp_path) == 2
    assert not old_tmp.exists() and not old_part.exists()
    assert fresh_tmp.exists() and keep.exists()


# ------------------------------------------------------- ежедневный бэкап

@pytest.fixture
def backup(monkeypatch):
    state = NS(now=datetime(2026, 9, 24, 9, 0), data=b'zip-bytes',
               settings=MagicMock(daily_backup=True, last_backup_date='', tz_offset=3))
    monkeypatch.setattr(bot, 'settings', state.settings)
    monkeypatch.setattr(bot, '_local_now', lambda: state.now)
    monkeypatch.setattr(bot, '_build_backup_archive', lambda: (state.data, 'backup.zip'))
    monkeypatch.setattr(bot, '_verify_backup_archive', lambda _d: {'ok': True, 'errors': []})
    monkeypatch.setattr(bot, '_backup_restore_selftest', lambda _d: {'ok': True, 'errors': []})
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, '_backup_check_alerted_day', '')
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    state.bot = MagicMock(send_document=AsyncMock())
    state.run = lambda: bot.daily_backup_job(NS(bot=state.bot))
    return state


@pytest.mark.asyncio
async def test_undeliverable_backup_is_reported_once_a_day(backup):
    backup.bot.send_document.side_effect = TelegramError('Forbidden: bot was blocked by the user')
    await backup.run()
    backup.now = datetime(2026, 9, 24, 10, 0)
    await backup.run()
    assert bot.notify_admin.await_count == 1
    text = bot.notify_admin.await_args.args[1]
    assert 'доставку' in text and 'blocked' in text
    assert backup.settings.last_backup_date == ''            # завтра — снова
    assert backup.bot.send_document.await_count == 2


@pytest.mark.asyncio
async def test_backup_over_the_telegram_limit_is_reported_not_attempted(backup, monkeypatch):
    monkeypatch.setattr(bot, 'TELEGRAM_DOCUMENT_LIMIT', 4)
    await backup.run()
    backup.bot.send_document.assert_not_awaited()
    assert 'больше лимита Telegram' in bot.notify_admin.await_args.args[1]


@pytest.mark.asyncio
async def test_delivered_backup_is_silent(backup):
    await backup.run()
    bot.notify_admin.assert_not_awaited()
    assert backup.settings.last_backup_date != ''


# ----------------------------------------------------------- HTML и SSRF

@pytest.mark.asyncio
async def test_logs_filter_word_cannot_break_the_html_header(tmp_path, monkeypatch):
    log = tmp_path / 'bot.log'
    log.write_text('запись <b>жирная</b> в логе\n', encoding='utf-8')
    monkeypatch.setattr(bot, 'LOG_FILE', log)
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    reply = AsyncMock()
    update = NS(message=NS(reply_text=reply), effective_chat=NS(type='private', id=1),
                effective_user=NS(id=1))
    await bot.logs_command.__wrapped__(update, NS(args=['<b>жирная'], bot=MagicMock()))
    text = reply.await_args.args[0]
    assert '«&lt;b&gt;жирная»' in text and '«<b>' not in text.split('<pre>')[0]


@pytest.mark.parametrize('url, expected', [
    ('https://100.100.100.200/latest/meta-data', False),   # CGNAT / Alibaba metadata
    ('http://100.64.0.1/', False),
    ('http://127.0.0.1/', False),
    ('http://169.254.169.254/latest', False),
    ('http://[::ffff:127.0.0.1]/', False),
    ('http://8.8.8.8/', True),
])
def test_url_precheck_uses_the_same_rule_as_the_connection(url, expected):
    assert bot._is_public_http_url(url) is expected
