"""Тесты: кнопки публикации, персистентный fuzzy-дедуп, флуд-ретрай, молчащие источники."""
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import anime_news_bot
from anime_news_bot import PendingPosts, SentLinksStore, _tg_call_flood_safe, _find_silent_sources


class TestPendingPosts:
    def test_add_get_pop(self, tmp_path):
        pp = PendingPosts(tmp_path / 'p.json')
        key = pp.add({'title': 'T', 'link': 'L', 'published_parsed': 'STRIP'})
        assert pp.get(key) == {'title': 'T', 'link': 'L'}
        assert pp.pop(key) == {'title': 'T', 'link': 'L'}
        assert pp.get(key) is None

    def test_persists_across_restart(self, tmp_path):
        p = tmp_path / 'p.json'
        key = PendingPosts(p).add({'title': 'T', 'link': 'L'})
        assert PendingPosts(p).get(key)['title'] == 'T'

    def test_ttl_cleanup(self, tmp_path):
        pp = PendingPosts(tmp_path / 'p.json')
        pp._items['old'] = {'news': {}, 'ts': time.time() - 8 * 86400}
        pp._cleanup()
        assert 'old' not in pp._items

    def test_max_items_cap(self, tmp_path):
        pp = PendingPosts(tmp_path / 'p.json')
        for i in range(pp.MAX_ITEMS + 20):
            pp._items[str(i)] = {'news': {}, 'ts': time.time() + i}
        pp._cleanup()
        assert len(pp._items) == pp.MAX_ITEMS
        assert '0' not in pp._items  # старейшие удалены


class TestPublishCallback:
    @pytest.fixture(autouse=True)
    def _place(self, monkeypatch):
        # Кнопки модерации работают только в своей ветке — фиксируем «место»
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_CHAT_ID', -100)
        monkeypatch.setattr(anime_news_bot, 'DISCUSSION_THREAD_ID', 10138)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(extra_admins=[], open_moderation=True))

    def _query(self, data, text='Пост'):
        q = MagicMock(data=data)
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.edit_message_caption = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        q.message = MagicMock(text=text, caption=None, chat_id=-100,
                              message_id=55, message_thread_id=10138)
        return q

    def test_pub_sends_to_channel(self, tmp_path, monkeypatch):
        pp = PendingPosts(tmp_path / 'p.json')
        monkeypatch.setattr(anime_news_bot, 'pending_posts', pp)
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        key = pp.add({'title': 'N', 'link': 'x', 'images': []})
        q = self._query(f'pub:{key}')
        update = MagicMock(callback_query=q)
        update.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID)
        sp = AsyncMock(return_value=True)
        monkeypatch.setattr(anime_news_bot, '_send_post', sp)
        asyncio.run(anime_news_bot.settings_callback(update, MagicMock()))
        assert sp.await_args.args[2] == anime_news_bot.CHANNEL_ID
        assert pp.get(key) is None
        q.answer.assert_awaited_with('📢 Опубликовано в канал!')

    def test_pub_failure_keeps_pending(self, tmp_path, monkeypatch):
        pp = PendingPosts(tmp_path / 'p.json')
        monkeypatch.setattr(anime_news_bot, 'pending_posts', pp)
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        key = pp.add({'title': 'N', 'link': 'x', 'images': []})
        q = self._query(f'pub:{key}')
        monkeypatch.setattr(anime_news_bot, '_send_post', AsyncMock(return_value=False))
        asyncio.run(anime_news_bot.settings_callback(MagicMock(callback_query=q), MagicMock()))
        assert pp.get(key) is not None  # можно нажать ещё раз

    def test_dis_hides(self, tmp_path, monkeypatch):
        pp = PendingPosts(tmp_path / 'p.json')
        monkeypatch.setattr(anime_news_bot, 'pending_posts', pp)
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        key = pp.add({'title': 'N', 'link': 'x'})
        q = self._query(f'dis:{key}')
        upd = MagicMock(callback_query=q)
        upd.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID)
        asyncio.run(anime_news_bot.settings_callback(upd, MagicMock()))
        assert pp.get(key) is None
        q.answer.assert_awaited_with('Скрыто')

    def test_stale_key_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'pending_posts', PendingPosts(tmp_path / 'p.json'))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        q = self._query('pub:999')
        asyncio.run(anime_news_bot.settings_callback(MagicMock(callback_query=q), MagicMock()))
        assert q.answer.await_args.kwargs.get('show_alert') is True


class TestRecentTitlesPersistence:
    def test_survives_restart(self, tmp_path):
        p = tmp_path / 's.json'
        s = SentLinksStore(p)
        asyncio.run(s.claim('https://a.com/1', 'Mamonotsukai no Musume Gets TV Anime'))
        s2 = SentLinksStore(p)
        assert len(s2._recent_titles) == 1
        assert s2.has_similar_title('Mamono Tsukai no Musume Anime: Cast Announced') is True


class TestFloodSafe:
    def test_retries_once_after_retryafter(self):
        calls = [0]
        async def flaky():
            calls[0] += 1
            if calls[0] == 1:
                raise anime_news_bot.RetryAfter(retry_after=0)
            return 'ok'
        async def run():
            with patch.object(anime_news_bot.asyncio, 'sleep', new=AsyncMock()):
                return await _tg_call_flood_safe(flaky)
        assert asyncio.run(run()) == 'ok'
        assert calls[0] == 2


class TestSilentSources:
    def test_detects_silent_skips_disabled_and_new(self, monkeypatch):
        stats = MagicMock()
        stats.get_by_source.return_value = {
            'Живой': {'last_success_at': datetime.now().isoformat()},
            'Молчун': {'last_success_at': (datetime.now() - timedelta(hours=100)).isoformat()},
            'Выкл': {'last_success_at': (datetime.now() - timedelta(hours=200)).isoformat()},
        }
        settings = MagicMock()
        settings.is_source_enabled = lambda n: n != 'Выкл'
        monkeypatch.setattr(anime_news_bot, 'stats', stats)
        monkeypatch.setattr(anime_news_bot, 'settings', settings)
        monkeypatch.setattr(anime_news_bot, 'SOURCES',
                            [('Живой', None), ('Молчун', None), ('Выкл', None), ('Новый', None)])
        assert _find_silent_sources(72) == ['Молчун']
