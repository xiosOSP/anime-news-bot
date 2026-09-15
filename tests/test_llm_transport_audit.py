"""Regressions for transport errors, free refusals and model rotation."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    for name, value in {
        'settings': bot.BotSettings(tmp_path / 'settings.json'),
        'LLM_BASE_URL': 'https://primary.invalid/v1', 'LLM_API_KEY': 'test-key',
        'LLM_MODEL': 'quality', 'LLM_MODEL_ALTERNATES': (),
        'LLM_FALLBACK_API_KEY': '', 'LLM_FAST_API_KEY': '',
        'LLM_DAILY_LIMIT': 20, 'LLM_MIN_INTERVAL': 0,
        'LLM_INLINE_RETRY_MAX_SEC': 0,
        '_llm_lock': asyncio.Lock(), '_llm_candidate': (),
        '_llm_using_fallback': False, '_llm_tried_candidates': set(),
        '_llm_json_mode': True, '_llm_extra_ok': {}, '_llm_fail_streak': 0,
        '_llm_disabled_runtime': False, '_llm_disabled_reason': '',
        '_llm_last_call': 0, '_llm_circuit_until': 0,
        '_llm_primary_retry_at': 0, '_llm_last_failure': {},
        '_llm_last_provider_error': '', '_llm_wait_hint_sec': 0,
        '_llm_failover_level': 0, '_llm_failover_alert_key': '',
        'llm_key_health': None, 'llm_budget': None,
        '_llm_pace_for': lambda slot: 0,
        '_queue_admin_alert': lambda message: None,
        '_llm_route_for': lambda task: None,
    }.items():
        monkeypatch.setattr(bot, name, value)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', False)


def reply(status=200, content='ready', usage=None, error=''):
    data = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        data['usage'] = {'total_tokens': usage}
    response = MagicMock(status_code=status, headers={},
                         text=error or json.dumps(data))
    response.json.return_value = data
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize('phrase', ['model not found', 'model_not_found',
                                   'insufficient balance', 'insufficient_balance'])
async def test_success_content_is_never_classified_as_provider_error(monkeypatch, phrase):
    content = json.dumps({'title': f'The trailer displays {phrase}'})
    response = reply(content=content, usage=7)
    post = MagicMock(return_value=response)
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == content
    assert not bot._llm_disabled_runtime
    assert bot._llm_last_failure == {}
    assert post.call_count == bot.settings.llm_calls_today == 1
    response.close.assert_called_once()


@pytest.mark.asyncio
async def test_http_200_error_envelope_still_disables_invalid_configuration(monkeypatch):
    response = reply(error='{"error":{"message":"model not found"}}')
    response.json.return_value = {'error': {'message': 'model not found'}}
    monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=response))

    assert await bot._llm_call([], 10) is None
    assert bot._llm_disabled_reason == 'model'
    assert bot.settings.llm_calls_today == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 402, 404])
async def test_free_refusal_releases_tokens_before_healthy_fallback(monkeypatch, tmp_path, status):
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://fallback.invalid/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'backup-key')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'backup')
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    # Exactly one estimated request fits. A refusal must release its reservation.
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', bot._estimate_llm_tokens([], 10))
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    post = MagicMock(side_effect=[reply(status, error='refused'), reply(usage=7)])
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    assert post.call_count == 2
    assert bot.settings.llm_calls_today == 1
    assert bot.llm_budget.snapshot()['tokens'] == 7
    assert bot.llm_budget.snapshot()['denied'] == 0


@pytest.mark.asyncio
async def test_rate_limited_model_uses_alternate_without_second_provider(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ('alternate',))
    post = MagicMock(side_effect=[reply(429, error='Model rate limit exceeded'), reply()])
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    assert [call.kwargs['json']['model'] for call in post.call_args_list] == [
        'quality', 'alternate']
    assert bot.settings.llm_calls_today == 2


@pytest.mark.asyncio
async def test_account_quota_still_skips_same_provider_alternates(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ('alternate',))
    post = MagicMock(return_value=reply(429, error='free_rate_limited'))
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) is None
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_unknown_server_failure_retains_conservative_token_reservation(monkeypatch, tmp_path):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 1000)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    monkeypatch.setattr(bot.requests, 'post', MagicMock(return_value=reply(502)))

    assert await bot._llm_call([], 10) is None
    assert bot.llm_budget.snapshot()['tokens'] == bot._estimate_llm_tokens([], 10)


@pytest.mark.asyncio
async def test_timeout_keeps_tokens_when_provider_may_have_generated(monkeypatch, tmp_path):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 1000)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    monkeypatch.setattr(bot.requests, 'post',
                        MagicMock(side_effect=bot.requests.exceptions.ReadTimeout('timed out')))

    assert await bot._llm_call([], 10) is None
    assert bot.llm_budget.snapshot()['tokens'] == bot._estimate_llm_tokens([], 10)


@pytest.fixture
def persisted_rejection(monkeypatch, tmp_path):
    path = tmp_path / 'key-health.json'
    store = bot.LLMKeyHealth(path)
    store.remember_rejected('primary', 'test-key', 401)
    restored = bot.LLMKeyHealth(path)
    monkeypatch.setattr(bot, 'llm_key_health', restored)
    return restored


@pytest.mark.asyncio
async def test_restart_skips_rejected_primary_and_uses_backup(monkeypatch, persisted_rejection):
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://fallback.invalid/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'backup-key')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'backup')
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    post.assert_called_once()
    assert post.call_args.args[0] == 'https://fallback.invalid/v1/chat/completions'
    assert bot.settings.llm_calls_today == 1
    assert persisted_rejection.rejected('primary', 'test-key', 3600)


@pytest.mark.asyncio
async def test_all_rejected_slots_make_no_request(monkeypatch, persisted_rejection):
    for slot, prefix in [('fallback', 'LLM_FALLBACK'), ('fast', 'LLM_FAST')]:
        monkeypatch.setattr(bot, prefix + '_BASE_URL', f'https://{slot}.invalid/v1')
        monkeypatch.setattr(bot, prefix + '_API_KEY', slot + '-key')
        monkeypatch.setattr(bot, prefix + '_MODEL', slot)
        persisted_rejection.remember_rejected(slot, slot + '-key', 401)
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) is None
    post.assert_not_called()
    assert bot.settings.llm_calls_today == 0


@pytest.mark.asyncio
async def test_replacing_rejected_key_allows_request(monkeypatch, persisted_rejection):
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'replacement-key')
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer replacement-key'


@pytest.mark.asyncio
async def test_explicit_probe_can_revive_rejected_primary(monkeypatch, persisted_rejection):
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)

    assert bot._llm_probe_slot('primary')['ok'] is True
    assert persisted_rejection.rejected('primary', 'test-key', 3600) is None
    assert await bot._llm_call([], 10) == 'ready'
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_known_rejected_fast_route_uses_quality_directly(monkeypatch, tmp_path):
    health = bot.LLMKeyHealth(tmp_path / 'health.json')
    health.remember_rejected('fast', 'fast-key', 401)
    monkeypatch.setattr(bot, 'llm_key_health', health)
    monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', 'fast-key')
    monkeypatch.setattr(bot, '_llm_route_for',
                        lambda task: ('https://fast.invalid/v1', 'fast-key', 'fast'))
    post = MagicMock(return_value=reply())
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    post.assert_called_once()
    assert post.call_args.args[0] == 'https://primary.invalid/v1/chat/completions'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 402, 404])
async def test_fast_route_refusal_releases_budget_for_quality(monkeypatch, tmp_path, status):
    monkeypatch.setattr(bot, '_llm_route_for',
                        lambda task: ('https://fast.invalid/v1', 'fast-key', 'fast'))
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', bot._estimate_llm_tokens([], 10))
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    post = MagicMock(side_effect=[reply(status, error='refused'), reply(usage=7)])
    monkeypatch.setattr(bot.requests, 'post', post)

    assert await bot._llm_call([], 10) == 'ready'
    assert bot.settings.llm_calls_today == 1
    assert bot.llm_budget.snapshot()['tokens'] == 7
    assert bot._llm_disabled_reason == ''
    assert bot._llm_using_fallback is False
    assert [call.args[0] for call in post.call_args_list] == [
        'https://fast.invalid/v1/chat/completions',
        'https://primary.invalid/v1/chat/completions']


@pytest.mark.parametrize('ttl', [0.0, 3600.0])
def test_rejected_key_expires_at_exact_ttl(monkeypatch, tmp_path, ttl):
    now = [1000.0]
    monkeypatch.setattr(bot.time, 'time', lambda: now[0])
    health = bot.LLMKeyHealth(tmp_path / 'health.json')
    health.remember_rejected('primary', 'key', 401)
    now[0] += ttl

    assert health.rejected('primary', 'key', ttl=ttl) is None
