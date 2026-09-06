"""Тесты выдачи админки: ответом на сообщение, по @имени, по id."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot
from anime_news_bot import UserDirectory


def _user(uid, username=None, name='Вася Пупкин', is_bot=False):
    return MagicMock(id=uid, username=username, full_name=name, is_bot=is_bot)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(anime_news_bot, 'settings',
                        anime_news_bot.BotSettings(tmp_path / 'cfg.json'))
    monkeypatch.setattr(anime_news_bot, 'user_directory',
                        UserDirectory(tmp_path / 'users.json'))
    return anime_news_bot


async def _call(cmd, args=None, reply_from=None, caller=None):
    upd = MagicMock()
    upd.effective_user = MagicMock(id=caller or anime_news_bot.ADMIN_ID)
    upd.message = MagicMock(reply_to_message=None)
    if reply_from is not None:
        upd.message.reply_to_message = MagicMock(from_user=reply_from)
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock(args=args or [])
    ctx.bot.get_chat = AsyncMock(side_effect=Exception('нет такого'))
    await cmd(upd, ctx)
    return upd.message.reply_text.await_args.args[0]


class TestUserDirectory:
    def test_remember_and_find(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(_user(777, 'vasya', 'Вася Пупкин'))
        assert d.find_by_username('@vasya') == (777, 'Вася Пупкин')
        assert d.find_by_username('vasya') == (777, 'Вася Пупкин')
        assert d.find_by_username('VASYA') == (777, 'Вася Пупкин')

    def test_unknown_username(self, tmp_path):
        assert UserDirectory(tmp_path / 'u.json').find_by_username('@nobody') is None

    def test_describe(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(_user(777, 'vasya', 'Вася Пупкин'))
        assert d.describe(777) == 'Вася Пупкин (@vasya)'
        assert d.describe(999) == '999'

    def test_describe_without_username(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(_user(777, None, 'Без Ника'))
        assert d.describe(777) == 'Без Ника'

    def test_bots_ignored(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(_user(1, 'somebot', 'Bot', is_bot=True))
        assert len(d) == 0

    def test_none_safe(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(None)
        assert len(d) == 0

    def test_persists(self, tmp_path):
        p = tmp_path / 'u.json'
        UserDirectory(p).remember(_user(777, 'vasya'))
        assert UserDirectory(p).find_by_username('@vasya')[0] == 777

    def test_username_change_updates(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.remember(_user(777, 'old_name'))
        d.remember(_user(777, 'new_name'))
        assert d.find_by_username('@new_name')[0] == 777
        assert d.find_by_username('@old_name') is None

    def test_capped(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        for i in range(anime_news_bot.USER_DIRECTORY_MAX + 20):
            d.remember(_user(i, f'user{i}'))
        assert len(d) <= anime_news_bot.USER_DIRECTORY_MAX

    def test_corrupt_file_safe(self, tmp_path):
        p = tmp_path / 'u.json'
        p.write_text('не json')
        assert len(UserDirectory(p)) == 0


class TestGrantByReply:
    def test_grants(self, env):
        out = asyncio.run(_call(env.addadmin_command,
                                reply_from=_user(777001, 'vasya')))
        assert 777001 in env.settings.extra_admins
        assert 'Вася Пупкин' in out

    def test_revokes(self, env):
        env.settings.add_admin(777001)
        out = asyncio.run(_call(env.deladmin_command,
                                reply_from=_user(777001, 'vasya')))
        assert 777001 not in env.settings.extra_admins
        assert 'больше не админ' in out

    def test_reply_remembers_user(self, env):
        asyncio.run(_call(env.addadmin_command, reply_from=_user(777001, 'vasya')))
        assert env.user_directory.find_by_username('@vasya')[0] == 777001


class TestGrantByUsername:
    def test_known_user(self, env):
        env.user_directory.remember(_user(777002, 'petya', 'Петя Иванов'))
        out = asyncio.run(_call(env.addadmin_command, args=['@petya']))
        assert 777002 in env.settings.extra_admins
        assert 'Петя Иванов' in out

    def test_without_at_sign(self, env):
        env.user_directory.remember(_user(777002, 'petya'))
        asyncio.run(_call(env.addadmin_command, args=['petya']))
        assert 777002 in env.settings.extra_admins

    def test_unknown_explains(self, env):
        out = asyncio.run(_call(env.addadmin_command, args=['@nobody_here']))
        assert env.settings.extra_admins == []
        assert 'ответь этой командой' in out.lower()

    def test_get_chat_fallback(self, env):
        upd = MagicMock()
        upd.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID)
        upd.message = MagicMock(reply_to_message=None)
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock(args=['@known_by_tg'])
        ctx.bot.get_chat = AsyncMock(
            return_value=MagicMock(id=555, full_name='Из Telegram'))
        asyncio.run(env.addadmin_command(upd, ctx))
        assert 555 in env.settings.extra_admins


class TestGrantById:
    def test_numeric_still_works(self, env):
        asyncio.run(_call(env.addadmin_command, args=['777003']))
        assert 777003 in env.settings.extra_admins

    def test_shows_name_if_known(self, env):
        env.user_directory.remember(_user(777003, 'kolya', 'Коля'))
        out = asyncio.run(_call(env.addadmin_command, args=['777003']))
        assert 'Коля' in out


class TestGuards:
    def test_only_main_admin(self, env):
        upd = MagicMock(callback_query=None)
        upd.effective_user = MagicMock(id=999999)
        upd.message = MagicMock(reply_to_message=None)
        upd.message.reply_text = AsyncMock()
        asyncio.run(env.addadmin_command(upd, MagicMock(args=['777'])))
        assert env.settings.extra_admins == []

    def test_cannot_demote_self(self, env):
        out = asyncio.run(_call(env.deladmin_command, args=[str(env.ADMIN_ID)]))
        assert 'нельзя' in out

    def test_main_admin_already(self, env):
        out = asyncio.run(_call(env.addadmin_command, args=[str(env.ADMIN_ID)]))
        assert 'главный админ' in out

    def test_no_target_hint(self, env):
        out = asyncio.run(_call(env.addadmin_command))
        assert 'ответь' in out.lower()

    def test_double_grant(self, env):
        env.settings.add_admin(777004)
        out = asyncio.run(_call(env.addadmin_command, args=['777004']))
        assert 'уже админ' in out

    def test_revoke_non_admin(self, env):
        out = asyncio.run(_call(env.deladmin_command, args=['777005']))
        assert 'не был админом' in out


class TestAdminsList:
    def test_shows_names(self, env):
        env.user_directory.remember(_user(env.ADMIN_ID, 'dobe', 'Dobe'))
        env.user_directory.remember(_user(777002, 'petya', 'Петя Иванов'))
        env.settings.add_admin(777002)
        out = asyncio.run(_call(env.admins_command))
        assert 'Dobe (@dobe)' in out
        assert 'Петя Иванов (@petya)' in out

    def test_empty_list(self, env):
        out = asyncio.run(_call(env.admins_command))
        assert 'Дополнительных админов нет' in out

    def test_shows_instructions(self, env):
        out = asyncio.run(_call(env.admins_command))
        assert '/addadmin' in out and '/deladmin' in out


class TestRememberHandler:
    def test_records_sender(self, env):
        upd = MagicMock()
        upd.effective_user = _user(777006, 'ivan', 'Иван')
        upd.message = MagicMock(reply_to_message=None)
        asyncio.run(env.remember_user_handler(upd, MagicMock()))
        assert env.user_directory.find_by_username('@ivan')[0] == 777006

    def test_records_reply_author(self, env):
        upd = MagicMock()
        upd.effective_user = _user(1, 'admin', 'Админ')
        upd.message = MagicMock(
            reply_to_message=MagicMock(from_user=_user(777007, 'target', 'Цель')))
        asyncio.run(env.remember_user_handler(upd, MagicMock()))
        assert env.user_directory.find_by_username('@target')[0] == 777007

    def test_never_raises(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'user_directory', None)
        asyncio.run(env.remember_user_handler(MagicMock(), MagicMock()))


class TestDirectoryPersistenceThrottle:
    """Справочник пополняется на каждое сообщение — писать файл каждый раз нельзя."""

    def test_writes_are_throttled(self, tmp_path, monkeypatch):
        d = UserDirectory(tmp_path / 'u.json')
        writes = []
        real = d.path.write_text
        monkeypatch.setattr(type(d.path), 'write_text',
                            lambda self, *a, **k: writes.append(1) or real(*a, **k))
        for i in range(50):
            d.remember(_user(i, f'user{i}'))
        assert len(writes) <= 2          # не по записи на пользователя

    def test_flush_persists(self, tmp_path):
        p = tmp_path / 'u.json'
        d = UserDirectory(p)
        for i in range(20):
            d.remember(_user(i, f'user{i}'))
        d.flush()
        assert len(UserDirectory(p)) == 20

    def test_flush_noop_when_clean(self, tmp_path):
        d = UserDirectory(tmp_path / 'u.json')
        d.flush()
        d.flush()                        # не должно падать

    def test_remember_now_writes_immediately(self, tmp_path):
        p = tmp_path / 'u.json'
        d = UserDirectory(p)
        d.remember_now(_user(777, 'vasya'))
        assert UserDirectory(p).find_by_username('@vasya')[0] == 777

    def test_grant_flushes_directory(self, env, tmp_path):
        asyncio.run(_call(env.addadmin_command, reply_from=_user(777010, 'newadmin')))
        reloaded = UserDirectory(env.user_directory.path)
        assert reloaded.find_by_username('@newadmin')[0] == 777010
