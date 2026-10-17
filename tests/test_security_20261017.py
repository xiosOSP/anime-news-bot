"""Аудит безопасности, октябрь 2026."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('name', ['TG_ACCOUNT_SESSION', 'TG_ACCOUNT_API_HASH', 'EXPERIMENT_SALT'])
def test_envfile_hides_account_session(name):
    """По строке сессии входят в аккаунт Telegram без кода: в чат её не выводим."""
    value = '1ApWapzMBu' + 'x' * 300
    shown = bot._dotenv_shown_value(name, value)
    assert value[:10] not in shown and 'скрыто' in shown


def _await_input(mode, *, admin, open_moderation, monkeypatch):
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=open_moderation))
    monkeypatch.setattr(bot, 'is_admin', lambda update: admin)
    pending = MagicMock()
    pending.get = MagicMock(return_value={'title': 'T', 'link': 'https://x', 'source': 's'})
    pending.update_news = MagicMock(return_value=True)
    monkeypatch.setattr(bot, 'pending_posts', pending)
    scheduled = MagicMock()
    monkeypatch.setattr(bot, 'scheduled_posts', scheduled)
    monkeypatch.setattr(bot, '_update_preview_text', AsyncMock(return_value=True))
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.message = MagicMock(message_thread_id=10138, text='Новый текст поста')
    upd.message.reply_text = AsyncMock()
    upd.effective_user = MagicMock(id=424242, full_name='Гость', username=None)
    ctx = MagicMock()
    ctx.user_data = {'await_input': {'mode': mode, 'key': 'k1', 'chat_id': -100, 'thread_id': 10138}}
    try:
        asyncio.run(bot.awaiting_input_handler(upd, ctx))
    except bot.ApplicationHandlerStop:
        pass
    return ctx, pending, scheduled


def test_guest_edit_dropped_after_open_moderation_turned_off(monkeypatch):
    ctx, pending, _ = _await_input('edit', admin=False, open_moderation=False, monkeypatch=monkeypatch)
    pending.update_news.assert_not_called()
    assert 'await_input' not in ctx.user_data


def test_guest_edit_still_works_with_open_moderation(monkeypatch):
    _, pending, _ = _await_input('edit', admin=False, open_moderation=True, monkeypatch=monkeypatch)
    pending.update_news.assert_called_once()


def test_reschedule_needs_admin_even_with_open_moderation(monkeypatch):
    """Разжалованный админ с недоведённым переносом отложки не меняет время."""
    ctx, _, scheduled = _await_input('reschedule', admin=False, open_moderation=True,
                                     monkeypatch=monkeypatch)
    scheduled.get.assert_not_called()
    assert 'await_input' not in ctx.user_data


def test_admin_reschedule_goes_through(monkeypatch):
    _, _, scheduled = _await_input('reschedule', admin=True, open_moderation=False,
                                   monkeypatch=monkeypatch)
    scheduled.get.assert_called()


def test_streaming_dependencies_are_patched():
    """urllib3 < 2.8: злой сервер вешает потоковое чтение (бесконечный цикл
    на сжатом chunked-ответе) или раздувает память строкой размера чанка;
    бот так качает картинки и ролики с чужих серверов. idna < 3.15: имя
    домена из ленты надолго занимает процессор."""
    from pathlib import Path
    pins = Path(bot.__file__).with_name('requirements.txt').read_text(encoding='utf-8')
    assert 'urllib3==2.8.0' in pins and 'idna==3.15' in pins
