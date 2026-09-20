"""Regression tests for PR #62 moderation quality dataset."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def make_store(tmp_path):
    return bot.ChatModerationStore(tmp_path / 'moderation.json')


def test_quality_feedback_is_persistent_idempotent_and_changeable(tmp_path):
    store = make_store(tmp_path)
    store.record_decision('spam', 'observe:warn')
    assert store.reserve_review(
        'sample1', chat_id=-100, user_id=7, category='spam', source='модель',
        action='observe:warn', reason='test', text='hello')
    assert store.record_feedback('sample1', 'incorrect')
    assert store.record_feedback('sample1', 'incorrect')

    stats = store.stats()
    assert stats['quality']['reviewed'] == 1
    assert stats['quality']['incorrect'] == 1
    assert stats['by_category']['spam']['incorrect'] == 1

    # A corrected human label must move the counter, not count a second review.
    assert store.record_feedback('sample1', 'correct')
    stats = store.stats()
    assert stats['quality']['reviewed'] == 1
    assert stats['quality']['incorrect'] == 0
    assert stats['quality']['correct'] == 1

    reloaded = bot.ChatModerationStore(store.path)
    assert reloaded.review('sample1')['feedback'] == 'correct'


def test_unavailable_is_not_counted_as_false_positive(tmp_path):
    store = make_store(tmp_path)
    store.record_unavailable('spam', 'llm')
    store.record_unavailable('media', 'media')
    stats = store.stats()
    assert stats['quality']['unavailable'] == 2
    assert stats['quality']['reviewed'] == 0
    assert stats['by_category']['spam']['unavailable'] == 1
    assert stats['by_category']['media']['unavailable'] == 1


def test_observe_report_markup_has_quality_buttons_without_sanction_buttons():
    markup = bot._mod_report_markup(-100, 7, '', review_id='abc123', allow_ban=True)
    buttons = [button for row in markup.inline_keyboard for button in row]
    labels = [button.text for button in buttons]
    callbacks = [button.callback_data for button in buttons]
    assert labels == ['✅ Верно', '❌ Ошибка']
    assert callbacks == ['modq:ok:abc123', 'modq:wrong:abc123']


def test_active_report_markup_keeps_feedback_and_undo_separate():
    markup = bot._mod_report_markup(-100, 7, 'incident1', review_id='abc123', allow_ban=True)
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert '✅ Верно' in labels
    assert '❌ Ошибка' in labels
    assert '↩️ Снять' in labels
    assert '🚫 Забанить' in labels


@pytest.mark.asyncio
async def test_observe_wrong_button_records_false_positive(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.record_decision('spam', 'observe:warn')
    assert store.reserve_review(
        'review42', chat_id=-100, user_id=7, category='spam', source='локальные правила',
        action='observe:warn', reason='короткая ссылка', text='Нет')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'is_admin', lambda _update: True)

    query = SimpleNamespace(
        data='modq:wrong:review42',
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(bot=SimpleNamespace())

    assert await bot.moderation_callback(update, context) is True
    assert store.review('review42')['feedback'] == 'incorrect'
    assert store.stats()['quality']['incorrect'] == 1
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_reset_stats_also_drops_stale_quality_cards(tmp_path):
    store = make_store(tmp_path)
    store.record_decision('flood', 'observe:warn')
    store.reserve_review(
        'old', chat_id=-100, user_id=7, category='flood', source='local',
        action='observe:warn', reason='burst', text='x')
    store.record_feedback('old', 'correct')
    assert store.reset_stats()
    assert store.stats() == {}
    assert store.review('old') == {}
