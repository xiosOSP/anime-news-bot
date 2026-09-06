"""Тесты: /health, автопауза мёртвых источников, дедуп по картинке,
ежедневный бэкап."""
import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import FakeHTTPResponse
from PIL import Image, ImageDraw, ImageFilter

import anime_news_bot
from anime_news_bot import ImageHashes, SourceHealth, _hash_distance, _image_fingerprint


def _frame(shift=0, w=800, h=450):
    """Реалистичный кадр: крупные плавные формы, как у постера/скриншота."""
    im = Image.new('RGB', (w, h), (30, 40, 90))
    d = ImageDraw.Draw(im)
    d.ellipse([w * 0.15 + shift, h * 0.1, w * 0.6 + shift, h * 0.8], fill=(230, 200, 120))
    d.ellipse([w * 0.25 + shift, h * 0.3, w * 0.33 + shift, h * 0.4], fill=(20, 20, 30))
    d.rectangle([0, h * 0.75, w, h], fill=(60, 120, 80))
    return im.filter(ImageFilter.GaussianBlur(1))


def _jpeg(im, quality=88):
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=quality)
    return buf.getvalue()


class TestImageFingerprint:
    def test_same_image_zero_distance(self):
        data = _jpeg(_frame())
        assert _hash_distance(_image_fingerprint(data), _image_fingerprint(data)) == 0

    @pytest.mark.parametrize('size', [(400, 225), (200, 112), (1200, 675)])
    def test_survives_resize(self, size):
        base = _image_fingerprint(_jpeg(_frame()))
        other = _image_fingerprint(_jpeg(_frame().resize(size, Image.LANCZOS)))
        assert _hash_distance(base, other) <= anime_news_bot.IMAGE_HASH_DISTANCE

    def test_survives_recompression(self):
        base = _image_fingerprint(_jpeg(_frame(), quality=92))
        other = _image_fingerprint(_jpeg(_frame(), quality=35))
        assert _hash_distance(base, other) <= anime_news_bot.IMAGE_HASH_DISTANCE

    def test_different_frame_far(self):
        base = _image_fingerprint(_jpeg(_frame()))
        other = _image_fingerprint(_jpeg(_frame(shift=260)))
        assert _hash_distance(base, other) > anime_news_bot.IMAGE_HASH_DISTANCE

    def test_empty_data(self):
        assert _image_fingerprint(b'') is None
        assert _image_fingerprint(None) is None

    def test_md5_fallback_without_pillow(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'Image', None)
        data = _jpeg(_frame())
        fp = _image_fingerprint(data)
        assert fp.startswith('m:')
        assert _hash_distance(fp, _image_fingerprint(data)) == 0
        # пережатая копия md5 уже не ловится — это честное ограничение фолбэка
        assert _hash_distance(fp, _image_fingerprint(_jpeg(_frame(), quality=30))) == 64

    def test_types_not_mixed(self, monkeypatch):
        dhash = _image_fingerprint(_jpeg(_frame()))
        monkeypatch.setattr(anime_news_bot, 'Image', None)
        md5 = _image_fingerprint(_jpeg(_frame()))
        assert _hash_distance(dhash, md5) is None

    def test_broken_bytes_fall_back(self):
        fp = _image_fingerprint(b'not an image at all')
        assert fp is not None and fp.startswith('m:')


class TestImageHashesStore:
    def test_find_and_add(self, tmp_path):
        store = ImageHashes(tmp_path / 'h.json')
        fp = _image_fingerprint(_jpeg(_frame()))
        assert store.find_duplicate(fp) is None
        store.add(fp, 'Аниме объявлено')
        found = store.find_duplicate(_image_fingerprint(
            _jpeg(_frame().resize((400, 225), Image.LANCZOS))))
        assert found and found['t'] == 'Аниме объявлено'

    def test_persists(self, tmp_path):
        p = tmp_path / 'h.json'
        fp = _image_fingerprint(_jpeg(_frame()))
        ImageHashes(p).add(fp, 'T')
        assert ImageHashes(p).find_duplicate(fp) is not None

    def test_capped(self, tmp_path):
        store = ImageHashes(tmp_path / 'h.json', max_items=10)
        for i in range(25):
            store.add(f'd:{i:016x}', f'T{i}')
        assert len(store) == 10

    def test_empty_fingerprint_ignored(self, tmp_path):
        store = ImageHashes(tmp_path / 'h.json')
        store.add('', 'T')
        assert len(store) == 0 and store.find_duplicate('') is None


class TestImageDedupFlow:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(image_dedup=True))
        monkeypatch.setattr(anime_news_bot, 'image_hashes', ImageHashes(tmp_path / 'h.json'))
        resp = FakeHTTPResponse(_jpeg(_frame()), headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: resp)
        return anime_news_bot

    def test_fingerprint_deferred_until_success(self, env):
        news = {'title': 'Новость', 'images': ['u1']}
        assert asyncio.run(env._image_duplicate(news)) is None
        assert '_img_fp' in news
        assert len(env.image_hashes) == 0          # ещё не зафиксирован
        env._commit_image_fingerprint(news)
        assert len(env.image_hashes) == 1 and '_img_fp' not in news

    def test_duplicate_detected_after_commit(self, env):
        first = {'title': 'Первая', 'images': ['u1']}
        asyncio.run(env._image_duplicate(first))
        env._commit_image_fingerprint(first)
        second = {'title': 'Та же с другого сайта', 'images': ['u2']}
        assert asyncio.run(env._image_duplicate(second)) == 'Первая'

    def test_failed_send_does_not_block_others(self, env):
        first = {'title': 'Первая', 'images': ['u1']}
        asyncio.run(env._image_duplicate(first))   # отправка «сорвалась», commit не вызван
        second = {'title': 'Та же с другого сайта', 'images': ['u2']}
        assert asyncio.run(env._image_duplicate(second)) is None

    def test_disabled_setting_skips_check(self, env, monkeypatch):
        monkeypatch.setattr(env, 'settings', MagicMock(image_dedup=False))
        assert asyncio.run(env._image_duplicate({'title': 'X', 'images': ['u']})) is None

    def test_no_images_skips_check(self, env):
        assert asyncio.run(env._image_duplicate({'title': 'X', 'images': []})) is None

    def test_download_failure_is_safe(self, env, monkeypatch):
        monkeypatch.setattr(env, 'http_get_public_with_retry', lambda *a, **k: None)
        assert asyncio.run(env._image_duplicate({'title': 'X', 'images': ['u']})) is None

    def test_commit_without_fingerprint_is_noop(self, env):
        env._commit_image_fingerprint({'title': 'X'})   # не должно падать
        assert len(env.image_hashes) == 0


class TestSourceAutoDisable:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'source_health',
                            SourceHealth(tmp_path / 's.json'))
        anime_news_bot._auto_disabled_pending.clear()
        return anime_news_bot

    def _settings(self, monkeypatch, auto=True):
        disabled = []
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(auto_disable_sources=auto,
                                      is_source_enabled=lambda n: n not in disabled,
                                      toggle_source=lambda n: disabled.append(n)))
        return disabled

    def _silence_for(self, env, name, hours):
        """Сдвигает начало молчания на N часов назад."""
        entry = env.source_health._entry(name)
        entry['silent_since'] = (datetime.now(timezone.utc)
                                 - timedelta(hours=hours)).isoformat()

    def test_not_disabled_before_deadline(self, env, monkeypatch):
        disabled = self._settings(monkeypatch)
        for _ in range(10):
            env._note_source_failure('Dead', '403')
            self._silence_for(env, 'Dead', anime_news_bot.AUTO_DISABLE_AFTER_HOURS - 2)
        assert disabled == []          # молчит меньше суток — ждём

    def test_disabled_after_deadline(self, env, monkeypatch):
        disabled = self._settings(monkeypatch)
        for _ in range(anime_news_bot.AUTO_DISABLE_MIN_CHECKS):
            env._note_source_failure('Dead', '403')
            self._silence_for(env, 'Dead', anime_news_bot.AUTO_DISABLE_AFTER_HOURS + 1)
        env._note_source_failure('Dead', '403')
        assert disabled == ['Dead']
        assert len(env._auto_disabled_pending) == 1

    def test_min_checks_required(self, env, monkeypatch):
        disabled = self._settings(monkeypatch)
        env._note_source_failure('Dead', '403')
        self._silence_for(env, 'Dead', anime_news_bot.AUTO_DISABLE_AFTER_HOURS + 50)
        assert disabled == []          # одной проверки мало, даже если давно молчит

    def test_no_repeat_notifications(self, env, monkeypatch):
        self._settings(monkeypatch)
        for _ in range(10):
            env._note_source_failure('Dead', '403')
            self._silence_for(env, 'Dead', anime_news_bot.AUTO_DISABLE_AFTER_HOURS + 1)
        assert len(env._auto_disabled_pending) == 1

    def test_respects_setting_off(self, env, monkeypatch):
        disabled = self._settings(monkeypatch, auto=False)
        for _ in range(8):
            env._note_source_failure('Dead', '403')
            self._silence_for(env, 'Dead', anime_news_bot.AUTO_DISABLE_AFTER_HOURS + 1)
        assert disabled == []

    def test_success_resets_counter(self, env, monkeypatch):
        self._settings(monkeypatch)
        env._note_source_failure('S', 'err')
        env._note_source_failure('S', 'err')
        env.source_health.record_ok('S', 4)
        assert env.source_health.info('S')['fails'] == 0

    def test_counter_persists(self, tmp_path):
        p = tmp_path / 'sh.json'
        sh = SourceHealth(p)
        sh.record_fail('S', 'e')
        sh.record_fail('S', 'e')
        assert SourceHealth(p).info('S')['fails'] == 2

    def test_manual_reset(self, tmp_path):
        sh = SourceHealth(tmp_path / 'sh.json')
        sh.record_fail('S', 'e')
        sh.reset('S')
        assert sh.info('S')['fails'] == 0

    def test_zero_items_counts_as_failure(self, env, monkeypatch):
        """Источник ответил 200, но пустой — это тоже неудача."""
        disabled = self._settings(monkeypatch)
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('Empty', lambda: [])])
        monkeypatch.setattr(anime_news_bot, 'stats',
                            MagicMock(record_collected=AsyncMock(),
                                      record_skipped=AsyncMock(),
                                      record_source_error=AsyncMock()))
        anime_news_bot.settings.require_image = False
        for _ in range(anime_news_bot.AUTO_DISABLE_MIN_CHECKS + 1):
            asyncio.run(anime_news_bot.collect_all_news())
            entry = anime_news_bot.source_health._entry('Empty')
            entry['silent_since'] = (datetime.now(timezone.utc) - timedelta(
                hours=anime_news_bot.AUTO_DISABLE_AFTER_HOURS + 1)).isoformat()
        assert disabled == ['Empty']


class TestBackup:
    def test_archive_contains_json_files(self, tmp_path, monkeypatch):
        (tmp_path / 'bot_settings.json').write_text('{"a":1}')
        (tmp_path / 'sent_links.json').write_text('{"b":2}')
        (tmp_path / 'ignore.txt').write_text('nope')
        monkeypatch.setattr(anime_news_bot, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(tz_offset=3))
        data, name = anime_news_bot._build_backup_archive()
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert set(names) == {'bot_settings.json', 'sent_links.json'}
        assert name.startswith('anime_bot_backup_') and name.endswith('.zip')

    def test_empty_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DATA_DIR', tmp_path)
        assert anime_news_bot._build_backup_archive() is None

    def _run_job(self, tmp_path, monkeypatch, now, last_date='', enabled=True,
                 admins=(111, 222)):
        (tmp_path / 'bot_settings.json').write_text('{"a":1}')
        monkeypatch.setattr(anime_news_bot, 'DATA_DIR', tmp_path)
        st = MagicMock(daily_backup=enabled, last_backup_date=last_date, tz_offset=3)
        monkeypatch.setattr(anime_news_bot, 'settings', st)
        monkeypatch.setattr(anime_news_bot, '_local_now', lambda: now)
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: set(admins))
        bot = MagicMock()
        bot.send_document = AsyncMock()
        asyncio.run(anime_news_bot.daily_backup_job(MagicMock(bot=bot)))
        return bot, st

    def test_sends_to_all_admins_in_dm(self, tmp_path, monkeypatch):
        bot, st = self._run_job(tmp_path, monkeypatch, datetime(2026, 7, 24, 9, 0))
        assert bot.send_document.await_count == 2
        chats = {c.kwargs['chat_id'] for c in bot.send_document.await_args_list}
        assert chats == {111, 222}                       # в личку, не в канал
        assert st.last_backup_date == '2026-07-24'

    def test_not_twice_a_day(self, tmp_path, monkeypatch):
        bot, _ = self._run_job(tmp_path, monkeypatch, datetime(2026, 7, 24, 14, 0),
                               last_date='2026-07-24')
        assert bot.send_document.await_count == 0

    def test_waits_for_backup_hour(self, tmp_path, monkeypatch):
        bot, _ = self._run_job(tmp_path, monkeypatch, datetime(2026, 7, 24, 2, 0))
        assert bot.send_document.await_count == 0

    def test_disabled_setting(self, tmp_path, monkeypatch):
        bot, _ = self._run_job(tmp_path, monkeypatch, datetime(2026, 7, 24, 9, 0),
                               enabled=False)
        assert bot.send_document.await_count == 0

    def test_send_error_does_not_crash(self, tmp_path, monkeypatch):
        from telegram.error import TelegramError
        (tmp_path / 'bot_settings.json').write_text('{"a":1}')
        monkeypatch.setattr(anime_news_bot, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(daily_backup=True, last_backup_date='', tz_offset=3))
        monkeypatch.setattr(anime_news_bot, '_local_now',
                            lambda: datetime(2026, 7, 24, 9, 0))
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: {111})
        bot = MagicMock()
        bot.send_document = AsyncMock(side_effect=TelegramError('blocked'))
        asyncio.run(anime_news_bot.daily_backup_job(MagicMock(bot=bot)))   # без исключения


class TestHealthCommand:
    def _env(self, tmp_path, monkeypatch, jobs_present=True):
        monkeypatch.setattr(anime_news_bot, 'SOURCES',
                            [('Alive', lambda: []), ('Dying', lambda: []),
                             ('Paused', lambda: [])])
        sh = SourceHealth(tmp_path / 'sh.json')
        sh.record_ok('Alive', 5)
        sh.record_fail('Dying', 'HTTP 403 Forbidden')
        sh.record_fail('Dying', 'HTTP 403 Forbidden')
        monkeypatch.setattr(anime_news_bot, 'source_health', sh)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(is_source_enabled=lambda n: n != 'Paused',
                                      last_backup_date='2026-07-24', tz_offset=3))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts',
                            anime_news_bot.ScheduledPosts(tmp_path / 'sp.json'))
        monkeypatch.setattr(anime_news_bot, 'pending_posts',
                            anime_news_bot.PendingPosts(tmp_path / 'pp.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes',
                            ImageHashes(tmp_path / 'ih.json'))
        monkeypatch.setattr(anime_news_bot, 'sent_links', MagicMock(_set={'a', 'b'}))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.application.job_queue.get_jobs_by_name.side_effect = (
            lambda n: [MagicMock(next_t=datetime.now(timezone.utc))] if jobs_present else [])
        asyncio.run(anime_news_bot.health_command(upd, ctx))
        return upd.message.reply_text.await_args.args[0]

    def test_reports_jobs_and_sources(self, tmp_path, monkeypatch):
        text = self._env(tmp_path, monkeypatch)
        assert 'Автопроверка новостей' in text
        assert 'Публикация отложки' in text
        assert 'Dying: молчит' in text
        assert 'до паузы' in text
        assert 'На паузе: Paused' in text

    def test_warns_when_job_missing(self, tmp_path, monkeypatch):
        text = self._env(tmp_path, monkeypatch, jobs_present=False)
        assert 'НЕ ЗАРЕГИСТРИРОВАН' in text

    def test_reports_system(self, tmp_path, monkeypatch):
        text = self._env(tmp_path, monkeypatch)
        assert 'Память процесса' in text or 'Диск' in text
        assert 'Последний бэкап: 2026-07-24' in text

    def test_healthy_sources_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('Alive', lambda: [])])
        sh = SourceHealth(tmp_path / 'sh2.json')
        sh.record_ok('Alive', 3)
        monkeypatch.setattr(anime_news_bot, 'source_health', sh)
        text = self._env(tmp_path, monkeypatch)
        assert 'отвечают' in text or 'молчит' in text
