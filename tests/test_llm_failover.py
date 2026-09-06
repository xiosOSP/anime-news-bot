"""Отказ провайдера не должен выключать обогащение целиком.

Бесплатные роутеры кончаются без предупреждения: квота, приостановка аккаунта,
снятая модель. Раньше любая такая ошибка выключала модель до перезапуска, и
посты выходили без тегов и перевода. Если задан запасной провайдер, бот один
раз переключается на него и продолжает работать.
"""
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'primary-key')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://primary.test/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'deepseek-v4-pro-free')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'fallback-key')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://fallback.test/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'mistral-small-latest')
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    monkeypatch.setattr(bot, '_llm_failover_at', '')
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_disabled_reason', '')
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_circuit_until', 0.0)
    monkeypatch.setattr(bot, '_queue_admin_alert', lambda _m: None)
    return monkeypatch


def _router(monkeypatch, primary_status, primary_body='{"error":"нет квоты"}'):
    """Основной отвечает ошибкой, запасной — нормально."""
    seen = []

    def post(url, **kwargs):
        seen.append(url)
        response = MagicMock()
        response.headers = {}
        if 'primary' in url:
            response.status_code = primary_status
            response.text = primary_body
            response.json.return_value = {}
        else:
            response.status_code = 200
            response.text = 'ok'
            response.json.return_value = {
                'choices': [{'message': {'content': 'ответ запасного'}}]}
        return response

    monkeypatch.setattr(bot.requests, 'post', post)
    return seen


class TestFailoverOnFatalErrors:
    @pytest.mark.parametrize('status', [401, 403, 402, 404])
    def test_switches_instead_of_disabling(self, providers, status):
        _router(providers, status)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_using_fallback is True
        assert bot._llm_disabled_runtime is False, 'модель выключилась вместо перехода'

    def test_next_request_goes_to_the_fallback(self, providers):
        seen = _router(providers, 401)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        answer = bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert answer == 'ответ запасного'
        assert 'primary' in seen[0] and 'fallback' in seen[1]

    def test_fallback_model_name_is_used(self, providers):
        _router(providers, 402)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_current()[2] == 'mistral-small-latest'

    def test_switch_happens_only_once(self, providers):
        """Если запасной тоже отказал — выключаемся, как раньше."""
        def both_fail(url, **kwargs):
            response = MagicMock()
            response.status_code = 401
            response.text = 'нет доступа'
            response.headers = {}
            response.json.return_value = {}
            return response

        providers.setattr(bot.requests, 'post', both_fail)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_using_fallback is True
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_disabled_runtime is True
        assert bot._llm_disabled_reason == 'auth'


class TestFailoverIsConservative:
    def test_transient_errors_do_not_switch(self, providers):
        """503 проходит сам — менять провайдера из-за него нельзя."""
        _router(providers, 503, 'временно недоступен')
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_using_fallback is False

    def test_rate_limit_does_not_switch(self, providers):
        response = MagicMock()
        response.status_code = 429
        response.text = 'rate limited'
        response.headers = {'Retry-After': '10'}
        response.json.return_value = {}
        providers.setattr(bot.requests, 'post', lambda *a, **k: response)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_using_fallback is False, 'лимит запросов не повод менять провайдера'

    def test_without_fallback_behaviour_is_unchanged(self, providers):
        providers.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
        providers.setattr(bot, 'LLM_FALLBACK_BASE_URL', '')
        providers.setattr(bot, 'LLM_FALLBACK_MODEL', '')
        _router(providers, 401)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_using_fallback is False
        assert bot._llm_disabled_runtime is True, 'без запасного должно быть как раньше'


class TestFailoverVisibility:
    def test_configured_flag(self, providers):
        assert bot._llm_fallback_configured() is True
        providers.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
        assert bot._llm_fallback_configured() is False

    def test_moment_of_switch_is_recorded(self, providers):
        _router(providers, 401)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_failover_at, 'момент перехода не записан'

    def test_current_provider_is_primary_by_default(self, providers):
        assert bot._llm_current()[2] == 'deepseek-v4-pro-free'
