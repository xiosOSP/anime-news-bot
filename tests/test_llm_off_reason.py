"""`/llm` должен называть настоящую причину отказа.

Раньше на любой отказ приходило «Ключ отклонён провайдером — проверь
LLM_API_KEY», даже когда ключ был совершенно исправен: в логе при этом стоял
429, то есть запросы проходили и упирались в лимит запросов в минуту.
Сообщение уводило в сторону и заставляло искать проблему не там.
"""
import time

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'key')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://provider.test/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'deepseek-v4-pro-free')
    monkeypatch.setattr(bot, '_llm_quota_left', lambda: 500)
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    monkeypatch.setattr(bot, '_llm_last_provider_error', '')
    monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
    monkeypatch.setattr(bot, '_llm_circuit_until', 0.0)
    yield


class TestReasonMatchesReality:
    def test_rate_limit_does_not_blame_the_key(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
        monkeypatch.setattr(bot, '_llm_circuit_until', time.monotonic() + 25)
        text = bot._llm_off_reason()
        assert 'лимит запросов' in text
        assert 'LLM_API_KEY' not in text, 'снова обвиняем ключ без причины'

    def test_rate_limit_shows_remaining_wait(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
        monkeypatch.setattr(bot, '_llm_circuit_until', time.monotonic() + 25)
        assert 'с' in bot._llm_off_reason()

    def test_auth_mentions_quota_as_an_option(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        text = bot._llm_off_reason()
        assert 'ключ' in text.lower() and 'квот' in text.lower()

    def test_billing_is_named_directly(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'billing')
        assert 'средств' in bot._llm_off_reason()

    def test_unknown_model_points_at_the_variable(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'model')
        assert 'LLM_MODEL' in bot._llm_off_reason()

    def test_not_configured_is_recognised(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', '')
        assert 'не настроена' in bot._llm_off_reason()

    def test_daily_limit_is_mentioned(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', '')
        monkeypatch.setattr(bot, '_llm_quota_left', lambda: 0)
        assert 'LLM_DAILY_LIMIT' in bot._llm_off_reason()


class TestReasonShowsEvidence:
    def test_provider_answer_is_included(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        monkeypatch.setattr(bot, '_llm_last_provider_error',
                            'HTTP 401: {"error":"Insufficient quota"}')
        assert 'Insufficient quota' in bot._llm_off_reason()

    def test_fallback_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        # При переключении настроенность проверяется у запасного, поэтому
        # задаём его целиком — как это и происходит в бою.
        monkeypatch.setattr(bot, '_llm_using_fallback', True)
        monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'fallback-key')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://fallback.test/v1')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'mistral-small-latest')
        assert 'mistral-small-latest' in bot._llm_off_reason()

    def test_output_is_valid_html(self, monkeypatch):
        """Текст уходит с parse_mode=HTML — сырые скобки сломали бы отправку."""
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        monkeypatch.setattr(bot, '_llm_last_provider_error', 'HTTP 401: <b>oops</b> & co')
        text = bot._llm_off_reason()
        assert '<b>oops</b>' not in text
        assert '&lt;b&gt;' in text

    def test_secrets_never_appear(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
        monkeypatch.setattr(bot, '_llm_last_provider_error',
                            bot._redact_secrets('HTTP 401: key 1234567890:AAHsecretvalue12345678901234'))
        assert 'AAHsecretvalue' not in bot._llm_off_reason()


class TestProbeDoesNotBurnTheLimit:
    """Проверка не должна ломать то, что проверяет.

    Раньше `/llm` всегда слал настоящий запрос. На бесплатном тарифе после
    деплоя это означало: бот уже обогащает новости, админ шлёт /llm, запрос
    упирается в лимит запросов в минуту — и команда отвечает «ответа нет»,
    хотя модель совершенно жива.
    """

    def test_silent_when_everything_is_fine(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', '')
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        assert bot._llm_probe_blocked() == '', 'настоящий запрос должен отправляться'

    def test_rate_limit_answers_without_a_request(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
        monkeypatch.setattr(bot, '_llm_circuit_until', time.monotonic() + 20)
        text = bot._llm_probe_blocked()
        assert text
        assert 'жива' in text, 'админ должен понять, что модель в порядке'
        assert 'не поломка' in text

    def test_exhausted_daily_limit_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', '')
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(bot, '_llm_quota_left', lambda: 0)
        assert 'LLM_DAILY_LIMIT' in bot._llm_probe_blocked()

    def test_disabled_model_explains_itself(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'billing')
        monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
        assert 'средств' in bot._llm_probe_blocked()

    def test_unconfigured_model_is_recognised(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', '')
        assert 'не настроена' in bot._llm_probe_blocked()

    def test_expired_pause_lets_the_request_through(self, monkeypatch):
        """Пауза истекла — значит проверять можно по-настоящему."""
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
        monkeypatch.setattr(bot, '_llm_circuit_until', time.monotonic() - 1)
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        assert bot._llm_probe_blocked() == ''
