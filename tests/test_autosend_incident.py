"""Регрессии на инцидент «авторассылка включена, но постов нет».

Причина была в одной строке: `_adaptive_hour_stats` звала `_local_tz()`,
которой в модуле нет. Функция вызывалась из `check_news` сразу после сбора
новостей и до отправки, без try. Исключение улетало в APScheduler, задача
оставалась живой, следующий тик падал там же — и так каждый раз, потому что
снимок так и не записывался, а значит оценка считалась просроченной вечно.

Тесты этого не ловили: `_adaptive_hour_stats` выходит раньше, если
`moderation_feedback is None`, а в юнит-тестах глобальные объекты обычно
не подняты. То есть охранное условие пряталo дефект.
"""
import asyncio
import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot

ROOT = Path(__file__).resolve().parent.parent


class TestNoUndefinedNames:
    def test_module_has_no_undefined_names(self):
        """Ровно эта проверка ловила баг мгновенно."""
        result = subprocess.run(
            [sys.executable, '-m', 'ruff', 'check', '--select', 'F821',
             str(ROOT / 'anime_news_bot.py')],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stdout or result.stderr

    def test_adaptive_hour_stats_resolves_timezone(self, monkeypatch):
        """Путь с поднятым moderation_feedback должен исполняться, а не падать."""
        monkeypatch.setattr(bot, 'moderation_feedback',
                            SimpleNamespace(_events=[
                                {'action': 'published', 'at': '2026-08-08T10:00:00+00:00'},
                                {'action': 'hidden', 'at': '2026-08-08T11:00:00+00:00'},
                            ]))
        stats = bot._adaptive_hour_stats()
        assert len(stats) == 24
        assert sum(row['samples'] for row in stats.values()) == 2


class TestAdvisoryFailureDoesNotStopPublishing:
    """Советующая подсистема не имеет права останавливать публикацию."""

    @pytest.fixture
    def ctx(self):
        return SimpleNamespace(bot=MagicMock(), application=SimpleNamespace(job_queue=None))

    async def test_cycle_survives_adaptive_error(self, ctx, monkeypatch):
        monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=True))

        def boom(*a, **k):
            raise RuntimeError('adaptive сломался')

        monkeypatch.setattr(bot, '_evaluate_adaptive_publishing', boom)
        collected = {'called': False}

        async def fake_collect():
            collected['called'] = True
            return [], ['Test: 0'], []

        monkeypatch.setattr(bot, 'collect_all_news', fake_collect)
        monkeypatch.setattr(bot, 'settings', MagicMock(thread_mode=False, quiet_mode=True))
        monkeypatch.setattr(bot, 'post_queue', MagicMock(
            peek_size=AsyncMock(return_value=0), push_many=AsyncMock(return_value=0),
            pop_next=AsyncMock(return_value=None)))
        monkeypatch.setattr(bot, 'cleanup_video_dir', lambda: None)
        monkeypatch.setattr(bot, '_maybe_send_daily_summary', AsyncMock())

        await bot.check_news(ctx)
        # Цикл дошёл до конца, а не оборвался на советующем куске.
        assert collected['called']
        assert 'error' not in str(bot._runtime_health.get('last_check_result', ''))


class TestFailedCycleIsVisible:
    """Упавший цикл должен быть виден, а не молчать."""

    @pytest.fixture
    def ctx(self):
        return SimpleNamespace(bot=MagicMock(), application=SimpleNamespace(job_queue=None))

    async def test_failure_lands_in_runtime_health(self, ctx, monkeypatch):
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(bot, 'notify_admin', notify)
        monkeypatch.setattr(bot, '_check_news_cycle',
                            AsyncMock(side_effect=RuntimeError('что-то сломалось')))
        bot._check_failure_streak = 0

        await bot.check_news(ctx)

        assert 'RuntimeError' in bot._runtime_health['last_check_result']
        assert bot._runtime_health['last_check_result'].startswith('error')
        assert notify.await_count == 1, 'админ должен узнать о первом падении'

    async def test_admin_is_not_spammed_every_cycle(self, ctx, monkeypatch):
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(bot, 'notify_admin', notify)
        monkeypatch.setattr(bot, '_check_news_cycle',
                            AsyncMock(side_effect=RuntimeError('сломалось')))
        bot._check_failure_streak = 0

        for _ in range(4):
            await bot.check_news(ctx)
        assert notify.await_count == 1, 'сообщение только на первое падение серии'

    async def test_streak_resets_after_success(self, ctx, monkeypatch):
        monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=True))
        monkeypatch.setattr(bot, '_check_news_cycle',
                            AsyncMock(side_effect=RuntimeError('сломалось')))
        bot._check_failure_streak = 0
        await bot.check_news(ctx)
        assert bot._check_failure_streak == 1

        monkeypatch.setattr(bot, '_check_news_cycle', AsyncMock(return_value=None))
        await bot.check_news(ctx)
        assert bot._check_failure_streak == 0


class TestAutoJobSurvivesRestart:
    def test_intent_is_persisted(self, tmp_path):
        path = tmp_path / 'settings.json'
        settings = bot.BotSettings(path)
        assert settings.auto_enabled is False
        settings.auto_enabled = True
        assert bot.BotSettings(path).auto_enabled is True

    def test_ensure_job_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(check_interval_sec=1800, check_interval_min=30))
        queue = MagicMock()
        queue.get_jobs_by_name.return_value = []
        assert bot._ensure_auto_news_job(queue) is True
        queue.get_jobs_by_name.return_value = [MagicMock()]
        assert bot._ensure_auto_news_job(queue) is False
        assert queue.run_repeating.call_count == 1

    def test_duplicate_jobs_are_cleaned_up(self):
        queue = MagicMock()
        first, second = MagicMock(), MagicMock()
        queue.get_jobs_by_name.return_value = [first, second]
        bot._ensure_auto_news_job(queue)
        second.schedule_removal.assert_called_once()
        first.schedule_removal.assert_not_called()


class TestPollingExitIsVisible:
    """Молчаливый выход из polling выглядел как перезапуск без причины.

    Telegram отдаёт 409 Conflict, если getUpdates по одному токену делают двое.
    PTB на этом останавливает polling, main() возвращается, процесс выходит с
    кодом 0 — и платформа его перезапускает. В логе при этом ни строчки.
    """

    def test_transient_conflict_recovers_without_dying(self, monkeypatch):
        """Старый контейнер дожил свои секунды — бот должен подняться сам."""
        from telegram.error import Conflict
        monkeypatch.setattr(bot, '_mark_lifecycle_exit', lambda *a, **k: None)
        monkeypatch.setattr(bot.time, 'sleep', lambda _s: None)
        monkeypatch.setattr(bot, 'POLLING_CONFLICT_RETRIES', -1)
        app = MagicMock()
        app.run_polling.side_effect = [Conflict('busy'), Conflict('busy'), None]

        bot._run_polling_guarded(app)      # без SystemExit

        assert app.run_polling.call_count == 3

    def test_persistent_conflict_gives_up_with_a_clear_message(self, monkeypatch):
        from telegram.error import Conflict
        marks = []
        monkeypatch.setattr(bot, '_mark_lifecycle_exit',
                            lambda kind, detail='': marks.append((kind, detail)))
        monkeypatch.setattr(bot.time, 'sleep', lambda _s: None)
        monkeypatch.setattr(bot, 'POLLING_CONFLICT_RETRIES', 2)
        app = MagicMock()
        app.run_polling.side_effect = Conflict('terminated by other getUpdates request')

        with pytest.raises(SystemExit) as exc:
            bot._run_polling_guarded(app)

        assert 'BOT_TOKEN' in str(exc.value)
        assert app.run_polling.call_count == 3      # 2 повтора + первая попытка
        assert marks and marks[0][0] == 'polling_conflict'

    def test_clean_return_is_recorded_as_anomaly(self, monkeypatch):
        marks = []
        monkeypatch.setattr(bot, '_mark_lifecycle_exit',
                            lambda kind, detail='': marks.append((kind, detail)))
        app = MagicMock()
        app.run_polling.return_value = None

        bot._run_polling_guarded(app)

        assert marks and marks[0][0] == 'polling_returned'


class TestRestartLoopNote:
    """Диагноз петли перезапусков должен доезжать до админа, а не только в лог."""

    @staticmethod
    def _lifecycle(monkeypatch, tmp_path, starts_sec, kind='', detail='', unclean=0):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        path = tmp_path / 'runtime_lifecycle.json'
        path.write_text(json.dumps({
            'schema_version': 1, 'state': 'running',
            'starts': [(now - timedelta(seconds=s)).isoformat()
                       for s in sorted(starts_sec, reverse=True)],
            'last_exit_kind': kind, 'last_exit_detail': detail,
            'consecutive_unclean': unclean,
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)

    def test_silent_when_restarts_are_rare(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, [86400, 43200, 0])
        assert bot._restart_loop_note() == ''

    def test_silent_on_a_single_start(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, [0])
        assert bot._restart_loop_note() == ''

    def test_names_the_token_conflict(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, [180, 120, 60, 0],
                        kind='polling_conflict', detail='Conflict: terminated', unclean=3)
        note = bot._restart_loop_note()
        assert 'Частые перезапуски' in note
        assert 'BOT_TOKEN' in note
        assert 'Грязных завершений подряд: 3' in note

    def test_points_at_the_platform_when_nothing_was_recorded(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, [180, 120, 60, 0], kind='', unclean=3)
        note = bot._restart_loop_note()
        assert 'убит снаружи' in note
        assert '137' in note and '143' in note

    def test_survives_a_broken_lifecycle_file(self, monkeypatch, tmp_path):
        path = tmp_path / 'runtime_lifecycle.json'
        path.write_text('{не json', encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
        assert bot._restart_loop_note() == ''


    def test_waits_forever_by_default_instead_of_dying(self, monkeypatch):
        """Лежачий бот хуже мелькающего: по умолчанию сдаваться нельзя."""
        from telegram.error import Conflict
        monkeypatch.setattr(bot, '_mark_lifecycle_exit', lambda *a, **k: None)
        waits = []
        monkeypatch.setattr(bot.time, 'sleep', waits.append)
        monkeypatch.setattr(bot, 'POLLING_CONFLICT_RETRIES', -1)
        app = MagicMock()
        app.run_polling.side_effect = [Conflict('busy')] * 30 + [None]

        bot._run_polling_guarded(app)      # без SystemExit

        assert app.run_polling.call_count == 31
        assert max(waits) <= bot.POLLING_CONFLICT_BACKOFF_MAX_SEC, 'пауза должна упираться в потолок'

    def test_default_is_unlimited_retries(self):
        assert bot.POLLING_CONFLICT_RETRIES < 0


class TestSecretsNeverLeakToAdmin:
    """Токен утёк в личку через диагностику перезапусков.

    PTB вставляет токен прямо в текст ошибки: «The token `123:ABC...` was
    rejected by the server». Этот текст попадал в runtime_lifecycle.json,
    а оттуда — в сообщение о запуске, которое бот шлёт админу.
    """

    def test_bot_token_is_masked(self, monkeypatch):
        monkeypatch.setattr(bot, 'TOKEN', '8773585577:AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej')
        text = ('InvalidToken: The token `8773585577:AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej` '
                'was rejected by the server.')
        out = bot._redact_secrets(text)
        assert '8773585577:AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej' not in out
        assert 'скрыто' in out

    def test_any_token_shaped_string_is_masked(self):
        out = bot._redact_secrets('сбой на 1234567890:AAHqwertyuiopASDFGHJKLzxcvbnm123456 тут')
        assert 'AAHqwertyuiopASDFGHJKLzxcvbnm123456' not in out
        assert '1234567890' in out, 'числовая часть не секрет, она помогает опознать бота'

    def test_ordinary_text_is_untouched(self):
        text = 'Канал -1003040322753, источников 12, память 111 МБ'
        assert bot._redact_secrets(text) == text

    def test_llm_key_is_masked(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-verysecretvalue12345')
        out = bot._redact_secrets('LLM error: key sk-verysecretvalue12345 rejected')
        assert 'sk-verysecretvalue12345' not in out

    def test_lifecycle_never_stores_the_raw_token(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', tmp_path / 'lifecycle.json')
        monkeypatch.setattr(bot, 'TOKEN', '8773585577:AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej')
        bot._mark_lifecycle_exit(
            'unhandled_exception',
            'InvalidToken: The token `8773585577:AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej` was rejected')
        on_disk = (tmp_path / 'lifecycle.json').read_text(encoding='utf-8')
        assert 'AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej' not in on_disk

    def test_startup_note_masks_an_already_saved_token(self, monkeypatch, tmp_path):
        """Даже если секрет уже лежит на диске со старой версии."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        path = tmp_path / 'lifecycle.json'
        path.write_text(json.dumps({
            'starts': [(now - timedelta(seconds=s)).isoformat() for s in (180, 120, 60, 0)],
            'last_exit_kind': 'unhandled_exception',
            'last_exit_detail': 'InvalidToken: The token `8773585577:AAEgIxM0NqPeJlrX'
                                'lYn9ZX4NzuM4ej` was rejected by the server.',
            'consecutive_unclean': 2,
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
        note = bot._restart_loop_note()
        assert 'AAEgIxM0NqPeJlrXlYn9ZX4NzuM4ej' not in note
        assert 'InvalidToken' in note, 'тип ошибки остаётся, скрывается только секрет'


class TestStartupReportDoesNotSpam:
    """25 отчётов за два часа — это не диагностика, а мусор в личке."""

    @staticmethod
    def _starts(monkeypatch, tmp_path, seconds_ago):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        path = tmp_path / 'lifecycle.json'
        path.write_text(json.dumps({
            'starts': [(now - timedelta(seconds=s)).isoformat()
                       for s in sorted(seconds_ago, reverse=True)],
            'last_exit_kind': 'unhandled_exception',
            'last_exit_detail': 'InvalidToken', 'consecutive_unclean': 3,
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)

    def test_first_start_gets_the_full_report(self, monkeypatch, tmp_path):
        self._starts(monkeypatch, tmp_path, [0])
        assert bot._startup_reports_are_spamming() is False

    def test_occasional_restarts_stay_verbose(self, monkeypatch, tmp_path):
        self._starts(monkeypatch, tmp_path, [3600, 0])
        assert bot._startup_reports_are_spamming() is False

    def test_restart_loop_switches_to_brief(self, monkeypatch, tmp_path):
        self._starts(monkeypatch, tmp_path, [180, 150, 120, 90, 60, 0])
        assert bot._startup_reports_are_spamming() is True

    def test_old_storm_does_not_keep_muting_reports(self, monkeypatch, tmp_path):
        """Утихло — значит полный отчёт снова полезен."""
        self._starts(monkeypatch, tmp_path, [86400, 80000, 70000, 0])
        assert bot._startup_reports_are_spamming() is False

    async def test_brief_report_is_short_and_names_the_reason(self, monkeypatch, tmp_path):
        self._starts(monkeypatch, tmp_path, [240, 180, 120, 60, 0])
        monkeypatch.setattr(bot, 'settings', MagicMock(startup_report=True))
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: {1})
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        await bot.send_startup_report(app, brief=True)

        text = app.bot.send_message.await_args.kwargs['text']
        assert 'перезапустился снова' in text
        assert 'unhandled_exception' in text, 'причина должна остаться'
        assert len(text) < 700, 'краткий отчёт не должен быть простынёй'
        assert 'Источников:' not in text and 'Хранилища' not in text

    async def test_brief_report_masks_secrets(self, monkeypatch, tmp_path):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        path = tmp_path / 'lifecycle.json'
        path.write_text(json.dumps({
            'starts': [(now - timedelta(seconds=s)).isoformat() for s in (180, 120, 60, 0)],
            'last_exit_kind': 'unhandled_exception',
            'last_exit_detail': 'InvalidToken: The token '
                                '`8773585577:AAE6wNielZKdBNL7hkEJsIK` was rejected',
            'consecutive_unclean': 3,
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
        monkeypatch.setattr(bot, 'settings', MagicMock(startup_report=True))
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: {1})
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        await bot.send_startup_report(app, brief=True)

        text = app.bot.send_message.await_args.kwargs['text']
        assert 'AAE6wNielZKdBNL7hkEJsIK' not in text
        assert 'InvalidToken' in text


class TestGracefulShutdownIsExplained:
    """Signal shutdown must not be overwritten by ``polling_returned``."""

    async def test_post_shutdown_marks_the_signal(self, monkeypatch):
        bot._runtime_health.pop('graceful_shutdown_at', None)
        marks = []
        monkeypatch.setattr(bot, '_mark_lifecycle_exit',
                            lambda kind, detail='': marks.append((kind, detail)))
        monkeypatch.setattr(bot, '_stop_health_server', lambda: None)
        monkeypatch.setattr(bot, '_release_instance_lock', lambda: None)
        monkeypatch.setattr(bot, 'user_directory', None)
        monkeypatch.setattr(bot, 'settings', None)
        monkeypatch.setattr(bot, 'experiments', None)
        monkeypatch.setattr(bot, 'post_queue', None)
        monkeypatch.setattr(bot, 'scheduled_posts', None)
        monkeypatch.setattr(bot, 'pending_posts', None)
        monkeypatch.setattr(bot, 'sent_links', None)
        monkeypatch.setattr(bot, 'cleanup_video_dir', lambda **k: None)

        await bot._post_shutdown(MagicMock())

        assert bot._runtime_health.get('graceful_shutdown_at')
        assert marks and marks[-1][0] == 'external_signal'
        assert 'Внешняя остановка' in marks[-1][1]

    def test_polling_return_does_not_overwrite_external_signal(self, monkeypatch):
        marks = []
        monkeypatch.setattr(bot, '_mark_lifecycle_exit',
                            lambda kind, detail='': marks.append((kind, detail)))
        monkeypatch.setattr(bot, '_read_lifecycle', lambda: {
            'state': 'stopped', 'pid': bot.os.getpid(),
            'last_exit_kind': 'external_signal',
        })
        app = MagicMock()
        app.run_polling.return_value = None

        bot._run_polling_guarded(app)

        assert marks == [], 'external_signal must remain the final lifecycle reason'

    def test_plain_polling_return_is_still_reported(self, monkeypatch):
        marks = []
        monkeypatch.setattr(bot, '_mark_lifecycle_exit',
                            lambda kind, detail='': marks.append((kind, detail)))
        monkeypatch.setattr(bot, '_read_lifecycle', lambda: {'state': 'running'})
        app = MagicMock()
        app.run_polling.return_value = None

        bot._run_polling_guarded(app)

        assert marks and marks[-1][0] == 'polling_returned'
        assert 'без post_shutdown' in marks[-1][1]


class TestAutoNewsCadenceSurvivesRestarts:
    def test_interrupted_cycle_is_not_replayed_five_seconds_after_restart(self, monkeypatch):
        """Исходный смысл теста сохранён: мгновенного повтора быть не должно.

        Но и полный интервал оказался неверным: при частых перезапусках цикл
        тогда не запускался вовсе. Ждём короткую паузу — не 5 секунд и не 30
        минут.
        """
        from datetime import datetime, timedelta, timezone
        started = datetime.now(timezone.utc) - timedelta(seconds=60)
        monkeypatch.setattr(bot, 'settings', MagicMock(check_interval_sec=1800))
        monkeypatch.setattr(bot, '_read_lifecycle', lambda: {
            'last_auto_cycle_started_at': started.isoformat(),
            'last_auto_cycle_state': 'running',
            'auto_cycle_interrupted_streak': 1,
        })

        delay = bot._auto_restore_first_delay()

        assert delay > 30, 'мгновенный повтор вернул бы шторм циклов'
        assert delay < 600, 'полный интервал означал бы, что новости не собираются'

    def test_cycle_due_after_full_interval_keeps_short_boot_delay(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        started = datetime.now(timezone.utc) - timedelta(seconds=1900)
        monkeypatch.setattr(bot, 'settings', MagicMock(check_interval_sec=1800))
        monkeypatch.setattr(bot, '_read_lifecycle', lambda: {
            'last_auto_cycle_started_at': started.isoformat(),
            'last_auto_cycle_state': 'ok',
        })

        assert bot._auto_restore_first_delay() == 5.0


class TestNetworkTranslationOffLoop:
    """format_news_short синхронная, но умеет уходить в сеть за переводом.

    Из корутин её звали напрямую, в том числе из `_prepare_news_for_send` —
    то есть внутри конвейера публикации. Медленный DeepL замораживал event
    loop на всё время таймаута и ретраев (замерено 6 секунд на один пост).
    """

    def test_no_direct_calls_from_coroutines(self):
        src = (ROOT / 'anime_news_bot.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        lines = src.splitlines()
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'format_news_short':
                enclosing = [f for f in funcs
                             if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
                if not enclosing:
                    continue
                owner = max(enclosing, key=lambda f: f.lineno)
                line = lines[node.lineno - 1]
                prev = lines[node.lineno - 2] if node.lineno >= 2 else ''
                threaded = 'to_thread' in line or 'to_thread' in prev
                if isinstance(owner, ast.AsyncFunctionDef) and not threaded:
                    offenders.append((node.lineno, owner.name))
        assert not offenders, f'перевод по сети внутри корутины: {offenders}'

    async def test_slow_translation_does_not_freeze_the_loop(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(bot, 'translate_text',
                            lambda text, *a, **k: (_time.sleep(0.4), text)[1])
        news = {'title': 'Naruto season', 'summary': 'english text.',
                'link': 'https://example.test/1', 'source': 'S', 'images': ['i']}
        gaps = []

        async def heartbeat(stop):
            last = _time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = _time.perf_counter()
                gaps.append(now - last - 0.005)
                last = now

        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat(stop))
        await asyncio.sleep(0.03)
        gaps.clear()
        await asyncio.to_thread(bot.format_news_short, news)
        stop.set()
        await task
        assert max(gaps) < 0.2, f'event loop замер на {max(gaps):.2f} с'


class TestSettingsAreThreadSafe:
    """Настройки читают и пишут несколько потоков одновременно."""

    def test_settings_have_a_lock(self, tmp_path):
        settings = bot.BotSettings(tmp_path / 'settings.json')
        assert hasattr(settings, '_lock')

    def test_concurrent_writes_do_not_corrupt_the_file(self, tmp_path):
        import threading as _threading
        path = tmp_path / 'settings.json'
        settings = bot.BotSettings(path)
        errors = []

        def hammer(offset):
            try:
                for i in range(40):
                    settings.update(check_interval_min=5 + (offset + i) % 50)
            except Exception as exc:          # noqa: BLE001 - тест ловит любую поломку
                errors.append(exc)

        threads = [_threading.Thread(target=hammer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        # Файл обязан остаться разбираемым, а не оборваться на полуслове.
        data = json.loads(path.read_text(encoding='utf-8'))
        assert 'check_interval_min' in data
        assert bot.BotSettings(path).check_interval_min >= 5

    def test_update_writes_related_values_at_once(self, tmp_path):
        path = tmp_path / 'settings.json'
        settings = bot.BotSettings(path)
        writes = []
        original = bot._atomic_write_json
        bot._atomic_write_json = lambda p, d, **kw: (writes.append(1), original(p, d, **kw))[1]
        try:
            settings.update(check_interval_min=15, quiet_mode=True)
        finally:
            bot._atomic_write_json = original
        assert len(writes) == 1, 'связанные значения должны писаться одной записью'
        reloaded = bot.BotSettings(path)
        assert reloaded.check_interval_min == 15


class TestCycleResourceFootprint:
    """Перезапуск случается во время цикла — надо знать, чего цикл стоит."""

    def test_cycle_records_memory_growth(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', tmp_path / 'lifecycle.json')
        bot._mark_auto_cycle_started()
        bot._mark_auto_cycle_finished('ok')
        data = json.loads((tmp_path / 'lifecycle.json').read_text(encoding='utf-8'))
        assert 'last_auto_cycle_rss_start_mb' in data
        assert 'last_auto_cycle_rss_end_mb' in data
        assert 'peak_rss_mb' in data

    def test_peak_only_grows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', tmp_path / 'lifecycle.json')
        bot._mark_auto_cycle_started()
        bot._mark_auto_cycle_finished('ok')
        first = json.loads((tmp_path / 'lifecycle.json').read_text(encoding='utf-8'))['peak_rss_mb']
        bot._mark_auto_cycle_started()
        bot._mark_auto_cycle_finished('ok')
        second = json.loads((tmp_path / 'lifecycle.json').read_text(encoding='utf-8'))['peak_rss_mb']
        assert second >= first


class TestInterruptedCycleRetriesSooner:
    """Прерванный цикл не должен ждать полный интервал.

    Найдено по логам продакшена: процесс живёт меньше минуты, цикл стартует и
    обрывается, а следующая попытка откладывалась на те же 30 минут — как у
    успешно завершённого. При частых перезапусках это значит, что новости не
    собираются вовсе: каждая попытка записывает новое время старта и снова не
    доживает.
    """

    @staticmethod
    def _lifecycle(monkeypatch, tmp_path, *, started_ago, state, streak=0):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        path = tmp_path / 'lifecycle.json'
        path.write_text(json.dumps({
            'last_auto_cycle_started_at': (now - timedelta(seconds=started_ago)).isoformat(),
            'last_auto_cycle_state': state,
            'auto_cycle_interrupted_streak': streak,
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
        monkeypatch.setattr(bot, 'settings', MagicMock(check_interval_sec=1800))

    def test_finished_cycle_keeps_the_full_interval(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, started_ago=60, state='finished')
        assert bot._auto_restore_first_delay() > 1000

    def test_interrupted_cycle_retries_within_minutes(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, started_ago=60, state='running', streak=1)
        delay = bot._auto_restore_first_delay()
        assert delay <= bot.AUTO_CYCLE_RETRY_BASE_SEC + 1, f'ждём {delay:.0f} с вместо короткой паузы'
        assert delay >= 5, 'мгновенный повтор вернул бы шторм циклов'

    def test_backoff_grows_with_repeated_interruptions(self, monkeypatch, tmp_path):
        delays = []
        for streak in (1, 2, 4):
            self._lifecycle(monkeypatch, tmp_path, started_ago=60,
                            state='running', streak=streak)
            delays.append(bot._auto_restore_first_delay())
        assert delays == sorted(delays) and delays[0] < delays[-1]

    def test_backoff_is_capped(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, started_ago=60, state='running', streak=999)
        assert bot._auto_restore_first_delay() <= bot.AUTO_CYCLE_RETRY_MAX_SEC + 1

    def test_backoff_never_exceeds_the_normal_interval(self, monkeypatch, tmp_path):
        """Ждать дольше обычной проверки бессмысленно."""
        self._lifecycle(monkeypatch, tmp_path, started_ago=1750, state='running', streak=999)
        assert bot._auto_restore_first_delay() <= 60

    def test_streak_grows_when_a_cycle_is_interrupted(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, started_ago=60, state='running', streak=2)
        bot._mark_auto_cycle_started()
        data = json.loads((tmp_path / 'lifecycle.json').read_text(encoding='utf-8'))
        assert data['auto_cycle_interrupted_streak'] == 3

    def test_streak_resets_after_a_finished_cycle(self, monkeypatch, tmp_path):
        self._lifecycle(monkeypatch, tmp_path, started_ago=60, state='running', streak=5)
        bot._mark_auto_cycle_finished('ok')
        data = json.loads((tmp_path / 'lifecycle.json').read_text(encoding='utf-8'))
        assert data['auto_cycle_interrupted_streak'] == 0
