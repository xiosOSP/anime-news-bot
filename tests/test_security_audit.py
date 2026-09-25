"""Аудит безопасности: права, HTML в письмах админам, SSRF, секреты, ссылки.

Каждый блок — отдельная найденная дыра. Тесты написаны так, чтобы откат
исправления ронял хотя бы один из них.
"""
import asyncio
import ipaddress
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.error import Forbidden

import anime_news_bot as bot
import safe_http
from post_text import _strip_links

NOW = datetime.now(timezone.utc)
ANON = User(id=1087968824, first_name='Group', is_bot=True, username='GroupAnonymousBot')
CHANNEL_BOT = User(id=136817688, first_name='Channel', is_bot=True, username='Channel_Bot')
SERVICE = User(id=777000, first_name='Telegram', is_bot=False)
GROUP = Chat(id=-100111, type='supergroup', title='Обсуждение')


def _owner():
    return User(id=bot.ADMIN_ID, first_name='Owner', is_bot=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'cfg.json'))
    monkeypatch.setattr(bot, 'user_directory', bot.UserDirectory(tmp_path / 'users.json'))
    replies = []

    async def fake_reply(self, text, **kw):
        replies.append(text)
    monkeypatch.setattr(Message, 'reply_text', fake_reply)
    return replies


def _addadmin_by_reply(target_msg, args=None):
    cmd = Message(message_id=99, date=NOW, chat=GROUP, from_user=_owner(),
                  text='/addadmin', reply_to_message=target_msg)
    ctx = MagicMock(args=args or [])
    asyncio.run(bot.addadmin_command(Update(update_id=1, message=cmd), ctx))


# ---------- 1. /addadmin и is_admin: служебные «отправители» Telegram ----------

class TestServiceSendersNeverBecomeAdmins:
    def test_reply_to_anonymous_admin_is_refused_with_explanation(self, env):
        """Анонимный админ любой группы пишет от одного и того же id."""
        anon_msg = Message(message_id=5, date=NOW, chat=GROUP, from_user=ANON,
                           sender_chat=GROUP, text='hi')
        _addadmin_by_reply(anon_msg)
        assert bot.settings.extra_admins == []
        assert 'анонимно' in env[-1] and 'любой анонимный админ' in env[-1]

    def test_reply_to_a_bot_is_refused(self, env):
        """У обычного бота id не служебный — отказ держится только на is_bot."""
        other_bot = User(id=4242, first_name='Some', is_bot=True, username='somebot')
        _addadmin_by_reply(Message(message_id=5, date=NOW, chat=GROUP, from_user=other_bot, text='x'))
        assert 4242 not in bot.settings.extra_admins

    def test_reply_to_message_sent_as_channel_is_refused(self, env):
        """sender_chat скрывает автора, кто бы ни стоял в from_user."""
        chan = Chat(id=-100777, type='channel', title='Канал')
        person = User(id=5555, first_name='P', is_bot=False)
        _addadmin_by_reply(Message(message_id=5, date=NOW, chat=GROUP, from_user=person,
                                   sender_chat=chan, text='x'))
        assert 5555 not in bot.settings.extra_admins

    @pytest.mark.parametrize('arg', ['777000', '1087968824', '136817688', '-100123'])
    def test_numeric_service_or_chat_id_is_refused(self, env, arg):
        cmd = Message(message_id=99, date=NOW, chat=Chat(id=bot.ADMIN_ID, type='private'),
                      from_user=_owner(), text=f'/addadmin {arg}')
        asyncio.run(bot.addadmin_command(Update(update_id=1, message=cmd), MagicMock(args=[arg])))
        assert bot.settings.extra_admins == []
        assert 'не человек' in env[-1]

    def test_settings_refuse_bots_chats_and_service_ids(self, tmp_path):
        s = bot.BotSettings(tmp_path / 'cfg.json')
        assert s.add_admin(4242, is_bot=True) is False
        assert s.add_admin(0) is False
        assert s.add_admin(-100123) is False
        for uid in (1087968824, 136817688, 777000):
            assert s.add_admin(uid) is False
        assert s.extra_admins == []
        assert s.add_admin(4243) is True

    def test_service_id_granted_before_the_fix_is_dropped_on_load(self, tmp_path):
        path = tmp_path / 'cfg.json'
        path.write_text(json.dumps({'extra_admins': [1087968824, 777000, 136817688, 4243]}))
        assert bot.BotSettings(path).extra_admins == [4243]

    def test_is_admin_rejects_service_id_even_if_listed(self, monkeypatch):
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: {bot.ADMIN_ID, 777000, 4242})
        msg = Message(message_id=1, date=NOW, chat=GROUP, from_user=SERVICE, text='/backup')
        assert bot.is_admin(Update(update_id=1, message=msg)) is False

    def test_is_admin_rejects_listed_bot(self, monkeypatch):
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: {bot.ADMIN_ID, 4242})
        other_bot = User(id=4242, first_name='B', is_bot=True)
        msg = Message(message_id=1, date=NOW, chat=GROUP, from_user=other_bot, text='/logs')
        assert bot.is_admin(Update(update_id=1, message=msg)) is False

    def test_is_admin_rejects_message_sent_as_chat(self):
        """Даже с id владельца в from_user: автор сообщения от имени чата скрыт."""
        msg = Message(message_id=1, date=NOW, chat=GROUP, from_user=_owner(),
                      sender_chat=Chat(id=-100555, type='channel'), text='/logs')
        assert bot.is_admin(Update(update_id=1, message=msg)) is False

    def test_button_under_a_channel_post_still_works_for_admin(self):
        """У кнопки effective_message — пост бота; нажимает настоящий человек."""
        post = Message(message_id=1, date=NOW, chat=Chat(id=-100777, type='channel'),
                       sender_chat=Chat(id=-100777, type='channel'), text='пост')
        query = CallbackQuery(id='1', from_user=_owner(), chat_instance='x', message=post, data='settings:back')
        assert bot.is_admin(Update(update_id=1, callback_query=query)) is True
        plain = Message(message_id=2, date=NOW, chat=Chat(id=bot.ADMIN_ID, type='private'),
                        from_user=_owner(), text='/status')
        assert bot.is_admin(Update(update_id=2, message=plain)) is True


# ---------- 5. /addadmin @ник: права уходят нынешнему владельцу ника ----------

class TestUsernameGoesToCurrentHolder:
    def test_find_prefers_most_recently_seen(self, tmp_path):
        path = tmp_path / 'users.json'
        path.write_text(json.dumps({
            '111': {'username': 'candidate', 'name': 'Прежний', 'seen': '2025-01-01T00:00:00+00:00'},
            '222': {'username': 'candidate', 'name': 'Нынешний', 'seen': '2026-09-01T00:00:00+00:00'},
        }))
        assert bot.UserDirectory(path).find_by_username('@candidate') == (222, 'Нынешний')

    def test_remember_takes_username_away_from_previous_holder(self, tmp_path):
        d = bot.UserDirectory(tmp_path / 'users.json')
        d.remember(User(id=111, first_name='Прежний', is_bot=False, username='candidate'))
        d.remember(User(id=222, first_name='Нынешний', is_bot=False, username='candidate'))
        assert d.find_by_username('@candidate')[0] == 222
        assert '@candidate' not in d.describe(111)

    def test_confirmation_shows_the_id(self, env):
        bot.user_directory.remember(User(id=777002, first_name='Петя', is_bot=False, username='petya'))
        cmd = Message(message_id=9, date=NOW, chat=Chat(id=bot.ADMIN_ID, type='private'),
                      from_user=_owner(), text='/addadmin @petya')
        asyncio.run(bot.addadmin_command(Update(update_id=1, message=cmd), MagicMock(args=['@petya'])))
        assert 777002 in bot.settings.extra_admins
        assert '777002' in env[-1]


# ---------- 2. Поиск ролика в HTML: работа ограничена ----------

def _links_page(n):
    return ('<html><body><article><div>'
            + ''.join(f'<a href="/x{i}">a</a>' for i in range(n))
            + '</div></article></body></html>')


class TestVideoScanIsBounded:
    def test_text_prefix_matches_get_text(self):
        soup = BeautifulSoup('<div> Трейлер  <b>второго</b> сезона <i> уже</i> вышел '
                             + 'слово ' * 200 + '</div><p></p>', 'html.parser')
        for node in (soup.div, soup.b, soup.p):
            assert bot._text_prefix(node) == node.get_text(' ', strip=True)[:300]
        assert bot._text_prefix(None) == ''

    def test_text_prefix_stops_reading_at_the_limit(self):
        read = [0]

        def pieces():
            for _ in range(100_000):
                read[0] += 1
                yield 'слово'
        assert len(bot._text_prefix(NS(stripped_strings=pieces()))) == 300
        assert read[0] < 100

    def test_parent_text_is_not_collected_whole_for_every_link(self, monkeypatch):
        """Раньше на каждую ссылку собирался весь текст родителя — квадратично."""
        taken = [0]
        original = Tag.get_text

        def counting(self, *args, **kwargs):
            out = original(self, *args, **kwargs)
            taken[0] += len(out)
            return out
        monkeypatch.setattr(Tag, 'get_text', counting)
        bot._find_video_in_html(_links_page(3000), 'https://example.com/')
        assert taken[0] < 20_000

    def test_scanned_links_are_capped(self, monkeypatch):
        calls = [0]
        original = bot._text_prefix

        def counting(node, limit=bot.VIDEO_SCAN_CONTEXT_CHARS):
            calls[0] += 1
            return original(node, limit)
        monkeypatch.setattr(bot, '_text_prefix', counting)
        n = 2000
        page = ('<html><body><article><div>'
                + ''.join(f'<a href="/x{i}">a</a>' for i in range(n))
                + ''.join(f'<iframe src="/f{i}"></iframe>' for i in range(n))
                + ''.join(f'<lite-youtube videoid="v{i}"></lite-youtube>' for i in range(n))
                + '</div></article></body></html>')
        bot._find_video_in_html(page, 'https://example.com/')
        # по ссылке — два куска (сама ссылка и родитель), по плееру — один
        assert calls[0] <= 4 * bot.VIDEO_SCAN_MAX_NODES

    def test_parser_gets_at_most_the_cap(self, monkeypatch):
        seen = []
        real = bot.BeautifulSoup

        def recording(markup, *args, **kwargs):
            seen.append(len(markup))
            return real(markup, *args, **kwargs)
        monkeypatch.setattr(bot, 'BeautifulSoup', recording)
        bot._find_video_in_html('<p>' + 'x' * (2 * bot.VIDEO_SCAN_MAX_HTML_CHARS) + '</p>')
        assert seen and max(seen) <= bot.VIDEO_SCAN_MAX_HTML_CHARS

    def test_video_is_still_found(self):
        page = ('<html><head><title>Frieren trailer</title></head><body><article>'
                '<p>Frieren season 2 trailer</p>'
                '<iframe src="https://www.youtube.com/embed/abcDEF12345"></iframe>'
                '</article></body></html>')
        assert bot._find_video_in_html(page, 'https://example.com/') == \
            'https://www.youtube.com/embed/abcDEF12345'


class TestArticleFetchHasADeadline:
    @pytest.fixture
    def hanging_fetch(self, monkeypatch):
        release = threading.Event()

        def fetch(url):
            release.wait(5)
            return {'text': 'поздно ' * 50, 'video': 'https://www.youtube.com/watch?v=late'}
        monkeypatch.setattr(bot, 'fetch_article', fetch)
        monkeypatch.setattr(bot, 'ARTICLE_FETCH_TIMEOUT_SEC', 0.2)
        monkeypatch.setattr(bot, 'settings', NS(video_enabled=True, llm_read_article=True))
        yield release
        release.set()

    @staticmethod
    def _timed(coro_factory, release):
        """Время ожидания самой корутины; поток отпускаем сразу после замера,
        иначе asyncio.run ждал бы его при закрытии цикла."""
        async def run():
            started = time.perf_counter()
            try:
                await coro_factory()
            finally:
                release.set()
            return time.perf_counter() - started
        return asyncio.run(run())

    def test_video_discovery_does_not_wait_forever(self, hanging_fetch):
        news = {'title': 'Frieren trailer revealed', 'link': 'https://example.com/a'}
        assert self._timed(lambda: bot._discover_article_video(news), hanging_fetch) < 2
        assert 'video' not in news

    def test_llm_source_text_does_not_wait_forever(self, hanging_fetch):
        news = {'title': 'Frieren trailer revealed', 'link': 'https://example.com/b', 'summary': ''}
        assert self._timed(lambda: bot._llm_source_text(news), hanging_fetch) < 2
        assert 'video' not in news


# ---------- 3. Письма админам: ввод гостя не становится разметкой ----------

EVIL_NAME = '<a href="https://evil.example/x">Mallory</a>'


def _no_markup_from_guest(text):
    """Ввод гостя виден как текст, но разметкой не становится."""
    return '<a' not in text and '<b>' not in text and '&lt;a href=' in text


@pytest.fixture
def thread(monkeypatch):
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=True))
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, 'experiments', None)
    notify = AsyncMock(return_value=1)
    monkeypatch.setattr(bot, 'notify_admin', notify)
    return notify


def _guest():
    return MagicMock(id=424242, full_name=EVIL_NAME, username=None)


def _button(data):
    q = MagicMock(data=data)
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_caption = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock(text='Пост', caption=None, chat_id=-100, message_id=55,
                          message_thread_id=10138)
    upd = MagicMock(callback_query=q)
    upd.effective_user = _guest()
    return upd


class TestGuestInputInAdminMail:
    def test_hide(self, thread, tmp_path, monkeypatch):
        pp = bot.PendingPosts(tmp_path / 'p.json')
        monkeypatch.setattr(bot, 'pending_posts', pp)
        key = pp.add({'title': '<a href="https://evil.example/t">Подтвердите права</a>', 'link': 'x'})
        asyncio.run(bot.settings_callback(_button(f'dis:{key}'), MagicMock()))
        assert _no_markup_from_guest(thread.await_args.args[1])

    def test_publish(self, thread, tmp_path, monkeypatch):
        pp = bot.PendingPosts(tmp_path / 'p.json')
        monkeypatch.setattr(bot, 'pending_posts', pp)
        monkeypatch.setattr(bot, '_send_post', AsyncMock(return_value=True))
        key = pp.add({'title': 'N', 'link': 'x', 'images': []})
        asyncio.run(bot.settings_callback(_button(f'pub:{key}'), MagicMock()))
        assert _no_markup_from_guest(thread.await_args.args[1])

    def _input(self, mode, text):
        upd = MagicMock()
        upd.effective_chat = MagicMock(id=-100)
        upd.message = MagicMock(message_thread_id=10138, text=text)
        upd.message.reply_text = AsyncMock()
        upd.effective_user = _guest()
        ctx = MagicMock()
        ctx.user_data = {'await_input': {'mode': mode, 'key': 'k1', 'chat_id': -100, 'thread_id': 10138}}
        try:
            asyncio.run(bot.awaiting_input_handler(upd, ctx))
        except bot.ApplicationHandlerStop:
            pass

    def test_schedule(self, thread, monkeypatch):
        pending = MagicMock()
        pending.get = MagicMock(return_value={'title': 'T', 'link': 'https://x', 'source': 's'})
        monkeypatch.setattr(bot, 'pending_posts', pending)
        monkeypatch.setattr(bot, 'scheduled_posts', MagicMock())
        monkeypatch.setattr(bot, '_parse_schedule_time', lambda text: NOW + timedelta(hours=2))
        monkeypatch.setattr(bot, '_update_moderation_done', AsyncMock())
        self._input('schedule', '18:30')
        assert _no_markup_from_guest(thread.await_args.args[1])

    def test_edit(self, thread, monkeypatch):
        pending = MagicMock()
        pending.get = MagicMock(return_value={'title': 'T', 'link': 'https://x', 'source': 's'})
        pending.update_news = MagicMock(return_value=True)
        monkeypatch.setattr(bot, 'pending_posts', pending)
        monkeypatch.setattr(bot, '_update_preview_text', AsyncMock(return_value=True))
        self._input('edit', 'Премьера в апреле. <a href="https://evil.example/login">Подтвердите права</a> '
                            'и зайдите на evil.example сегодня.')
        text = thread.await_args.args[1]
        assert _no_markup_from_guest(text)
        body = text.split('в ветке:', 1)[1]
        assert 'evil.example' not in body          # ссылки из текста гостя вычищены
        assert 'Премьера в апреле' in body
        assert 'Ссылки из текста' in text

    def test_shadow_titles_are_escaped(self):
        """Заголовки из лент в сводке shadow mode — чужой ввод."""
        import inspect
        src = inspect.getsource(bot._check_news_cycle)
        assert 'html.escape(str(item.get("title") or "")[:120])' in src


# ---------- 4. Команды с данными бота — только в личке ----------

DATA_COMMANDS = ['backup_command', 'posts_command', 'logs_command', 'envfile_command',
                 'doctor_command', 'health_command', 'status', 'stats_command',
                 'analytics_command', 'admins_command', 'sources_command', 'settings_command']
GROUP_COMMANDS = ['chatinfo_command', 'modhere_command', 'modoff_command', 'modmiss_command',
                  'warns_command', 'unwarn_command']


def _group_update(user_id, chat_type='supergroup'):
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.reply_document = AsyncMock()
    message.message_thread_id = None
    message.reply_to_message = None
    upd = MagicMock(message=message, effective_message=message, callback_query=None)
    upd.effective_user = MagicMock(id=user_id, is_bot=False, full_name='X')
    upd.effective_chat = MagicMock(id=-100111, type=chat_type)
    return upd


def _ctx():
    ctx = MagicMock(args=[])
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    return ctx


class TestDataStaysInPrivate:
    @pytest.mark.parametrize('name', DATA_COMMANDS)
    def test_admin_in_group_gets_a_hint_in_private(self, name):
        upd, ctx = _group_update(bot.ADMIN_ID), _ctx()
        asyncio.run(getattr(bot, name)(upd, ctx))
        assert upd.message.reply_text.await_count == 0
        assert upd.message.reply_document.await_count == 0
        assert ctx.bot.send_document.await_count == 0
        ctx.bot.send_message.assert_awaited_once()
        sent = ctx.bot.send_message.await_args.kwargs
        assert sent['chat_id'] == bot.ADMIN_ID and sent['text'] == bot._PRIVATE_ONLY_HINT

    def test_hint_falls_back_to_one_line_in_group_when_private_is_closed(self):
        upd, ctx = _group_update(bot.ADMIN_ID), _ctx()
        ctx.bot.send_message = AsyncMock(side_effect=Forbidden('bot was blocked'))
        asyncio.run(bot.backup_command(upd, ctx))
        upd.message.reply_text.assert_awaited_once_with(bot._PRIVATE_ONLY_HINT)
        assert ctx.bot.send_document.await_count == 0

    @pytest.mark.parametrize('name', GROUP_COMMANDS)
    def test_group_scoped_commands_still_run_in_group(self, name, monkeypatch):
        monkeypatch.setattr(bot, 'chat_moderation', None)
        upd, ctx = _group_update(bot.ADMIN_ID), _ctx()
        try:
            asyncio.run(getattr(bot, name)(upd, ctx))
        except Exception:
            pass          # тело команды на моках может упасть — важно, что гейт пропустил
        hints = [c for c in upd.message.reply_text.await_args_list
                 if c.args and c.args[0] == bot._PRIVATE_ONLY_HINT]
        assert not hints and ctx.bot.send_message.await_count == 0

    def test_chatinfo_answers_in_group(self):
        upd, ctx = _group_update(bot.ADMIN_ID), _ctx()
        upd.message.message_thread_id = 10138
        asyncio.run(bot.chatinfo_command(upd, ctx))
        assert 'Chat ID' in upd.message.reply_text.await_args.args[0]

    @pytest.mark.parametrize('name', ['backup_command', 'logs_command', 'addadmin_command',
                                      'envfile_command', 'status'])
    def test_non_admin_in_group_gets_silence(self, name):
        upd, ctx = _group_update(987654321), _ctx()
        asyncio.run(getattr(bot, name)(upd, ctx))
        assert upd.message.reply_text.await_count == 0
        assert ctx.bot.send_message.await_count == 0

    def test_non_admin_in_private_is_still_told(self):
        upd, ctx = _group_update(987654321, chat_type='private'), _ctx()
        asyncio.run(bot.backup_command(upd, ctx))
        upd.message.reply_text.assert_awaited_once()

    def test_reply_keyboard_text_in_group(self):
        """«⚙️ Настройки» отвечает меню прямо из обработчика кнопок, без команды
        с собственным гейтом — защищает только проверка в самом обработчике."""
        upd, ctx = _group_update(bot.ADMIN_ID), _ctx()
        upd.message.text = bot.BTN_SETTINGS
        asyncio.run(bot.reply_button_handler(upd, ctx))
        assert upd.message.reply_text.await_count == 0
        assert ctx.bot.send_message.await_args.kwargs['text'] == bot._PRIVATE_ONLY_HINT

    def test_cancel_with_nothing_to_cancel_is_silent_in_group(self):
        upd, ctx = _group_update(987654321), _ctx()
        ctx.user_data = {}
        asyncio.run(bot.cancel_command(upd, ctx))
        assert upd.message.reply_text.await_count == 0
        private = _group_update(987654321, chat_type='private')
        asyncio.run(bot.cancel_command(private, ctx))
        private.message.reply_text.assert_awaited_once_with('Нечего отменять.')


# ---------- 6. SSRF: адреса со вложенным IPv4 ----------

def _resolve_to(monkeypatch, address):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    sockaddr = (address, 80, 0, 0) if family == socket.AF_INET6 else (address, 80)
    monkeypatch.setattr(safe_http.socket, 'getaddrinfo',
                        lambda *a, **k: [(family, socket.SOCK_STREAM, 6, '', sockaddr)])


class TestEmbeddedIPv4:
    @pytest.mark.parametrize('address,inner', [
        ('64:ff9b::a9fe:a9fe', '169.254.169.254'),
        ('::ffff:127.0.0.1', '127.0.0.1'),
        ('2002:7f00:1::1', '127.0.0.1'),
        ('::7f00:1', '127.0.0.1'),
    ])
    def test_unwraps(self, address, inner):
        assert safe_http._embedded_ipv4(ipaddress.ip_address(address)) == ipaddress.ip_address(inner)

    def test_plain_ipv6_has_nothing_inside(self):
        assert safe_http._embedded_ipv4(ipaddress.ip_address('2606:4700::1111')) is None

    @pytest.mark.parametrize('address', [
        '64:ff9b::a9fe:a9fe', '64:ff9b::a00:1', '64:ff9b::7f00:1', '::7f00:1', '::808:808',
        '::ffff:169.254.169.254', '::ffff:10.0.0.1', '2002:a9fe:a9fe::1', '2002:808:808::1',
        '64:ff9b:1::a00:1', '100.64.0.1', '240.0.0.1', '127.0.0.1',
    ])
    def test_blocked(self, monkeypatch, address):
        _resolve_to(monkeypatch, address)
        with pytest.raises(ValueError):
            safe_http.public_addresses('http://rebind.example/')

    @pytest.mark.parametrize('address', ['8.8.8.8', '2606:4700::1111', '::ffff:8.8.8.8',
                                         '64:ff9b::808:808'])
    def test_public_still_allowed(self, monkeypatch, address):
        """64:ff9b::808:808 — так DNS64 отдаёт обычный сайт без IPv6."""
        _resolve_to(monkeypatch, address)
        assert safe_http.public_addresses('http://site.example/')


# ---------- 7. /envfile и логи: пароль в URL ----------

class TestUrlCredentialsAreMasked:
    def test_proxy_password_is_hidden_host_is_shown(self):
        shown = bot._dotenv_shown_value('HTTPS_PROXY', 'http://user:s3cret@proxy.local:3128')
        assert 's3cret' not in shown and 'user' not in shown
        assert 'http://***@proxy.local:3128' in shown

    def test_proxy_without_scheme(self):
        shown = bot._dotenv_shown_value('HTTP_PROXY', 'user:s3cret@proxy.local:3128')
        assert 's3cret' not in shown and '***@proxy.local:3128' in shown

    def test_token_only_userinfo_in_a_dsn(self):
        shown = bot._dotenv_shown_value('DATABASE_DSN', 'postgres://t0ken@db.local/app')
        assert 't0ken' not in shown and 'postgres://***@db.local/app' in shown

    def test_password_in_any_variable(self):
        shown = bot._dotenv_shown_value('MIRROR', 'https://u:pw12345@mirror.example/x')
        assert 'pw12345' not in shown and 'mirror.example' in shown

    @pytest.mark.parametrize('name,value', [
        ('LLM_BASE_URL', 'https://api.orcarouter.ai/v1'),
        ('NOTE', 'пишите на a@b'),
        ('SHIKIMORI_NEWS_URL', 'https://shikimori.one/forum/news'),
    ])
    def test_plain_values_are_left_alone(self, name, value):
        assert bot._dotenv_shown_value(name, value) == f'<code>{value}</code>'

    def test_redact_secrets_masks_userinfo(self):
        out = bot._redact_secrets("ProxyError('http://user:p4ss@proxy.local:3128/' failed)")
        assert 'p4ss' not in out and 'proxy.local:3128' in out


# ---------- 8. Ссылки в посте: @каналы и любые доменные зоны ----------

class TestLinkStripping:
    @pytest.mark.parametrize('text,forbidden', [
        ('Подписывайтесь на @anime_news_ru, там всё.', 'anime_news_ru'),
        ('Премьера в апреле. Подробнее — @AnimeLeaks.', 'animeleaks'),
        ('Трейлер уже на animeshka.xyz/trailer, премьера в апреле.', 'animeshka'),
        ('Читайте на anime-portal.site', 'anime-portal'),
        ('Премьера в апреле. Подробнее...site.club тут', 'site.club'),
    ])
    def test_removed(self, text, forbidden):
        assert forbidden not in _strip_links(text).lower()

    def test_email_goes_whole(self):
        out = _strip_links('Пишите на press@kadokawa.co.jp, если есть вопросы по премьере.')
        assert '@' not in out and 'kadokawa' not in out
        assert 'вопросы по премьере' in out

    @pytest.mark.parametrize('text', [
        'Vol.2 выйдет в мае, а Re:Zero получит третий сезон.',
        'Dr.Stone и D.Gray-man вернутся осенью.',
        'Версия 1.2.3 и 3.5 миллиона просмотров.',
        'Выйдет 4.04 в 20.00 по московскому времени.',
        'Короткий @abc и адрес admin@localhost не трогаем.',
        'Сериал a.k.a. «Фрирен» идёт в 12 p.m. по Токио.',
        'Спасибо @ всем за 2.5 года!',
        'Анонс No.6 и Ep.12 состоится 5.10.',
    ])
    def test_kept(self, text):
        assert _strip_links(text) == text

    def test_long_dotted_chain_is_linear(self):
        started = time.perf_counter()
        _strip_links('a.' * 8000)
        assert time.perf_counter() - started < 1
