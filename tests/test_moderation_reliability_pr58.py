"""Regression tests for PR #58 moderation reliability and outage diagnostics."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import anime_news_bot as bot


@pytest.mark.asyncio
async def test_unjudged_text_is_coalesced_by_chat_and_category(monkeypatch):
    fake = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: {1})
    monkeypatch.setattr(bot, 'MODERATION_UNJUDGED_NOTICE_SEC', 900)
    bot._moderation_unjudged_reports.clear()

    first = SimpleNamespace(chat_id=-100, message_id=1, link=None)
    second = SimpleNamespace(chat_id=-100, message_id=2, link=None)

    await bot._mod_unjudged_text(fake, first, 'spam', 'короткая ссылка')
    await bot._mod_unjudged_text(fake, second, 'spam', 'другая короткая ссылка')
    assert fake.send_message.await_count == 1

    # Different uncertainty classes must not hide one another.
    await bot._mod_unjudged_text(fake, second, 'toxic', 'неоднозначная реплика')
    assert fake.send_message.await_count == 2
    first_text = fake.send_message.await_args_list[0].args[1]
    assert '/modllmping' in first_text
    assert '/llmping' not in first_text


@pytest.mark.asyncio
async def test_media_failures_are_coalesced_by_reason_not_whole_chat(monkeypatch):
    store = SimpleNamespace(
        is_enabled=lambda _chat: True,
        log_decision=Mock(),
    )
    fake = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: name == 'chat_moderation')
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: {1})
    monkeypatch.setattr(bot, '_mod_actor', lambda _message: (7, 'User'))
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_NOTICE_SEC', 900)
    bot._moderation_media_reports.clear()

    first = SimpleNamespace(chat_id=-100, message_id=10, link=None)
    second = SimpleNamespace(chat_id=-100, message_id=11, link=None)

    await bot._mod_media_unchecked(fake, first, 'Файл превышает лимит проверки 20 МБ')
    await bot._mod_media_unchecked(fake, second, 'Файл превышает лимит проверки 21 МБ')
    assert fake.send_message.await_count == 1

    # A decoder failure is a separate technical incident and must still surface.
    await bot._mod_media_unchecked(fake, second, 'Ошибка декодирования или детектора: ValueError')
    assert fake.send_message.await_count == 2


@pytest.mark.asyncio
async def test_modllmping_uses_moderation_model_not_news_probe(monkeypatch):
    client = SimpleNamespace(
        model='moderation-test-model',
        daily_limit=120,
        configured=True,
        snapshot=Mock(side_effect=[
            {'requests': 4, 'error': ''},
            {'requests': 5, 'error': ''},
        ]),
    )
    classify = AsyncMock(return_value={
        'violation': False, 'category': '', 'severity': 0, 'reason': ''
    })
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, '_get_moderation_llm_client', lambda: client)
    monkeypatch.setattr(bot, '_moderation_classify', classify)

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=[])

    # admin_only preserves __wrapped__ through functools.wraps.
    command = getattr(bot.modllmping_command, '__wrapped__', bot.modllmping_command)
    await command(update, context)

    classify.assert_awaited_once()
    rendered = message.reply_text.await_args.args[0]
    assert 'moderation-test-model' in rendered
    assert 'отвечает' in rendered


def test_default_notice_intervals_are_not_notification_storms():
    assert bot.MODERATION_UNJUDGED_NOTICE_SEC >= 900
    assert bot.MODERATION_MEDIA_NOTICE_SEC >= 900
