"""Regression tests for moderation quality feedback introduced in PR #62."""
from pathlib import Path

import anime_news_bot as bot


def test_review_feedback_is_persistent_and_idempotent(tmp_path):
    store = bot.ChatModerationStore(Path(tmp_path) / 'moderation.json')
    assert store.ensure_review(
        'review-1', -100, 7, 'spam', 'warn', 'модель',
        'рекламная ссылка', 'пример сообщения'
    )
    assert store.record_review_feedback('review-1', 'correct')
    assert store.record_review_feedback('review-1', 'correct')
    assert not store.record_review_feedback('review-1', 'wrong')

    stats = store.stats()
    assert stats['feedback_total'] == 1
    assert stats['feedback_correct'] == 1
    assert stats.get('feedback_wrong', 0) == 0
    assert stats['by_category']['spam']['feedback_correct'] == 1
    assert stats['by_source']['модель']['correct'] == 1

    reloaded = bot.ChatModerationStore(Path(tmp_path) / 'moderation.json')
    assert reloaded.review('review-1')['feedback'] == 'correct'
    assert reloaded.stats()['feedback_total'] == 1


def test_wrong_feedback_tracks_false_positive_by_category_and_source(tmp_path):
    store = bot.ChatModerationStore(Path(tmp_path) / 'moderation.json')
    assert store.ensure_review(
        'review-2', -100, 8, 'flood', 'warn', 'локальные правила',
        'burst threshold', 'Нет'
    )
    assert store.record_review_feedback('review-2', 'wrong')

    stats = store.stats()
    assert stats['feedback_total'] == 1
    assert stats['feedback_wrong'] == 1
    assert stats['by_category']['flood']['feedback_wrong'] == 1
    assert stats['by_source']['локальные правила']['wrong'] == 1


def test_observe_markup_contains_feedback_without_destructive_controls():
    markup = bot._mod_report_markup(-100, 7, 'review-3', '')
    rows = markup.inline_keyboard
    assert len(rows) == 1
    callbacks = [button.callback_data for button in rows[0]]
    assert callbacks == [
        'mod:ok:-100:7:review-3',
        'mod:wrong:-100:7:review-3',
    ]


def test_active_markup_keeps_feedback_and_incident_controls():
    markup = bot._mod_report_markup(-100, 7, 'incident-4', 'incident-4')
    rows = markup.inline_keyboard
    assert len(rows) == 2
    assert [button.text for button in rows[0]] == ['✅ Верно', '❌ Ошибка']
    assert [button.text for button in rows[1]] == ['↩️ Снять', '🚫 Забанить']


def test_reset_stats_also_resets_review_dataset(tmp_path):
    store = bot.ChatModerationStore(Path(tmp_path) / 'moderation.json')
    store.ensure_review('review-5', -100, 9, 'spam', 'warn', 'модель', '', 'x')
    store.record_review_feedback('review-5', 'wrong')
    assert store.reset_stats()
    assert store.stats() == {}
    assert store.review('review-5') == {}
