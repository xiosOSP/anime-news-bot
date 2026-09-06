"""Тесты на найденные при аудите дефекты:
конвейер качества в обоих путях отправки, диагностика видео,
защита от двойной публикации."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import anime_news_bot


def _calls_of(func_name: str) -> set:
    """Какие функции вызывают указанную."""
    tree = ast.parse(Path(anime_news_bot.__file__).read_text(encoding='utf-8'))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == func_name):
                    out.add(node.name)
    return out


class TestPipelineInBothPaths:
    """Раньше весь конвейер качества был вшит только в отправку в ветку,
    и режим канала публиковал сырые переводы без фильтров, тегов и дедупа."""

    @pytest.mark.parametrize('path', ['send_news', 'send_news_to_thread'])
    @pytest.mark.parametrize('step', ['_prepare_news_for_send', '_prepare_video_file',
                                      '_commit_image_fingerprint', '_mark_published'])
    def test_every_step_in_every_path(self, path, step):
        assert path in _calls_of(step), f'{path} не вызывает {step}'

    def test_pipeline_used_by_both(self):
        assert _calls_of('_prepare_news_for_send') == {'send_news', 'send_news_to_thread'}

    def test_enrichment_only_through_pipeline(self):
        # чтобы новые проверки нельзя было случайно добавить в один путь
        for step in ('_llm_enrich', '_image_duplicate', '_improve_thumb'):
            callers = _calls_of(step) - {'llm_command'}
            assert callers == {'_prepare_news_for_send'}, (step, callers)


class TestVideoDiagnosticsReachable:
    """Диагностика «новость про ролик, но ссылки нет» была недостижима:
    _prepare_video_file вызывался только когда видео уже есть."""

    def _note(self, news):
        anime_news_bot.settings = MagicMock(video_enabled=True)
        anime_news_bot.YT_DLP_AVAILABLE = True
        asyncio.run(anime_news_bot._prepare_video_file(news))
        return news.get('_video_note')

    def test_trailer_without_link_explained(self):
        assert 'ссылки на него нет' in self._note(
            {'title': 'Вышел трейлер второго сезона', 'summary': '', 'video': None})

    def test_text_news_silent(self):
        assert self._note(
            {'title': 'Манга завершилась', 'summary': '', 'video': None}) is None


class TestDoublePublishGuard:
    """Отправка занимает секунды — за это время успевало пройти второе нажатие
    или тик планировщика, и пост уходил в канал дважды."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(extra_admins=[], open_moderation=True, tz_offset=3))
        monkeypatch.setattr(anime_news_bot, 'pending_posts',
                            anime_news_bot.PendingPosts(tmp_path / 'p.json'))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts',
                            anime_news_bot.ScheduledPosts(tmp_path / 's.json'))
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_CHAT_ID', -100)
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_THREAD_ID', 10138)
        monkeypatch.setattr(anime_news_bot, 'recent_subjects', None)
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        anime_news_bot._publishing_now.clear()
        return anime_news_bot

    def _query(self, data):
        q = MagicMock(data=data)
        for m in ('answer', 'edit_message_text', 'edit_message_caption',
                  'edit_message_reply_markup'):
            setattr(q, m, AsyncMock())
        q.message = MagicMock(text='пост', caption=None, chat_id=-100,
                              message_id=5, message_thread_id=10138)
        return q

    async def _press(self, env, data):
        upd = MagicMock(callback_query=self._query(data))
        upd.effective_user = MagicMock(id=env.ADMIN_ID, full_name='Dobe')
        await env.settings_callback(upd, MagicMock(bot=MagicMock()))

    def test_double_tap_publishes_once(self, env):
        sent = []

        async def slow(bot, news, target, vf, **kw):
            await asyncio.sleep(0.02)
            sent.append(news.get('title'))
            return True

        key = env.pending_posts.add({'title': 'Новость', 'link': 'https://x/1'})

        async def main():
            with patch.object(env, '_send_post', new=slow):
                await asyncio.gather(self._press(env, f'pub:{key}'),
                                     self._press(env, f'pub:{key}'))
        asyncio.run(main())
        assert len(sent) == 1

    def test_job_and_button_publish_once(self, env):
        sent = []

        async def slow(bot, news, target, vf, **kw):
            await asyncio.sleep(0.02)
            sent.append(news.get('title'))
            return True

        key = env.scheduled_posts.add({'title': 'Отложенный', 'link': 'https://y/1'},
                                      datetime.now(timezone.utc) - timedelta(seconds=5))

        async def main():
            with patch.object(env, '_send_post', new=slow), \
                 patch.object(env, 'notify_admin', new=AsyncMock()), \
                 patch.object(env.asyncio, 'sleep', new=AsyncMock()):
                await asyncio.gather(
                    env.publish_scheduled(MagicMock(bot=MagicMock())),
                    self._press(env, f'snow:{key}'))
        asyncio.run(main())
        assert len(sent) == 1

    def test_guard_released_after_error(self, env):
        sent = []
        calls = {'n': 0}

        async def flaky(bot, news, target, vf, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('сеть упала')
            sent.append(news.get('title'))
            return True

        key = env.pending_posts.add({'title': 'Сбойный', 'link': 'https://z/1'})

        async def main():
            with patch.object(env, '_send_post', new=flaky):
                try:
                    await self._press(env, f'pub:{key}')
                except RuntimeError:
                    pass
                await self._press(env, f'pub:{key}')
        asyncio.run(main())
        assert len(sent) == 1
        assert not env._publishing_now          # блокировка снята

    def test_guard_is_per_post(self, env):
        with anime_news_bot._PublishGuard('a') as g1:
            assert g1.acquired
            with anime_news_bot._PublishGuard('b') as g2:
                assert g2.acquired
            with anime_news_bot._PublishGuard('a') as g3:
                assert not g3.acquired
        assert not anime_news_bot._publishing_now


class TestOverviewRobustness:
    """Обзор очереди не должен падать на неполных записях."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings', MagicMock(tz_offset=3))
        monkeypatch.setattr(anime_news_bot, 'scheduled_posts',
                            anime_news_bot.ScheduledPosts(tmp_path / 's.json'))
        return anime_news_bot

    def test_post_without_author(self, env):
        env.scheduled_posts.add({'title': 'Старый пост', 'link': 'x'},
                                datetime.now(timezone.utc) + timedelta(hours=1))
        text, _ = env._scheduled_overview()
        assert 'Старый пост' in text

    def test_post_without_title(self, env):
        env.scheduled_posts.add({'link': 'y'},
                                datetime.now(timezone.utc) + timedelta(hours=1))
        text, _ = env._scheduled_overview()
        assert 'без заголовка' in text

    def test_html_escaped(self, env):
        env.scheduled_posts.add({'title': 'Аниме <b>жирный</b> & "кавычки"', 'link': 'z'},
                                datetime.now(timezone.utc) + timedelta(hours=1))
        text, _ = env._scheduled_overview()
        assert '&lt;b&gt;' in text and '&amp;' in text

    def test_broken_date_ignored(self, tmp_path):
        import json
        p = tmp_path / 's.json'
        p.write_text(json.dumps(
            {'counter': 1, 'items': {'1': {'news': {'title': 'T'}, 'at': 'не дата'}}}))
        assert anime_news_bot.ScheduledPosts(p).all() == []

    def test_safe_edit_ignores_not_modified(self):
        from telegram.error import TelegramError
        q = MagicMock()
        q.edit_message_text = AsyncMock(
            side_effect=TelegramError('Message is not modified'))
        asyncio.run(anime_news_bot._safe_edit(q, 'текст', None))
