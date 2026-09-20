"""Regression tests for confidence-gated moderation decisions in PR #63."""
import json
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def test_ambiguous_llm_categories_require_high_confidence():
    state, confidence, threshold = bot._mod_decision_state(
        'spam', 'модель', confidence=.91, needs_review=False)
    assert state == 'review'
    assert confidence == pytest.approx(.91)
    assert threshold >= .95

    state, _, threshold = bot._mod_decision_state(
        'spam', 'модель', confidence=.99, needs_review=False)
    assert state == 'auto'
    assert threshold >= .95


def test_needs_review_overrides_high_model_confidence():
    state, _, _ = bot._mod_decision_state(
        'toxic', 'модель', confidence=.99, needs_review=True)
    assert state == 'review'


def test_media_borderline_result_is_review_only():
    state, _, threshold = bot._mod_decision_state(
        'spoiler_16', 'локальный детектор медиа', confidence=.86)
    assert state == 'review'
    assert threshold > .86


def test_deterministic_local_rule_is_not_confidence_gated():
    state, confidence, threshold = bot._mod_decision_state(
        'flood', 'локальные правила', confidence=None)
    assert state == 'auto'
    assert confidence == 1.0
    assert threshold == 1.0


@pytest.mark.asyncio
async def test_missing_model_confidence_can_never_authorize_action(monkeypatch):
    monkeypatch.setattr(bot, '_moderation_llm_ready', lambda: True)
    monkeypatch.setattr(bot, '_moderation_llm_budget_left', lambda: 100)
    call = AsyncMock(return_value=json.dumps({
        'violation': True,
        'category': 'toxic',
        'severity': 2,
        'reason': 'оскорбление',
        'evidence': 'ты дебил',
    }, ensure_ascii=False))
    monkeypatch.setattr(bot, '_llm_call', call)

    verdict = await bot._moderation_classify(-100, 'ты дебил')
    assert verdict['violation'] is True
    assert verdict['confidence'] == 0.0
    assert verdict['needs_review'] is True
    state, _, _ = bot._mod_decision_state(
        verdict['category'], 'модель',
        confidence=verdict['confidence'],
        needs_review=verdict['needs_review'])
    assert state == 'review'


@pytest.mark.asyncio
async def test_grounded_high_confidence_model_verdict_can_be_auto(monkeypatch):
    monkeypatch.setattr(bot, '_moderation_llm_ready', lambda: True)
    monkeypatch.setattr(bot, '_moderation_llm_budget_left', lambda: 100)
    call = AsyncMock(return_value=json.dumps({
        'violation': True,
        'category': 'toxic',
        'severity': 2,
        'confidence': .99,
        'needs_review': False,
        'reason': 'прямое оскорбление',
        'evidence': 'ты дебил',
    }, ensure_ascii=False))
    monkeypatch.setattr(bot, '_llm_call', call)

    verdict = await bot._moderation_classify(-100, 'ты дебил')
    assert verdict['confidence'] == pytest.approx(.99)
    state, _, threshold = bot._mod_decision_state(
        verdict['category'], 'модель',
        confidence=verdict['confidence'],
        needs_review=verdict['needs_review'])
    assert state == 'auto'
    assert .9 < threshold < .99
