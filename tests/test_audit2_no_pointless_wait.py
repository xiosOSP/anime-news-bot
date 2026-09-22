"""Пост не ждёт модель, которая не может вернуться.

Отсрочка поста задумана для отказов бесплатных тарифов: они длятся минуты,
а сырой пост остаётся в канале навсегда. Но ключ, отклонённый провайдером,
выключает модель до перезапуска, а исчерпанный лимит — до завтра. Ждать их
бесполезно: пост опаздывает на срок отсрочки (до 15 минут) и уходит тем же
обычным путём. Пока ключ был отклонён, так опаздывал каждый пост канала.
"""
import asyncio
from pathlib import Path

import pytest

import anime_news_bot as bot


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', Path(tmp_path))
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.groq.com/openai/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'openai/gpt-oss-120b')
    settings = bot.BotSettings(Path(tmp_path) / 'settings.json')
    settings.llm_enabled = True
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, '_llm_deferral_store', None)
    monkeypatch.setattr(bot, '_llm_deferred', {})
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_disabled_reason', '')
    monkeypatch.setattr(bot, '_llm_circuit_until', 0.0)
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('до провайдера дело доходить не должно')))
    return settings


def _enrich():
    return asyncio.run(bot._llm_enrich({'title': 'Новость', 'link': 'https://example.com/n1',
                                        'summary': 'текст'}))


class TestItDoesNotWaitForTheImpossible:
    def test_a_rejected_key_does_not_hold_the_post(self, model, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        assert _enrich() == 'off'

    def test_an_exhausted_daily_limit_does_not_hold_the_post(self, model):
        model.llm_day = bot._local_now().strftime('%Y-%m-%d')
        model.llm_calls_today = bot.LLM_DAILY_LIMIT
        assert _enrich() == 'off'

    def test_an_exhausted_token_budget_does_not_hold_the_post(self, model, monkeypatch, tmp_path):
        """Бюджет считается исчерпанным, когда не хватает на самый дешёвый вызов.

        can_charge(0) отвечал бы «можно» до последнего токена, и пост ждал бы
        вызова, который бюджет всё равно не пропустит.
        """
        monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
        monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 10_000)
        budget = bot.LLMBudgetStore(Path(tmp_path) / 'budget.json')
        cheapest = bot._estimate_llm_tokens([], bot.LLM_MAX_TOKENS)
        budget.charge(10_000 - cheapest + 1)     # остаток меньше одного вызова
        monkeypatch.setattr(bot, 'llm_budget', budget)
        assert budget.can_charge(0), 'граничный случай: can_charge(0) ещё «можно»'
        assert bot._llm_outage_is_temporary() is False

    def test_room_for_one_call_is_still_worth_waiting(self, model, monkeypatch, tmp_path):
        monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
        monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 10_000)
        budget = bot.LLMBudgetStore(Path(tmp_path) / 'budget.json')
        budget.charge(10_000 - bot._estimate_llm_tokens([], bot.LLM_MAX_TOKENS))
        monkeypatch.setattr(bot, 'llm_budget', budget)
        assert bot._llm_outage_is_temporary() is True

    def test_a_config_failure_does_not_hold_the_post(self, model, monkeypatch):
        """Несуществующая модель и прочие отказы настройки — тоже до перезапуска."""
        monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'model')
        assert bot._llm_outage_is_temporary() is False


class TestItStillWaitsForABriefPause:
    def test_a_rate_limit_pause_is_worth_waiting(self, model, monkeypatch):
        """Пауза после серии 429 закрывается сама — ради неё отсрочка и есть."""
        monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
        monkeypatch.setattr(bot, '_llm_circuit_until', bot.time.monotonic() + 300)
        assert _enrich() == 'defer'

    def test_a_healthy_model_is_temporary_by_definition(self, model):
        assert bot._llm_outage_is_temporary() is True
