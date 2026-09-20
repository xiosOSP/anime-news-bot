"""Regression tests for PR #64 moderation calibration/readiness."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import anime_news_bot as bot


def test_wilson_lower_bound_is_conservative():
    assert 0.88 < bot._wilson_lower_bound(30, 30) < 0.90
    assert bot._wilson_lower_bound(0, 0) == 0.0


def test_quality_readiness_requires_real_admin_labels(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_LABELS', 30)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_PRECISION', .90)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_WILSON', .80)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_LABELS', 5)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_PRECISION', .75)

    result = bot._moderation_quality_readiness({
        'feedback_total': 5,
        'feedback_correct': 5,
        'by_category': {},
    })
    assert not result['ready']
    assert any('минимум 30' in reason for reason in result['reasons'])


def test_quality_readiness_accepts_well_labelled_observe_data(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_LABELS', 30)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_PRECISION', .90)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_WILSON', .80)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_LABELS', 5)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_PRECISION', .75)

    result = bot._moderation_quality_readiness({
        'feedback_total': 30,
        'feedback_correct': 30,
        'by_category': {
            'spam': {'feedback_total': 10, 'feedback_correct': 10},
            'flood': {'feedback_total': 8, 'feedback_correct': 8},
        },
    })
    assert result['ready']
    assert result['precision'] == 1.0
    assert result['wilson_lower'] >= .80


def test_weak_category_blocks_readiness_even_if_overall_is_good(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_LABELS', 30)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_PRECISION', .90)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_MIN_WILSON', .75)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_LABELS', 5)
    monkeypatch.setattr(bot, 'MODERATION_ACTIVE_CATEGORY_MIN_PRECISION', .75)

    result = bot._moderation_quality_readiness({
        'feedback_total': 40,
        'feedback_correct': 37,
        'by_category': {
            'spam': {'feedback_total': 5, 'feedback_correct': 3},
            'flood': {'feedback_total': 10, 'feedback_correct': 10},
        },
    })
    assert not result['ready']
    assert result['weak_categories'][0]['category'] == 'spam'


@pytest.mark.asyncio
async def test_modmode_active_is_blocked_before_readiness(monkeypatch):
    store = SimpleNamespace(
        stats=lambda: {'feedback_total': 2, 'feedback_correct': 2, 'by_category': {}},
        set_mode=Mock(return_value=True),
    )
    monkeypatch.setattr(bot, 'chat_moderation', store)

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(args=['active'])
    command = getattr(bot.modmode_command, '__wrapped__', bot.modmode_command)
    await command(update, context)

    store.set_mode.assert_not_called()
    rendered = message.reply_text.await_args.args[0]
    assert 'Active пока заблокирован' in rendered
    assert '/modquality' in rendered


@pytest.mark.asyncio
async def test_modmode_force_is_explicit_escape_hatch(monkeypatch):
    store = SimpleNamespace(
        stats=lambda: {'feedback_total': 0, 'feedback_correct': 0, 'by_category': {}},
        set_mode=Mock(return_value=True),
    )
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, '_audit_update', lambda *args, **kwargs: None)

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(args=['active', 'force'])
    command = getattr(bot.modmode_command, '__wrapped__', bot.modmode_command)
    await command(update, context)

    store.set_mode.assert_called_once_with('active')
    rendered = message.reply_text.await_args.args[0]
    assert 'Принудительно' in rendered
