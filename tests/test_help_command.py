"""/help: все команды бота по разделам, и справка не расходится с кодом."""
import inspect
import re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def registered() -> dict:
    """Команда → функция-обработчик, как их регистрирует main()."""
    source = inspect.getsource(bot.main)
    return {name: getattr(bot, fn) for name, fn in
            re.findall(r'CommandHandler\("([a-z_]+)",\s*(\w+)\)', source)}


def in_help() -> list:
    return [command for _title, rows in bot.HELP_SECTIONS for command, _text in rows]


def test_every_command_is_in_the_help_exactly_once():
    commands = in_help()
    assert len(commands) == len(set(commands)), 'команда в справке дважды'
    assert set(commands) == set(registered())


def _owner_only(handler) -> bool:
    inner = inspect.unwrap(handler)
    before_def = inspect.getsource(bot).split(f'def {inner.__name__}(')[0][-40:]
    return '@owner_only' in before_def or 'effective_user.id != ADMIN_ID' in inspect.getsource(inner)


def test_crown_marks_exactly_the_owner_only_commands():
    owner = {name for name, handler in registered().items() if _owner_only(handler)}
    assert owner == set(bot.HELP_OWNER_COMMANDS)


def test_chat_mark_matches_commands_allowed_in_groups():
    allowed = {name for name, handler in registered().items()
               if inspect.unwrap(handler).__name__ in bot._GROUP_COMMANDS}
    # /moderation — для админов группы, /cancel — без проверки прав вовсе.
    assert set(bot.HELP_CHAT_COMMANDS) == allowed | {'moderation', 'cancel'}


def test_messages_fit_telegram_and_keep_sections_whole():
    messages = bot._help_messages()
    assert all(len(m) <= bot.HELP_MESSAGE_LIMIT < 4096 for m in messages)
    text = '\n\n'.join(messages)
    for title, rows in bot.HELP_SECTIONS:
        holder = [m for m in messages if f'<b>{title}</b>' in m]
        assert len(holder) == 1
        assert all(f'/{command} ' in holder[0] for command, _ in rows)
    assert text.count('/historyok 👑 —') == 1 and text.count('/modunblock 💬 —') == 1
    assert '👑 — только владелец' in messages[-1]


def test_small_limit_splits_between_sections(monkeypatch):
    monkeypatch.setattr(bot, 'HELP_MESSAGE_LIMIT', 900)
    messages = bot._help_messages()
    assert len(messages) > 3
    for title, _rows in bot.HELP_SECTIONS:
        assert sum(f'<b>{title}</b>' in m for m in messages) == 1


def test_descriptions_are_html_safe(monkeypatch):
    monkeypatch.setattr(bot, 'HELP_SECTIONS', (('🧪 Проба', (('x', 'a <b> & c'),)),))
    assert 'a &lt;b&gt; &amp; c' in bot._help_messages()[0]


def test_one_section_or_one_command():
    only = bot._help_messages('модерация')
    assert len(only) == 1 and '/modhere' in only[0] and '/news' not in only[0]
    by_command = bot._help_messages('/backup')
    assert '💾' in by_command[0] and '/news' not in by_command[0]
    assert 'Разделы:' in bot._help_messages('нет такого')[0]


@pytest.mark.asyncio
async def test_help_command_sends_every_message(monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    reply = AsyncMock()
    update = NS(effective_user=NS(id=bot.ADMIN_ID), effective_chat=NS(id=1, type='private'),
                message=NS(reply_text=reply), effective_message=None)
    await bot.help_command(update, NS(args=[], bot=None))
    assert [c.args[0] for c in reply.await_args_list] == bot._help_messages()
    assert reply.await_args.kwargs['parse_mode'] == bot.ParseMode.HTML


@pytest.mark.asyncio
async def test_strangers_get_no_help(monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: False)
    deny = AsyncMock()
    monkeypatch.setattr(bot, 'deny_access', deny)
    reply = AsyncMock()
    await bot.help_command(NS(message=NS(reply_text=reply)), NS(args=[]))
    deny.assert_awaited_once()
    reply.assert_not_awaited()


def test_help_is_in_the_menu_and_in_start():
    assert 'BotCommand("help"' in inspect.getsource(bot.setup_bot_commands)
    assert '/help' in inspect.getsource(inspect.unwrap(bot.start))
