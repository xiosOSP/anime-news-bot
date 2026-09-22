"""Журнал модерации не должен останавливать бота на каждом сообщении.

Хранилище модерации пишется целиком — копия, JSON и fsync — прямо в цикле
событий. Заполненное до лимитов, оно весит около 8 МБ и пишется около 300 мс.
Журнал же пишется на КАЖДОЕ решение, включая «не проверено»: при недоступной
модели это каждое сообщение с матом, а на одно предупреждение приходилось
пять полных записей.
"""
import json
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_LOG_FLUSH_SEC', 30)
    clock = [1000.0]
    monkeypatch.setattr(bot.time, 'monotonic', lambda: clock[0])
    s = bot.ChatModerationStore(tmp_path / 'moderation.json')
    s.set_chat(-100, True)          # настоящая запись: отсчёт окна пошёл
    s.clock = clock
    return s


def _log(store, text='текст'):
    store.log_decision(-100, 7, 'User', 'toxic', 'не проверено', 'локальные правила', 'r', text)


def _on_disk(store):
    return json.loads(store.path.read_text(encoding='utf-8'))


class TestTheLogWaits:
    def test_a_log_row_does_not_rewrite_the_whole_store(self, store):
        before = store.path.stat().st_mtime_ns
        _log(store)
        assert store.path.stat().st_mtime_ns == before
        assert not _on_disk(store).get('log'), 'журнал записан сразу'

    def test_stats_counters_wait_too(self, store):
        store.record_decision('toxic', 'warn')
        assert not (_on_disk(store).get('stats') or {}).get('by_category')

    def test_the_log_reaches_disk_after_the_window(self, store):
        _log(store, 'первое')
        store.clock[0] += 31
        _log(store, 'второе')
        texts = [row['text'] for row in _on_disk(store).get('log', [])]
        # Уходят обе строки: отложенная не теряется, а едет со следующей.
        assert texts == ['первое', 'второе']

    def test_zero_means_write_immediately(self, store, monkeypatch):
        monkeypatch.setattr(bot, 'MODERATION_LOG_FLUSH_SEC', 0)
        _log(store)
        assert _on_disk(store).get('log')


class TestNothingIsLost:
    def test_a_real_write_carries_pending_rows(self, store):
        """Предупреждение пишется сразу — и забирает с собой журнал."""
        _log(store, 'ожидало записи')
        store.add_warn(-100, 7, 'toxic')
        data = _on_disk(store)
        assert [row['text'] for row in data.get('log', [])] == ['ожидало записи']
        assert data['users'], 'предупреждение не записано'

    def test_a_warning_is_on_disk_immediately(self, store):
        """Наказание не имеет права ждать: при падении его забыли бы."""
        store.add_warn(-100, 7, 'toxic')
        reloaded = bot.ChatModerationStore(store.path)
        assert reloaded.warn_count(-100, 7) == 1

    def test_flush_writes_what_is_pending(self, store):
        _log(store, 'перед остановкой')
        store.flush()
        assert [row['text'] for row in _on_disk(store).get('log', [])] == ['перед остановкой']

    def test_a_failed_write_keeps_the_rows_pending(self, store, monkeypatch):
        """Диск отказал — строки журнала обязаны дождаться следующей попытки.

        И запись должна честно сообщить об отказе: по её результату команды
        отвечают «❌ Не удалось записать настройку на диск».
        """
        _log(store, 'пережила отказ диска')
        real = bot._atomic_write_json

        def broken(*a, **k):
            raise OSError('диск переполнен')

        monkeypatch.setattr(bot, '_atomic_write_json', broken)
        assert store.set_chat(-200, True) is False
        monkeypatch.setattr(bot, '_atomic_write_json', real)
        store.flush()
        assert [row['text'] for row in _on_disk(store).get('log', [])] == ['пережила отказ диска']

    def test_flush_on_a_clean_store_writes_nothing(self, store):
        before = store.path.stat().st_mtime_ns
        store.flush()
        assert store.path.stat().st_mtime_ns == before

    @pytest.mark.asyncio
    async def test_graceful_shutdown_flushes_the_log(self, store, monkeypatch):
        """Штатная остановка — последний шанс записать хвост журнала."""
        _log(store, 'хвост журнала')
        monkeypatch.setattr(bot, 'chat_moderation', store)
        monkeypatch.setattr(bot, '_mark_lifecycle_exit', lambda *a, **k: None)
        monkeypatch.setattr(bot, '_stop_health_server', lambda: None)
        monkeypatch.setattr(bot, '_release_instance_lock', lambda: None)
        for name in ('user_directory', 'settings', 'experiments', 'post_queue',
                     'scheduled_posts', 'pending_posts', 'sent_links'):
            monkeypatch.setattr(bot, name, None)
        monkeypatch.setattr(bot, 'cleanup_video_dir', lambda **k: None)
        await bot._post_shutdown(MagicMock())
        assert [row['text'] for row in _on_disk(store).get('log', [])] == ['хвост журнала']
