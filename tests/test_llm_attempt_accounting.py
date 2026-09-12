"""Exercise actual HTTP attempt boundaries with local provider responses."""
import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    for name, value in {
        'settings': bot.BotSettings(tmp_path / 'settings.json'),
        'LLM_BASE_URL': 'https://primary.invalid/v1', 'LLM_API_KEY': 'test',
        'LLM_MODEL': 'quality', 'LLM_MODEL_ALTERNATES': (),
        'LLM_FALLBACK_API_KEY': '', 'LLM_FAST_API_KEY': '',
        'LLM_DAILY_LIMIT': 20, 'LLM_MIN_INTERVAL': 0,
        '_llm_lock': asyncio.Lock(), '_llm_candidate': (),
        '_llm_using_fallback': False, '_llm_tried_candidates': set(),
        '_llm_json_mode': True, '_llm_extra_ok': {}, '_llm_fail_streak': 0,
        '_llm_disabled_runtime': False, '_llm_disabled_reason': '',
        '_llm_last_call': 0, '_llm_circuit_until': 0,
        '_llm_primary_retry_at': 0, '_llm_last_failure': {},
        '_llm_pace_for': lambda slot: 0, '_queue_admin_alert': lambda message: None,
        '_llm_route_for': lambda task: None,
    }.items():
        monkeypatch.setattr(bot, name, value)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', False)


def reply(status=200, content='ready', usage=None):
    r = MagicMock(status_code=status, text='unsupported parameter' if status == 400 else '', headers={})
    r.json.return_value = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        r.json.return_value['usage'] = {'total_tokens': usage}
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,expected_calls,expected_result', [(1, 1, None), (3, 3, 'ready')])
async def test_parameter_negotiation_obeys_call_quota(monkeypatch, limit, expected_calls, expected_result):
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', limit)
    monkeypatch.setenv('LLM_EXTRA_PARAMS', '{"reasoning_effort": "none"}')
    responses = [reply(400), reply(400), reply()]
    post = MagicMock(side_effect=responses)
    monkeypatch.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([], 100) == expected_result
    assert post.call_count == bot.settings.llm_calls_today == expected_calls
    for r in responses[:expected_calls]:
        r.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure, charged', [
    # Ключ отклонён и запрос не дошёл — провайдер не потратил ни одного токена,
    # поэтому на дневном счётчике остаётся только удачная попытка.
    (401, 1),
    (OSError('primary unavailable'), 1),
    # 502 — модель могла отработать, такую попытку списываем.
    (502, 2),
])
async def test_failover_serves_current_request(monkeypatch, failure, charged):
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'fallback-test')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://fallback.invalid/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'backup')
    post = MagicMock(side_effect=[reply(failure) if isinstance(failure, int) else failure, reply()])
    monkeypatch.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([], 100) == 'ready'
    assert [c.args[0] for c in post.call_args_list] == [
        'https://primary.invalid/v1/chat/completions',
        'https://fallback.invalid/v1/chat/completions']
    assert bot.settings.llm_calls_today == charged


@pytest.mark.asyncio
async def test_budget_blocks_fast_route_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, '_llm_route_for', lambda task: ('https://fast.invalid/v1', 'test', 'fast'))
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 50)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    post = MagicMock(return_value=reply(500))
    monkeypatch.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([], 10) is None
    assert post.call_count == 1
    assert bot.llm_budget.snapshot()['tokens'] == bot._estimate_llm_tokens([], 10)
    assert bot.llm_budget.snapshot()['denied'] == 1


@pytest.mark.asyncio
async def test_each_attempt_reconciles_own_usage(monkeypatch, tmp_path):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 1000)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    post = MagicMock(side_effect=[reply(400), reply(200, usage=7)])
    monkeypatch.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([], 10) == 'ready'
    assert bot.llm_budget.snapshot()['tokens'] == bot._estimate_llm_tokens([], 10) + 7


@pytest.mark.parametrize('content', [None, '', '  ', [], {'text': 'wrong schema'}, 42])
def test_invalid_completion_is_failure_and_response_is_closed(monkeypatch, content):
    r = reply(content=content)
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **kw: r)
    assert bot._llm_request([], 10) is None
    assert bot._llm_fail_streak == 1
    assert bot._llm_last_failure['kind'] == 'bad_body'
    r.close.assert_called_once()


def test_extra_parameters_cannot_replace_request_or_enable_streaming(monkeypatch):
    monkeypatch.setenv('LLM_EXTRA_PARAMS', json.dumps({
        'model': 'wrong', 'messages': [], 'max_tokens': 99999, 'stream': True}))
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)
    messages = [{'role': 'user', 'content': 'translate'}]
    assert bot._llm_request(messages, 10) == 'ready'
    payload = post.call_args.kwargs['json']
    assert (payload['model'], payload['messages'], payload['max_tokens'], payload['stream']) == (
        'quality', messages, 10, False)


@pytest.mark.asyncio
async def test_waiting_call_rechecks_circuit(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_FAIL_PAUSE_AFTER', 1)
    post = MagicMock(return_value=reply(500))
    monkeypatch.setattr(bot.requests, 'post', post)
    assert await asyncio.gather(bot._llm_call([], 10), bot._llm_call([], 10)) == [None, None]
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_http_worker_keeps_lock_and_accounts_usage(monkeypatch, tmp_path):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 1000)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    entered, release = threading.Event(), threading.Event()

    def post(*a, **kw):
        entered.set()
        assert release.wait(5)
        return reply(usage=11)

    monkeypatch.setattr(bot.requests, 'post', post)
    task = asyncio.create_task(bot._llm_call([], 10))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert bot._llm_lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bot._llm_lock.locked()
    assert bot.llm_budget.snapshot()['tokens'] == 11


class TestFailedAttemptsDoNotEatTheDay:
    """Дневной лимит тратится на работу, а не на ошибки настройки.

    На проде это выглядело так: шесть провайдеров с отклонёнными ключами,
    `/llm` показывает «Вызовов сегодня: 30 из 30», постов ноль. Лимит считается
    от токенов (пакетный разбор — около 5 000), а 401 не стоит ни одного.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize('failure', [401, 403, 404])
    async def test_a_rejected_key_costs_nothing(self, monkeypatch, failure):
        monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=reply(failure)))
        assert await bot._llm_call([], 100) is None
        assert bot.settings.llm_calls_today == 0

    @pytest.mark.asyncio
    async def test_an_unreachable_provider_costs_nothing(self, monkeypatch):
        monkeypatch.setattr(bot.requests, 'post',
                            MagicMock(side_effect=OSError('connection refused')))
        assert await bot._llm_call([], 100) is None
        assert bot.settings.llm_calls_today == 0

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_still_charged(self, monkeypatch):
        """429 провайдер посчитал. Сделать вид, что запроса не было, —
        значит пойти на новый 429 и получить его же."""
        monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=reply(429)))
        assert await bot._llm_call([], 100) is None
        assert bot.settings.llm_calls_today == 1

    @pytest.mark.asyncio
    async def test_a_working_call_is_charged(self, monkeypatch):
        monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=reply()))
        assert await bot._llm_call([], 100) == 'ready'
        assert bot.settings.llm_calls_today == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_is_still_charged(self, monkeypatch):
        """Пустой или неразбираемый ответ — модель отработала и сожгла токены.

        Проверяется сразу после возвращённого 401: возврат не имеет права
        распространиться на следующую попытку, которая до модели дошла.
        """
        post = MagicMock(side_effect=[reply(401), reply(200, content='')])
        monkeypatch.setattr(bot.requests, 'post', post)
        await bot._llm_call([], 100)
        bot._llm_disabled_runtime = False
        bot._llm_disabled_reason = ''
        bot._llm_using_fallback = False
        bot._llm_candidate = ()
        assert await bot._llm_call([], 100) is None
        assert bot.settings.llm_calls_today == 1

    def test_a_refund_never_crosses_into_another_day(self, tmp_path):
        """Возврат после полуночи не имеет права списать вызов новых суток.

        Счётчик обнуляется при первом вызове новых суток. Возврат, опоздавший
        на смену дня, списал бы чужой вызов — и лимит нового дня начался бы
        с долга.
        """
        store = bot.BotSettings(tmp_path / 'settings.json')
        store.increment_llm_call('2026-09-12')
        assert store.refund_llm_call('2026-09-13') == 0
        assert store.llm_calls_today == 1

    @pytest.mark.asyncio
    async def test_the_counter_never_goes_below_zero(self, monkeypatch):
        """Возврат не имеет права увести счётчик в минус: отрицательный
        остаток дал бы модели лишние вызовы сверх лимита."""
        monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=reply(401)))
        for _ in range(3):
            bot._llm_disabled_runtime = False
            bot._llm_disabled_reason = ''
            await bot._llm_call([], 100)
        bot._llm_refund_call()
        assert bot.settings.llm_calls_today == 0
