"""Тесты самодиагностики: сторож тишины, стартовый отчёт, квота DeepL,
качество превью-кадра «ленивого» видео."""
import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image, ImageDraw

import anime_news_bot


def _pic(w, h):
    im = Image.new('RGB', (w, h), (30, 40, 90))
    ImageDraw.Draw(im).ellipse([w * 0.2, h * 0.2, w * 0.8, h * 0.8], fill=(230, 200, 120))
    buf = io.BytesIO()
    im.save(buf, 'JPEG')
    return buf.getvalue()


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class TestSilenceWatchdog:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('A', None), ('B', None)])
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts', None)
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(last_publish_at='', is_source_enabled=lambda n: True))
        anime_news_bot._silence_reported = False

    def test_first_run_only_marks(self):
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
        assert na.await_count == 0

    def test_quiet_period_no_alarm(self):
        anime_news_bot.settings.last_publish_at = _ago(3)
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
        assert na.await_count == 0

    def test_alarm_after_threshold(self):
        anime_news_bot.settings.last_publish_at = _ago(anime_news_bot.WATCHDOG_SILENCE_HOURS + 2)
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
            assert na.await_count == 1
            assert 'без единой публикации' in na.await_args.args[1]
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
            assert na.await_count == 1          # не спамит

    def test_publication_rearms_watchdog(self):
        anime_news_bot.settings.last_publish_at = _ago(20)
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
            anime_news_bot._mark_published()
            assert anime_news_bot._silence_reported is False
            anime_news_bot.settings.last_publish_at = _ago(20)
            asyncio.run(anime_news_bot._check_silence(MagicMock()))
            assert na.await_count == 2

    def test_garbage_in_settings_is_safe(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(last_publish_at=12345))     # не строка
        assert anime_news_bot._silence_hours() is None

    def test_mark_published_writes_iso(self, tmp_path, monkeypatch):
        st = anime_news_bot.BotSettings(tmp_path / 's.json')
        monkeypatch.setattr(anime_news_bot, 'settings', st)
        anime_news_bot._mark_published()
        assert datetime.fromisoformat(st.last_publish_at)


class TestDeepLQuota:
    @pytest.fixture
    def st(self, tmp_path, monkeypatch):
        settings = anime_news_bot.BotSettings(tmp_path / 's.json')
        monkeypatch.setattr(anime_news_bot, 'settings', settings)
        anime_news_bot._pending_admin_alerts.clear()
        return settings

    def test_counts_characters(self, st):
        anime_news_bot._count_deepl_chars('x' * 1000)
        anime_news_bot._count_deepl_chars('y' * 500)
        assert st.deepl_chars == 1500

    def test_warns_at_thresholds_once(self, st):
        limit = anime_news_bot.DEEPL_MONTHLY_LIMIT
        anime_news_bot._count_deepl_chars('x' * int(limit * 0.7))
        assert anime_news_bot._pending_admin_alerts == []
        anime_news_bot._count_deepl_chars('x' * int(limit * 0.15))    # 85%
        assert len(anime_news_bot._pending_admin_alerts) == 1
        anime_news_bot._count_deepl_chars('x' * int(limit * 0.02))    # 87% — без нового
        assert len(anime_news_bot._pending_admin_alerts) == 1
        anime_news_bot._count_deepl_chars('x' * int(limit * 0.1))     # 97%
        assert len(anime_news_bot._pending_admin_alerts) == 2

    def test_new_month_resets(self, st):
        anime_news_bot._count_deepl_chars('x' * 100)
        st.deepl_month = '2020-01'
        anime_news_bot._count_deepl_chars('abc')
        assert st.deepl_chars == 3

    def test_local_usage_survives_restart(self, tmp_path, monkeypatch):
        p = tmp_path / 's.json'
        monkeypatch.setattr(anime_news_bot, 'settings', anime_news_bot.BotSettings(p))
        anime_news_bot._count_deepl_chars('x' * 4242)
        monkeypatch.setattr(anime_news_bot, 'settings', anime_news_bot.BotSettings(p))
        used, _month = anime_news_bot._deepl_usage_local()
        assert used == 4242

    def test_usage_local_tolerates_garbage(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(deepl_month='2026-07', deepl_chars='ой'))
        used, _ = anime_news_bot._deepl_usage_local()
        assert used == 0

    def test_remote_usage_without_key(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        assert anime_news_bot._deepl_usage_remote() is None

    def test_remote_usage_parses(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        monkeypatch.setattr(anime_news_bot.requests, 'get',
                            lambda *a, **k: MagicMock(
                                status_code=200,
                                json=lambda: {'character_count': 5, 'character_limit': 500000}))
        assert anime_news_bot._deepl_usage_remote() == (5, 500000)

    def test_remote_usage_network_error(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')

        def boom(*a, **k):
            raise OSError('down')
        monkeypatch.setattr(anime_news_bot.requests, 'get', boom)
        assert anime_news_bot._deepl_usage_remote() is None

    def test_alert_queue_dedupes(self):
        anime_news_bot._pending_admin_alerts.clear()
        anime_news_bot._queue_admin_alert('одно и то же')
        anime_news_bot._queue_admin_alert('одно и то же')
        assert len(anime_news_bot._pending_admin_alerts) == 1


class TestThumbImprovement:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        anime_news_bot._image_bytes_cache.clear()
        yield
        anime_news_bot._image_bytes_cache.clear()

    def test_replaces_blurry_thumb(self):
        news = {'title': 'Тизер', 'link': 'https://t.me/c/1',
                'images': ['https://cdn/thumb.jpg'], '_thumb_only': True}
        small, big = _pic(320, 180), _pic(1280, 720)
        with patch.object(anime_news_bot, '_cached_image_bytes',
                          side_effect=lambda u: big if 'og' in u else small), \
             patch.object(anime_news_bot, 'fetch_og_image', return_value='https://cdn/og.jpg'), \
             patch.object(anime_news_bot, '_normalize_image_url', side_effect=lambda u, l: u):
            asyncio.run(anime_news_bot._improve_thumb(news))
        assert news['images'][0] == 'https://cdn/og.jpg'
        assert '_thumb_only' not in news

    def test_keeps_thumb_when_no_better(self):
        news = {'title': 'Т', 'link': 'https://t.me/c/2',
                'images': ['https://cdn/thumb.jpg'], '_thumb_only': True}
        with patch.object(anime_news_bot, '_cached_image_bytes', return_value=_pic(320, 180)), \
             patch.object(anime_news_bot, 'fetch_og_image', return_value=None):
            asyncio.run(anime_news_bot._improve_thumb(news))
        assert news['images'] == ['https://cdn/thumb.jpg']     # пост не теряется

    def test_keeps_thumb_when_candidate_smaller(self):
        news = {'title': 'Т', 'link': 'https://t.me/c/5',
                'images': ['https://cdn/thumb.jpg'], '_thumb_only': True}
        small, tiny = _pic(400, 220), _pic(120, 80)
        with patch.object(anime_news_bot, '_cached_image_bytes',
                          side_effect=lambda u: tiny if 'og' in u else small), \
             patch.object(anime_news_bot, 'fetch_og_image', return_value='https://cdn/og.jpg'), \
             patch.object(anime_news_bot, '_normalize_image_url', side_effect=lambda u, l: u):
            asyncio.run(anime_news_bot._improve_thumb(news))
        assert news['images'] == ['https://cdn/thumb.jpg']

    def test_large_thumb_untouched(self):
        news = {'title': 'Т', 'link': 'https://t.me/c/3',
                'images': ['https://cdn/ok.jpg'], '_thumb_only': True}
        calls = []
        with patch.object(anime_news_bot, '_cached_image_bytes',
                          side_effect=lambda u: calls.append(u) or _pic(1280, 720)), \
             patch.object(anime_news_bot, 'fetch_og_image',
                          side_effect=AssertionError('не должен вызываться')):
            asyncio.run(anime_news_bot._improve_thumb(news))
        assert len(calls) == 1

    def test_regular_post_skipped(self):
        news = {'title': 'Т', 'images': ['u']}
        with patch.object(anime_news_bot, '_cached_image_bytes',
                          side_effect=AssertionError('не должен вызываться')):
            asyncio.run(anime_news_bot._improve_thumb(news))

    def test_parser_marks_lazy_video(self, monkeypatch):
        html = ('<div class="tgme_widget_message" data-post="c/1">'
                '<a class="tgme_widget_message_video_thumb" '
                'style="background-image:url(\'https://cdn/thumb.jpg\')"></a>'
                '<time class="tgme_widget_message_video_duration">1:20</time>'
                '<div class="tgme_widget_message_text">'
                'Текст новости достаточно длинный для фильтра</div>'
                '<time datetime="2026-07-24T12:00:00+00:00"></time></div>')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        posts = anime_news_bot.get_telegram_channel('c', 'TG: T')
        assert posts[0]['_thumb_only'] is True

    def test_normal_photo_post_not_marked(self, monkeypatch):
        html = ('<div class="tgme_widget_message" data-post="c/2">'
                '<a class="tgme_widget_message_photo_wrap" '
                'style="background-image:url(\'https://cdn/p.jpg\')"></a>'
                '<div class="tgme_widget_message_text">'
                'Текст новости достаточно длинный для фильтра</div>'
                '<time datetime="2026-07-24T12:00:00+00:00"></time></div>')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(post_max_age_hours=24))
        posts = anime_news_bot.get_telegram_channel('c', 'TG: T')
        assert posts[0]['_thumb_only'] is False


class TestStartupReport:
    def _run(self, monkeypatch, tmp_path, enabled=True, deepl=''):
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('Alive', None), ('Dead', None)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(startup_report=enabled, thread_mode=True,
                                      video_enabled=True, image_dedup=True,
                                      open_moderation=True, tz_offset=3,
                                      is_source_enabled=lambda n: n != 'Dead'))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts',
                            anime_news_bot.ScheduledPosts(tmp_path / 'sp.json'))
        monkeypatch.setattr(anime_news_bot, 'pending_posts',
                            anime_news_bot.PendingPosts(tmp_path / 'pp.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes',
                            anime_news_bot.ImageHashes(tmp_path / 'ih.json'))
        monkeypatch.setattr(anime_news_bot, 'sent_links', MagicMock(_set={'a'}))
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', deepl)
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: {111, 222})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        asyncio.run(anime_news_bot.send_startup_report(app))
        return app.bot.send_message

    def test_sends_to_all_admins(self, monkeypatch, tmp_path):
        send = self._run(monkeypatch, tmp_path)
        assert send.await_count == 2
        text = send.await_args.kwargs['text']
        assert 'Бот запущен' in text
        assert 'Dead' in text and 'Pillow' in text

    def test_flags_missing_deepl(self, monkeypatch, tmp_path):
        text = self._run(monkeypatch, tmp_path).await_args.kwargs['text']
        assert 'нет ключа DeepL' in text

    def test_no_warning_with_deepl(self, monkeypatch, tmp_path):
        text = self._run(monkeypatch, tmp_path, deepl='key:fx').await_args.kwargs['text']
        assert 'нет ключа DeepL' not in text

    def test_disabled_setting(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, enabled=False).await_count == 0

    def test_send_error_does_not_raise(self, monkeypatch, tmp_path):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('A', None)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(startup_report=True, thread_mode=True,
                                      video_enabled=True, image_dedup=True,
                                      open_moderation=True, tz_offset=3,
                                      is_source_enabled=lambda n: True))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts', None)
        monkeypatch.setattr(anime_news_bot, 'pending_posts', None)
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        monkeypatch.setattr(anime_news_bot, 'sent_links', None)
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: {111})
        app = MagicMock()
        app.bot.send_message = AsyncMock(side_effect=TelegramError('blocked'))
        asyncio.run(anime_news_bot.send_startup_report(app))   # без исключения


class TestVideoCheckCommand:
    """Живая диагностика видео: отвечает в чат, а не в лог."""

    def _run(self, monkeypatch, posts, video_on=True, resolve=lambda u: None, args=None):
        monkeypatch.setattr(anime_news_bot, 'TELEGRAM_CHANNELS',
                            [('VanitasNews', 'TG: VanitasNews')])
        monkeypatch.setattr(anime_news_bot, 'custom_sources', None)
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(video_enabled=video_on))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        monkeypatch.setattr(anime_news_bot, 'get_telegram_channel',
                            lambda ch, lbl: posts)
        monkeypatch.setattr(anime_news_bot, '_resolve_video',
                            AsyncMock(side_effect=resolve))
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(anime_news_bot.videocheck_command(
            upd, MagicMock(args=args if args is not None else ['VanitasNews'])))
        return [c.args[0] for c in upd.message.reply_text.await_args_list]

    def test_video_disabled_is_flagged_first(self, monkeypatch):
        out = self._run(monkeypatch, [], video_on=False)
        assert 'ВЫКЛЮЧЕНА' in out[0]

    def test_working_video_reports_size(self, monkeypatch):
        posts = [{'link': 'https://t.me/ch/22344', '_video_note': 'прямой mp4 (46с)',
                  'video': 'https://cdn4.cdn-telegram.org/file/v.mp4'}]
        out = self._run(monkeypatch, posts, resolve=lambda u: b'x' * (12 * 1024 * 1024))
        body = out[-1]
        assert '12.0 МБ' in body and 'видео прикрепится' in body

    def test_download_failure_reported(self, monkeypatch):
        posts = [{'link': 'https://t.me/ch/22344', '_video_note': 'прямой mp4 (46с)',
                  'video': 'https://cdn4.cdn-telegram.org/file/v.mp4'}]
        out = self._run(monkeypatch, posts, resolve=lambda u: None)
        assert 'файл не отдался' in out[-1]

    def test_plain_url_needs_no_download(self, monkeypatch):
        posts = [{'link': 'https://t.me/ch/1', '_video_note': 'прямой mp4 (30с)',
                  'video': 'https://site.com/v.mp4'}]
        out = self._run(monkeypatch, posts, resolve=lambda u: u)
        assert 'не требуется' in out[-1] and 'прикрепится' in out[-1]

    def test_no_link_gives_explanation(self, monkeypatch):
        posts = [{'link': 'https://t.me/ch/22342', 'video': None,
                  '_video_note': 'Telegram не отдал mp4 даже на странице поста — только кадр'}]
        out = self._run(monkeypatch, posts)
        assert 'защиты контента' in out[-1]
        assert 'Telethon' in out[-1]

    def test_no_video_posts(self, monkeypatch):
        out = self._run(monkeypatch, [{'link': 'https://t.me/ch/1', '_video_note': ''}])
        assert 'видео не найдено' in out[-1] and 'не распознал' in out[-1]

    def test_empty_channel(self, monkeypatch):
        out = self._run(monkeypatch, [])
        assert 'Постов не получено' in out[-1]

    def test_parse_error_reported(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'TELEGRAM_CHANNELS', [('C', 'TG: C')])
        monkeypatch.setattr(anime_news_bot, 'custom_sources', None)
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(video_enabled=True))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)

        def boom(ch, lbl):
            raise RuntimeError('канал упал')
        monkeypatch.setattr(anime_news_bot, 'get_telegram_channel', boom)
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(anime_news_bot.videocheck_command(upd, MagicMock(args=['C'])))
        assert 'не прочитался' in upd.message.reply_text.await_args.args[0]

    def test_lists_channels_without_arg(self, monkeypatch):
        out = self._run(monkeypatch, [], args=[])
        assert 'VanitasNews' in out[0]

    def test_accepts_at_prefix(self, monkeypatch):
        posts = [{'link': 'https://t.me/ch/1', '_video_note': 'прямой mp4', 'video': None}]
        out = self._run(monkeypatch, posts, args=['@VanitasNews'])
        assert 'VanitasNews' in out[0]

    def test_custom_channels_included(self, monkeypatch, tmp_path):
        cs = anime_news_bot.CustomSources(tmp_path / 'cs.json')
        cs.add('tg', 'MyChan', 'TG: MyChan')
        monkeypatch.setattr(anime_news_bot, 'custom_sources', cs)
        monkeypatch.setattr(anime_news_bot, 'TELEGRAM_CHANNELS', [('Base', 'TG: Base')])
        pairs = anime_news_bot._tg_channels_available()
        assert ('MyChan', 'TG: MyChan') in pairs and ('Base', 'TG: Base') in pairs


class TestLogsFilter:
    def _log(self, tmp_path):
        p = tmp_path / 'bot.log'
        rows = [f'2026-07-24 18:00:00 - INFO - публикация #{i}\n' for i in range(120)]
        rows.insert(5, '2026-07-24 18:00:00 - INFO - TG ch/22342: видео — mp4 не отдан\n')
        p.write_text(''.join(rows), encoding='utf-8')
        return p

    def _run(self, monkeypatch, tmp_path, args):
        monkeypatch.setattr(anime_news_bot, 'LOG_FILE', self._log(tmp_path))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(anime_news_bot.logs_command(upd, MagicMock(args=args)))
        return upd.message.reply_text.await_args.args[0]

    def test_tail_misses_early_lines(self, monkeypatch, tmp_path):
        # Ровно проблема из прода: интересное в начале цикла, хвост его не видит
        assert 'видео' not in self._run(monkeypatch, tmp_path, [])

    def test_filter_finds_them(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, ['видео'])
        assert '22342' in out

    def test_filter_is_case_insensitive(self, monkeypatch, tmp_path):
        assert '22342' in self._run(monkeypatch, tmp_path, ['ВИДЕО'])

    def test_no_matches_hint(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, ['абырвалг'])
        assert 'нет' in out and '/logs видео' in out


class TestChannelBinding:
    """Привязка к каналу и проверка прав на публикацию."""

    def test_channel_id_is_numeric(self):
        expected = int(os.getenv('CHANNEL_ID', str(anime_news_bot.MAIN_CHANNEL_ID)))
        assert anime_news_bot.CHANNEL_ID == expected
        assert isinstance(anime_news_bot.CHANNEL_ID, int)

    def _check(self, member=None, chat_err=None):
        bot = MagicMock()
        if chat_err:
            bot.get_chat = AsyncMock(side_effect=chat_err)
        else:
            bot.get_chat = AsyncMock(return_value=MagicMock(title='Канал'))
        bot.get_me = AsyncMock(return_value=MagicMock(id=42))
        bot.get_chat_member = AsyncMock(return_value=member or MagicMock(
            status='administrator', can_post_messages=True))
        return asyncio.run(anime_news_bot._check_channel_access(bot))

    def test_admin_with_rights(self):
        ok, note = self._check()
        assert ok and note == 'Канал'

    def test_creator_ok(self):
        ok, _ = self._check(member=MagicMock(status='creator', can_post_messages=None))
        assert ok

    def test_not_admin(self):
        ok, note = self._check(member=MagicMock(status='member'))
        assert not ok and 'не администратор' in note

    def test_cannot_post(self):
        ok, note = self._check(
            member=MagicMock(status='administrator', can_post_messages=False))
        assert not ok and 'нет права публиковать' in note

    def test_chat_unreachable(self):
        from telegram.error import TelegramError
        ok, note = self._check(chat_err=TelegramError('chat not found'))
        assert not ok and 'недоступен' in note

    def test_any_exception_is_safe(self):
        ok, note = self._check(chat_err=RuntimeError('внезапно'))
        assert not ok and 'RuntimeError' in note

    def test_problem_shown_in_startup_report(self, monkeypatch, tmp_path):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('A', None)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(startup_report=True, thread_mode=True,
                                      video_enabled=True, image_dedup=True,
                                      open_moderation=True, tz_offset=3,
                                      llm_enabled=False,
                                      is_source_enabled=lambda n: True))
        for attr in ('scheduled_posts', 'pending_posts', 'image_hashes', 'sent_links'):
            monkeypatch.setattr(anime_news_bot, attr, None)
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: {1})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        app.bot.get_chat = AsyncMock(side_effect=TelegramError('chat not found'))
        asyncio.run(anime_news_bot.send_startup_report(app))
        text = app.bot.send_message.await_args.kwargs['text']
        assert 'не сработает' in text


class TestMainChannelBinding:
    """Все пути публикации ведут в основной канал, старого в коде нет."""

    def test_main_channel_constant(self):
        assert anime_news_bot.MAIN_CHANNEL_ID == -1003040322753

    def test_no_old_channel_in_source(self):
        source = Path(anime_news_bot.__file__).read_text(encoding='utf-8')
        assert 'Doyentor' not in source

    def test_default_is_main_channel(self, monkeypatch):
        monkeypatch.delenv('CHANNEL_ID', raising=False)
        import importlib
        mod = importlib.reload(anime_news_bot)
        try:
            assert mod.CHANNEL_ID == mod.MAIN_CHANNEL_ID
            assert mod.CHANNEL_FROM_ENV is False
        finally:
            importlib.reload(anime_news_bot)

    def test_env_overrides_and_is_flagged(self, monkeypatch):
        monkeypatch.setenv('CHANNEL_ID', '@SomeOther')
        import importlib
        mod = importlib.reload(anime_news_bot)
        try:
            assert mod.CHANNEL_ID == '@SomeOther'
            assert mod.CHANNEL_FROM_ENV is True
        finally:
            monkeypatch.delenv('CHANNEL_ID', raising=False)
            importlib.reload(anime_news_bot)

    def _publish_targets(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=False, require_image=False,
                                      open_moderation=True, extra_admins=[],
                                      llm_tags=False, tz_offset=3))
        monkeypatch.setattr(anime_news_bot, 'pending_posts',
                            anime_news_bot.PendingPosts(tmp_path / 'p.json'))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts',
                            anime_news_bot.ScheduledPosts(tmp_path / 's.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        monkeypatch.setattr(anime_news_bot, 'recent_subjects', None)
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_CHAT_ID', -100)
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_THREAD_ID', 10138)
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        targets = []

        async def spy(bot, news, target, video_file, **kw):
            targets.append(target)
            return True

        def query(data):
            q = MagicMock(data=data)
            for m in ('answer', 'edit_message_text', 'edit_message_caption',
                      'edit_message_reply_markup'):
                setattr(q, m, AsyncMock())
            q.message = MagicMock(text='пост', caption=None, chat_id=-100,
                                  message_id=5, message_thread_id=10138)
            return q

        with patch.object(anime_news_bot, '_send_post', new=spy), \
             patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()), \
             patch.object(anime_news_bot.asyncio, 'sleep', new=AsyncMock()):
            # 1. Кнопка «В канал», в том числе после ручной правки
            key = anime_news_bot.pending_posts.add(
                {'title': 'T', 'link': 'x', '_edited_text': 'Правленый текст'})
            upd = MagicMock(callback_query=query(f'pub:{key}'))
            upd.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID, full_name='D')
            asyncio.run(anime_news_bot.settings_callback(upd, MagicMock(bot=MagicMock())))

            # 2. Отложка по расписанию
            anime_news_bot.scheduled_posts.add(
                {'title': 'S', 'link': 'y'},
                datetime.now(timezone.utc) - timedelta(seconds=5))
            asyncio.run(anime_news_bot.publish_scheduled(MagicMock(bot=MagicMock())))

            # 3. Кнопка «📢 Сейчас» в /scheduled
            k2 = anime_news_bot.scheduled_posts.add(
                {'title': 'N', 'link': 'z'},
                datetime.now(timezone.utc) + timedelta(hours=2))
            upd2 = MagicMock(callback_query=query(f'snow:{k2}'))
            upd2.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID)
            asyncio.run(anime_news_bot.settings_callback(upd2, MagicMock(bot=MagicMock())))
        return targets

    def test_all_paths_go_to_channel(self, tmp_path, monkeypatch):
        targets = self._publish_targets(tmp_path, monkeypatch)
        assert len(targets) == 3
        assert all(t == anime_news_bot.CHANNEL_ID for t in targets)

    def test_env_mismatch_warned_in_report(self, monkeypatch, tmp_path):
        monkeypatch.setattr(anime_news_bot, 'CHANNEL_FROM_ENV', True)
        monkeypatch.setattr(anime_news_bot, 'CHANNEL_ID', '@Other')
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('A', None)])
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(startup_report=True, thread_mode=True,
                                      video_enabled=True, image_dedup=True,
                                      open_moderation=True, tz_offset=3,
                                      llm_enabled=False,
                                      is_source_enabled=lambda n: True))
        for attr in ('scheduled_posts', 'pending_posts', 'image_hashes',
                     'sent_links', 'recent_subjects'):
            monkeypatch.setattr(anime_news_bot, attr, None)
        monkeypatch.setattr(anime_news_bot, '_all_admin_ids', lambda: {1})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        app.bot.get_chat = AsyncMock(return_value=MagicMock(title='Другой'))
        app.bot.get_me = AsyncMock(return_value=MagicMock(id=1))
        app.bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status='administrator', can_post_messages=True))
        asyncio.run(anime_news_bot.send_startup_report(app))
        text = app.bot.send_message.await_args.kwargs['text']
        assert 'переменной CHANNEL_ID' in text
