"""Chat failover uses fake credentials and MockTransport only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import anime_news_bot as bot
from moderation_llm import ChatModelClient


MESSAGES = [{'role': 'user', 'content': 'Обычный разговор об аниме'}]
BENIGN = '{"violation":false,"category":"","severity":0,"evidence":""}'


@pytest.fixture
def pair(tmp_path, monkeypatch):
    calls = []

    def make(slot):
        def handler(request):
            calls.append(slot)
            assert request.url.host == slot + '.example'
            assert request.headers['Authorization'] == 'Bearer test-' + slot
            assert json.loads(request.content)['model'] == slot + '-model'
            return httpx.Response(200, json={
                'choices': [{'message': {'content': BENIGN}}], 'usage': {'total_tokens': 30}})
        return ChatModelClient(base_url=f'https://{slot}.example/v1', api_key='test-' + slot,
                               model=slot + '-model', state_path=tmp_path / (slot + '.json'),
                               min_interval=0, transport=httpx.MockTransport(handler))

    primary, fallback = make('primary'), make('fallback')
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, '_get_moderation_llm_client', lambda: primary)
    monkeypatch.setattr(bot, '_get_moderation_llm_fallback_client', lambda: fallback)
    monkeypatch.setattr(bot, '_moderation_llm_last_slot', 'primary')
    monkeypatch.setattr(bot, '_llm_can_call', lambda: pytest.fail('news quota consulted'))
    return primary, fallback, calls


@pytest.mark.asyncio
async def test_benign_primary_never_requests_second_opinion(pair):
    primary, fallback, calls = pair
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    assert calls == ['primary']
    assert primary.snapshot()['requests'] == 1
    assert fallback.snapshot()['requests'] == 0


@pytest.mark.parametrize('status', [401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_provider_failure_uses_reserve_in_same_call(pair, status):
    primary, fallback, calls = pair
    primary._transport = httpx.MockTransport(lambda _: httpx.Response(status))
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    assert calls == ['fallback']
    assert bot._moderation_llm_last_slot == 'fallback'
    assert primary.snapshot()['requests'] == fallback.snapshot()['requests'] == 1
    assert 'Резерв чата: fallback-model' in bot._moderation_llm_status()


@pytest.mark.asyncio
async def test_network_failure_and_cooldown_do_not_block_reserve(pair):
    primary, _, calls = pair
    attempts = []

    def fail(request):
        attempts.append(1)
        raise httpx.ConnectError('simulated', request=request)

    primary._transport = httpx.MockTransport(fail)
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    assert await bot._llm_call([{'role': 'user', 'content': 'Другой текст'}], task='moderation') == BENIGN
    assert len(attempts) == 1
    assert calls == ['fallback', 'fallback']


@pytest.mark.asyncio
async def test_primary_timeout_reaches_reserve(pair, monkeypatch):
    primary, _, calls = pair
    original_timeout = asyncio.timeout
    # Exercise the real timeout handler without a multi-second test delay.
    # Короткий таймаут — только первому (основному) запросу. Раньше 0.01 с
    # получал и резервный: на медленной машине CI его запрос с записью
    # состояния на диск не укладывался в 10 мс, и тест падал через раз.
    budgets = iter([.01])
    monkeypatch.setattr('moderation_llm.asyncio.timeout',
                        lambda seconds: original_timeout(next(budgets, seconds)))

    async def slow(_):
        await asyncio.sleep(10)

    primary._transport = httpx.MockTransport(slow)
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    assert primary.last_error == 'timeout'
    assert calls == ['fallback']


@pytest.mark.parametrize('reason', ['quota', 'tokens', 'storage', 'unconfigured'])
@pytest.mark.asyncio
async def test_primary_unavailable_budget_allows_full_classifier(pair, reason):
    primary, fallback, calls = pair
    if reason == 'quota':
        primary.daily_limit = 0
    elif reason == 'tokens':
        primary.token_budget = 1
    elif reason == 'storage':
        primary._storage_error = True
    else:
        primary.api_key = ''
    assert bot._moderation_llm_ready()
    assert bot._moderation_llm_budget_left() >= fallback.daily_limit
    assert (await bot._moderation_classify(42, 'Обычное сообщение'))['violation'] is False
    assert calls == ['fallback']


@pytest.mark.asyncio
async def test_all_exhausted_means_unknown_not_a_violation(pair):
    primary, fallback, calls = pair
    primary.daily_limit = fallback.daily_limit = 0
    assert bot._moderation_llm_budget_left() == 0
    assert await bot._moderation_classify(42, 'Обычное сообщение') is None
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_moderation_does_not_use_either_provider(pair, monkeypatch):
    _, _, calls = pair
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    assert await bot._llm_call(MESSAGES, task='moderation') is None
    assert calls == []


@pytest.mark.asyncio
async def test_cancellation_does_not_launch_backup(pair):
    primary, _, calls = pair

    async def cancel(_):
        raise asyncio.CancelledError()

    primary._transport = httpx.MockTransport(cancel)
    with pytest.raises(asyncio.CancelledError):
        await bot._llm_call(MESSAGES, task='moderation')
    assert calls == []


@pytest.mark.asyncio
async def test_primary_recovers_after_cooldown(pair):
    primary, _, calls = pair
    healthy = primary._transport
    primary._transport = httpx.MockTransport(lambda _: httpx.Response(429))
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    primary._transport, primary._cooldown = healthy, 0
    assert await bot._llm_call(MESSAGES, task='moderation') == BENIGN
    assert calls == ['fallback', 'primary']
    assert bot._moderation_llm_last_slot == 'primary'


@pytest.mark.asyncio
async def test_ping_reports_actual_reserve_model(pair, monkeypatch):
    primary, _, _ = pair
    primary.daily_limit = 0
    monkeypatch.setattr(bot, 'is_admin', lambda _: True)
    message = SimpleNamespace(reply_text=AsyncMock())
    await bot.modllmping_command(
        SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=[]))
    text = message.reply_text.await_args.args[0]
    assert 'fallback-model' in text and '(резерв)' in text and 'отвечает' in text


@pytest.mark.asyncio
async def test_explicit_reserve_probe_leaves_healthy_primary_alone(pair, monkeypatch):
    primary, fallback, calls = pair
    monkeypatch.setattr(bot, 'is_admin', lambda _: True)
    message = SimpleNamespace(reply_text=AsyncMock())
    await bot.modllmping_command(
        SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=['fallback']))
    assert calls == ['fallback']
    assert primary.snapshot()['requests'] == 0
    assert fallback.snapshot()['requests'] == 1
    assert 'fallback-model' in message.reply_text.await_args.args[0]


def test_fallback_configuration_and_quota_survive_recreation(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    for name, value in {
        'MODERATION_LLM_FALLBACK_BASE_URL': 'https://fallback.example/v1',
        'MODERATION_LLM_FALLBACK_MODEL': 'fallback-model',
        'MODERATION_LLM_FALLBACK_API_KEY': 'test-fallback-secret',
        'LLM_API_KEY': 'news-key', 'MODERATION_LLM_API_KEY': 'primary-key',
    }.items():
        monkeypatch.setattr(bot, name, value)
    client = bot._get_moderation_llm_fallback_client()
    assert client.api_key == 'test-fallback-secret'
    assert client.state_path.name == 'moderation_llm_fallback_budget.json'
    assert client._reserve(123)
    monkeypatch.setattr(bot, '_moderation_llm_fallback_client', None)
    restored = bot._get_moderation_llm_fallback_client()
    assert restored.snapshot()['requests'] == 1
    assert restored.snapshot()['tokens'] == 123
    assert 'test-fallback-secret' not in bot._redact_secrets('key test-fallback-secret')
    monkeypatch.setattr(bot, 'MODERATION_LLM_FALLBACK_API_KEY', '')
    assert not bot._get_moderation_llm_fallback_client().configured
