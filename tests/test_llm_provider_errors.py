"""Ошибки провайдера модели: что повторять, а что нет.

Реальный случай: роутер отвечал 402 «недостаточно средств», а бот считал это
обычной ошибкой и повторял запрос каждым циклом. В логе копились одинаковые
строки на китайском, провайдер получал лишние запросы, а понять, что именно
чинить, было нельзя без чтения тела ответа.
"""
from unittest.mock import MagicMock

import time

import pytest

import anime_news_bot as bot


class TestFatalClassification:
    @pytest.mark.parametrize('status, body, expected', [
        (402, '{"type":"insufficient_balance"}', 'billing'),
        (402, 'TeamoRouter 钱包余额不足', 'billing'),
        (200, '{"error":"insufficient_balance"}', 'billing'),
        (404, '{"error":{"message":"model_not_found"}}', 'model'),
        (200, 'model not found', 'model'),
    ])
    def test_permanent_errors_are_recognised(self, status, body, expected):
        result = bot._llm_fatal_reason(status, body)
        assert result and result['reason'] == expected

    @pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
    def test_transient_errors_stay_retryable(self, status):
        assert bot._llm_fatal_reason(status, 'temporary trouble') is None

    def test_empty_body_does_not_crash(self):
        assert bot._llm_fatal_reason(500, '') is None
        assert bot._llm_fatal_reason(500, None) is None

    def test_admin_hint_names_what_to_fix(self):
        billing = bot._llm_fatal_reason(402, 'insufficient_balance')
        assert 'LLM_BASE_URL' in billing['admin']
        model = bot._llm_fatal_reason(404, 'model_not_found')
        assert 'LLM_MODEL' in model['admin']


class TestFatalErrorStopsRetrying:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(bot, '_llm_disabled_reason', '')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'key')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://provider.test/v1')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'deepseek-v4-pro-free')
        yield

    def _response(self, status, body):
        r = MagicMock()
        r.status_code = status
        r.text = body
        r.json.return_value = {}
        return r

    def test_payment_required_disables_the_model(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bot.requests, 'post',
                            lambda *a, **k: (calls.append(1),
                                             self._response(402, 'insufficient_balance'))[1])
        alerts = []
        monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)

        assert bot._llm_request([{'role': 'user', 'content': 'hi'}], 100) is None
        assert bot._llm_disabled_runtime is True
        assert bot._llm_disabled_reason == 'billing'
        assert alerts and 'баланс' in alerts[0].lower()

        # Страж перед запросом обязан закрыть путь к провайдеру: именно он
        # решает, идти ли за ответом. Раньше billing-ошибка сюда не доходила,
        # и каждый цикл повторял заведомо провальный запрос.
        assert bot._llm_active() is False, 'после неустранимой ошибки запросы должны прекратиться'

    def test_billing_block_is_not_lifted_by_cooldown(self, monkeypatch):
        """Временную паузу circuit снимает сам, а нехватку денег — нет."""
        monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
        monkeypatch.setattr(bot, '_llm_disabled_reason', 'billing')
        monkeypatch.setattr(bot, '_llm_circuit_until', 0.0)
        assert bot._llm_active() is False

    def test_unknown_model_disables_and_names_the_variable(self, monkeypatch):
        monkeypatch.setattr(bot.requests, 'post',
                            lambda *a, **k: self._response(404, 'model_not_found'))
        alerts = []
        monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)

        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)

        assert bot._llm_disabled_reason == 'model'
        assert alerts and 'LLM_MODEL' in alerts[0]

    def test_server_error_keeps_retrying(self, monkeypatch):
        monkeypatch.setattr(bot.requests, 'post',
                            lambda *a, **k: self._response(503, 'unavailable'))
        monkeypatch.setattr(bot, '_queue_admin_alert', lambda _m: None)

        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)

        assert bot._llm_disabled_runtime is False, 'временная ошибка не должна выключать модель'


class TestRateLimitRespectsProvider:
    """429 — это просьба сбавить темп, а не отказ провайдера.

    Раньше слой модели игнорировал `Retry-After`: бот увеличивал счётчик
    неудач и шёл дальше, следующий запрос упирался в тот же лимит, и после
    пяти таких подряд модель выключалась по счётчику ошибок — хотя достаточно
    было подождать несколько секунд. На бесплатных тарифах с жёстким лимитом
    запросов в минуту это происходило постоянно.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'key')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://provider.test/v1')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'deepseek-v4-pro-free')
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(bot, '_llm_disabled_reason', '')
        monkeypatch.setattr(bot, '_llm_circuit_until', 0.0)
        monkeypatch.setattr(bot, '_llm_fail_streak', 0)
        yield

    def _rate_limited(self, monkeypatch, headers):
        response = MagicMock()
        response.status_code = 429
        response.text = 'rate limited'
        response.headers = headers
        response.json.return_value = {}
        monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: response)

    def test_retry_after_sets_exactly_that_pause(self, monkeypatch):
        self._rate_limited(monkeypatch, {'Retry-After': '20'})
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        left = bot._llm_circuit_until - time.monotonic()
        assert 18 <= left <= 21, f'ждём {left:.0f} с вместо запрошенных 20'
        assert bot._llm_disabled_reason == 'circuit'

    def test_honoured_rate_limit_is_not_a_failure(self, monkeypatch):
        """Мы выполнили просьбу — наказывать модель длинной паузой не за что."""
        self._rate_limited(monkeypatch, {'Retry-After': '1'})
        for _ in range(bot.LLM_FAIL_PAUSE_AFTER + 2):
            bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_fail_streak == 0

    def test_missing_header_keeps_the_old_protection(self, monkeypatch):
        """Без заголовка мы не знаем, сколько ждать — защита по счётчику нужна."""
        self._rate_limited(monkeypatch, {})
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_fail_streak == 1
        assert bot._llm_circuit_until == 0.0

    def test_pause_expires_on_its_own(self, monkeypatch):
        self._rate_limited(monkeypatch, {'Retry-After': '1'})
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_disabled_runtime is True
        # Пауза короткая и должна сняться сама, без вмешательства.
        # Дневной бюджет подменяем: в тесте его хранилище не поднято, и без
        # этого _llm_active() вернёт False по совсем другой причине.
        monkeypatch.setattr(bot, '_llm_circuit_until', time.monotonic() - 0.1)
        monkeypatch.setattr(bot, '_llm_quota_left', lambda: 100)
        monkeypatch.setattr(bot, '_llm_budget_admits', lambda *a, **k: True, raising=False)
        bot._llm_active()
        assert bot._llm_disabled_reason == '', 'пауза не снялась после истечения'
        assert bot._llm_disabled_runtime is False

    def test_absurd_retry_after_is_capped(self, monkeypatch):
        self._rate_limited(monkeypatch, {'Retry-After': '99999'})
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        left = bot._llm_circuit_until - time.monotonic()
        assert left <= bot.LLM_CIRCUIT_MAX_SEC + 1, 'провайдер не должен усыпить модель навсегда'

    def test_broken_header_does_not_crash(self, monkeypatch):
        self._rate_limited(monkeypatch, {'Retry-After': 'скоро'})
        assert bot._llm_request([{'role': 'user', 'content': 'hi'}], 100) is None
        assert bot._llm_fail_streak == 1, 'непонятный заголовок = как будто его нет'


class TestProviderErrorIsVisible:
    """Наша трактовка может не совпадать с реальностью.

    У бесплатных роутеров 401 нередко означает исчерпанную квоту, а не плохой
    ключ. Раньше наружу шло только наше «ключ отклонён — проверь LLM_API_KEY»,
    и понять настоящую причину было нельзя.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-secret-value-1234567890')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://provider.test/v1')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'model')
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(bot, '_llm_last_provider_error', '')
        yield

    def _reject(self, monkeypatch, status, body):
        response = MagicMock()
        response.status_code = status
        response.text = body
        response.headers = {}
        response.json.return_value = {}
        monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: response)

    def test_quota_message_is_preserved(self, monkeypatch):
        self._reject(monkeypatch, 401, '{"error":{"message":"Insufficient quota for free tier"}}')
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert 'quota' in bot._llm_last_provider_error.lower()
        assert '401' in bot._llm_last_provider_error

    def test_key_is_never_echoed_back(self, monkeypatch):
        self._reject(monkeypatch, 401,
                     '{"error":"Invalid API key: sk-secret-value-1234567890"}')
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert 'sk-secret-value-1234567890' not in bot._llm_last_provider_error
        assert 'скрыто' in bot._llm_last_provider_error

    def test_billing_error_is_preserved_too(self, monkeypatch):
        self._reject(monkeypatch, 402, 'insufficient_balance: пополните счёт')
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert 'insufficient_balance' in bot._llm_last_provider_error

    def test_long_body_is_trimmed(self, monkeypatch):
        self._reject(monkeypatch, 401, 'x' * 5000)
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert len(bot._llm_last_provider_error) <= 320

    def test_empty_body_still_records_status(self, monkeypatch):
        self._reject(monkeypatch, 403, '')
        bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
        assert bot._llm_last_provider_error == 'HTTP 403'
