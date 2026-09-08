"""The optional chat model has its own transport, durable quota and failure budget."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading

import httpx
import pytest

import moderation_llm as module
from moderation_llm import ChatModelClient


MESSAGES = [{'role': 'user', 'content': 'Check this message.'}]


def completion(text='{"violation":false}', tokens=25):
    return httpx.Response(200, json={
        'choices': [{'message': {'content': text}}], 'usage': {'total_tokens': tokens},
    })


def make_client(tmp_path, handler=None, **kwargs):
    return ChatModelClient(
        base_url='https://moderation.example/v1', api_key='chat-only-secret',
        model='chat-only-model', state_path=tmp_path / 'chat-quota.json',
        min_interval=0, transport=httpx.MockTransport(handler or (lambda _: completion())),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_chat_credentials_and_model_never_borrow_news_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'news-secret')
    monkeypatch.setenv('LLM_MODEL', 'news-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'news-openai-secret')
    news_state = tmp_path / 'news-quota.json'
    news_state.write_text('{"requests":79,"tokens":900}', encoding='utf-8')
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == 'https://moderation.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer chat-only-secret'
        assert json.loads(request.content)['model'] == 'chat-only-model'
        assert b'news-secret' not in request.content
        return completion()

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES) == '{"violation":false}'
    assert len(requests) == 1
    assert client.snapshot()['requests'] == 1
    assert client.snapshot()['tokens'] == 25
    assert news_state.read_text(encoding='utf-8') == '{"requests":79,"tokens":900}'
    client.api_key = ''
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'not_configured'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_json_fallback_reserves_every_http_attempt_before_sending(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        state = json.loads((tmp_path / 'chat-quota.json').read_text(encoding='utf-8'))
        assert state['requests'] == len(bodies)
        if len(bodies) == 1:
            assert body['response_format'] == {'type': 'json_object'}
            return httpx.Response(400, text='response_format is not supported')
        assert 'response_format' not in body
        return completion(tokens=11)

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES) == '{"violation":false}'
    reserved = len(json.dumps(MESSAGES, ensure_ascii=False).encode()) // 3 + 200
    assert client.snapshot()['requests'] == 2
    assert client.snapshot()['tokens'] == reserved + 11
    assert await client.complete([{'role': 'user', 'content': 'Next message'}])
    assert len(bodies) == 3 and 'response_format' not in bodies[2]


@pytest.mark.asyncio
async def test_json_fallback_cannot_exceed_request_limit(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(422, text='unsupported response_format')

    client = make_client(tmp_path, handler, daily_limit=1)
    assert await client.complete(MESSAGES) is None
    assert len(requests) == 1
    assert client.snapshot()['requests'] == 1
    assert client.last_error == 'quota'


@pytest.mark.asyncio
async def test_json_fallback_cannot_exceed_token_budget(tmp_path):
    reserved = len(json.dumps(MESSAGES, ensure_ascii=False).encode()) // 3 + 200
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, text='unsupported response_format')

    client = make_client(tmp_path, handler, token_budget=reserved)
    assert await client.complete(MESSAGES) is None
    assert len(requests) == 1
    assert client.snapshot()['tokens'] == reserved
    assert client.last_error == 'token_budget'


@pytest.mark.asyncio
async def test_quota_persists_across_restart_and_resets_only_for_new_utc_day(tmp_path):
    client = make_client(tmp_path, daily_limit=1)
    assert await client.complete(MESSAGES)
    restarted = make_client(tmp_path, daily_limit=1)
    assert await restarted.complete(MESSAGES) is None
    assert restarted.last_error == 'quota'
    state = json.loads((tmp_path / 'chat-quota.json').read_text(encoding='utf-8'))
    state['day'] = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    (tmp_path / 'chat-quota.json').write_text(json.dumps(state), encoding='utf-8')
    next_day = make_client(tmp_path, daily_limit=1)
    assert await next_day.complete(MESSAGES)
    assert next_day.snapshot()['requests'] == 1


@pytest.mark.asyncio
async def test_cache_uses_context_and_output_limit_without_spending_again(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return completion(text=str(len(requests)))

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES, max_tokens=200) == '1'
    assert await client.complete(MESSAGES, max_tokens=200) == '1'
    assert len(requests) == 1
    assert await client.complete(MESSAGES, max_tokens=64) == '2'
    assert await client.complete(MESSAGES + [{'role': 'user', 'content': 'More context'}]) == '3'
    assert client.snapshot()['requests'] == 3
    client._cache = type(client._cache)((key, (0, row[1])) for key, row in client._cache.items())
    assert await client.complete(MESSAGES, max_tokens=200) == '4'


@pytest.mark.asyncio
async def test_cache_does_not_grow_without_bound(tmp_path, monkeypatch):
    client = make_client(tmp_path, daily_limit=5000, token_budget=0)
    # Persistence has dedicated tests; cache eviction must not depend on 514
    # filesystem writes racing platform scanners on Windows.
    monkeypatch.setattr(client, '_save', lambda: None)
    for number in range(257):
        assert await client.complete([{'role': 'user', 'content': str(number)}]), (number, client.snapshot())
    assert len(client._cache) == 256


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error,cooldown', [(429, 'rate_limit', 120), (401, 'auth', 3600), (403, 'auth', 3600)])
async def test_rate_limit_and_auth_open_circuit_without_hidden_retries(tmp_path, status, error, cooldown):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '120'})

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES) is None
    assert await client.complete(MESSAGES) is None
    assert len(calls) == 1
    state = client.snapshot()
    assert state['requests'] == 1 and state['tokens'] > 0
    assert state['error'] == error
    assert cooldown - 1 <= state['cooldown_sec'] <= cooldown


@pytest.mark.asyncio
async def test_network_errors_keep_reservation_and_back_off_then_recover(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= 2:
            raise httpx.ConnectError('connection failed', request=request)
        return completion()

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'network'
    assert 59 <= client.snapshot()['cooldown_sec'] <= 60
    client._cooldown = 0
    assert await client.complete(MESSAGES) is None
    assert 119 <= client.snapshot()['cooldown_sec'] <= 120
    client._cooldown = 0
    assert await client.complete(MESSAGES)
    assert client._failures == 0 and client.last_error == ''
    assert client.snapshot()['requests'] == 3


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_queue_or_double_spend(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return completion()

    client = make_client(tmp_path, handler)
    first = asyncio.create_task(client.complete(MESSAGES))
    await entered.wait()
    assert await client.complete([{'role': 'user', 'content': 'Second'}]) is None
    assert client.snapshot()['requests'] == 1 and client.snapshot()['busy']
    release.set()
    assert await first
    assert client.snapshot()['busy'] is False


@pytest.mark.asyncio
async def test_cancelled_http_keeps_charge_and_releases_busy_state(tmp_path):
    entered, exited = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    client = make_client(tmp_path, handler)
    task = asyncio.create_task(client.complete(MESSAGES))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
    assert client.snapshot()['busy'] is False
    assert client.snapshot()['requests'] == 1
    assert client.snapshot()['tokens'] > 0
    assert make_client(tmp_path).snapshot()['requests'] == 1


@pytest.mark.asyncio
async def test_cancelled_reservation_does_not_send_after_background_save_finishes(tmp_path, monkeypatch):
    entered, release, saved = threading.Event(), threading.Event(), threading.Event()
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion())
    save = client._save

    def slow_save():
        entered.set()
        try:
            assert release.wait(5)
            save()
        finally:
            saved.set()

    monkeypatch.setattr(client, '_save', slow_save)
    task = asyncio.create_task(client.complete(MESSAGES))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._busy is False and calls == []
    finally:
        release.set()
    assert await asyncio.to_thread(saved.wait, 3)
    assert calls == []
    assert json.loads((tmp_path / 'chat-quota.json').read_text(encoding='utf-8'))['requests'] == 1


@pytest.mark.asyncio
async def test_total_timeout_is_shared_by_json_fallback_attempts(tmp_path):
    requests = []
    ended = asyncio.Event()

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            await asyncio.sleep(0.6)
            return httpx.Response(400, text='response_format unsupported')
        try:
            await asyncio.sleep(0.6)
            return completion()
        finally:
            ended.set()

    client = make_client(tmp_path, handler)
    client.timeout = 1.0  # Each response fits alone, both cannot share this deadline.
    assert await client.complete(MESSAGES) is None
    assert len(requests) == 2 and ended.is_set()
    assert client.last_error == 'timeout'
    assert client.snapshot()['requests'] == 2 and not client.snapshot()['busy']


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.chunks = 0

    async def __aiter__(self):
        for _ in range(100):
            self.chunks += 1
            yield b'x' * 8192

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_cancellation_while_streaming_closes_response(tmp_path):
    entered = asyncio.Event()

    class SlowBody(_ChunkedBody):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b'unreachable'

    body = SlowBody()
    client = make_client(tmp_path, lambda _: httpx.Response(200, stream=body))
    task = asyncio.create_task(client.complete(MESSAGES))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and not client.snapshot()['busy']


@pytest.mark.asyncio
async def test_oversized_response_stops_reading_and_closes_stream(tmp_path):
    body = _ChunkedBody()
    client = make_client(tmp_path, lambda _: httpx.Response(200, stream=body))
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'response_too_large'
    assert body.closed and body.chunks == 17
    assert client.snapshot()['requests'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['{broken', '[]', '{}', 'null',
    '{"day":"2026-09-08","requests":true,"tokens":0}',
    '{"day":"2026-09-08","requests":-1,"tokens":0}',
    '{"day":"2026-09-08","requests":"1","tokens":0}',
    '{"day":"2026-09-08","requests":0,"tokens":1.5}',
    '{"day":"not-a-date","requests":0,"tokens":0}',
])
async def test_corrupt_storage_fails_closed_without_sending(tmp_path, content):
    (tmp_path / 'chat-quota.json').write_text(content, encoding='utf-8')
    requests = []
    client = make_client(tmp_path, lambda request: requests.append(request) or completion())
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'storage'
    assert requests == []
    assert (tmp_path / 'chat-quota.json').read_text(encoding='utf-8') == content


@pytest.mark.asyncio
async def test_storage_fault_cannot_be_cleared_by_configuration_status(tmp_path):
    (tmp_path / 'chat-quota.json').write_text('broken', encoding='utf-8')
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion())
    client.api_key = ''
    assert await client.complete(MESSAGES) is None
    client.api_key = 'chat-only-secret'
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'storage' and calls == []


@pytest.mark.asyncio
async def test_failed_atomic_reservation_prevents_http_and_rolls_back(tmp_path, monkeypatch):
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion())

    def cannot_replace(*args):
        raise OSError('disk full')

    monkeypatch.setattr(module.os, 'replace', cannot_replace)
    assert await client.complete(MESSAGES) is None
    assert calls == [] and client.last_error == 'storage'
    assert client.snapshot()['requests'] == 0 and client.snapshot()['tokens'] == 0
    assert not list(tmp_path.glob('.moderation-llm-*'))


@pytest.mark.asyncio
async def test_reconciliation_failure_preserves_durable_conservative_reservation(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    save = client._save
    calls = 0

    def save_then_fail():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('disk full')
        save()

    monkeypatch.setattr(client, '_save', save_then_fail)
    assert await client.complete(MESSAGES)
    state = json.loads((tmp_path / 'chat-quota.json').read_text(encoding='utf-8'))
    assert state['requests'] == 1 and state['tokens'] > 25
    assert client.snapshot()['tokens'] == state['tokens'] and client.last_error == 'storage'
    assert await client.complete([{'role': 'user', 'content': 'Another'}]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
async def test_redirect_is_not_followed_and_never_sends_key_to_new_host(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://attacker.example/collect'})

    client = make_client(tmp_path, handler)
    assert await client.complete(MESSAGES) is None
    assert len(calls) == 1 and calls[0].url.host == 'moderation.example'
    assert client.last_error == f'http_{status}'
    assert 'chat-only-secret' not in json.dumps(client.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://[broken', 'https://example.com:invalid', 'https://user:secret@example.com',
                                 'ftp://example.com', 'https://example.com/v1?key=value'])
async def test_invalid_configuration_is_nonfatal_and_does_not_request(tmp_path, url):
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion())
    client.base_url = url
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'not_configured' and not client.snapshot()['configured']
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,error', [
    ({'choices': []}, 'bad_response'),
    ({'choices': [{'message': {'content': None}}]}, 'empty_response'),
    ({'choices': [{'message': {'content': '  '}}]}, 'empty_response'),
    ({'choices': [{'message': {'content': ['not text']}}]}, 'empty_response'),
    ({'unexpected': 'shape'}, 'bad_response'),
])
async def test_invalid_completion_is_not_cached_and_retains_attempt_charge(tmp_path, payload, error):
    client = make_client(tmp_path, lambda _: httpx.Response(200, json=payload))
    assert await client.complete(MESSAGES) is None
    assert client.last_error == error and not client._cache
    assert client.snapshot()['requests'] == 1 and client.snapshot()['tokens'] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('usage', [None, True, -1, '5'])
async def test_invalid_usage_never_refunds_reserved_tokens(tmp_path, usage):
    client = make_client(tmp_path, lambda _: completion(tokens=usage))
    assert await client.complete(MESSAGES)
    assert client.snapshot()['tokens'] > 25


@pytest.mark.parametrize('value,expected', [('120', 120), ('-5', 1), ('999999', 86400),
                                         ('nan', 60), ('inf', 60), ('bad', 60), (None, 60)])
def test_retry_after_is_finite_and_bounded(value, expected):
    assert ChatModelClient._retry_after(value) == expected


@pytest.mark.asyncio
async def test_disabled_quota_and_oversized_input_never_send_or_create_state(tmp_path):
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion(), daily_limit=0)
    assert await client.complete(MESSAGES) is None
    assert client.last_error == 'quota' and calls == []
    client.daily_limit = 10
    assert await client.complete([{'role': 'user', 'content': 'x' * 20001}]) is None
    assert client.last_error == 'input_too_large' and calls == []
    assert client.snapshot()['requests'] == 0
    assert not (tmp_path / 'chat-quota.json').exists()


@pytest.mark.asyncio
async def test_minimum_interval_drops_new_calls_but_serves_cache(tmp_path):
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or completion())
    client.min_interval = 60
    assert await client.complete(MESSAGES)
    assert await client.complete(MESSAGES)
    assert await client.complete([{'role': 'user', 'content': 'Different'}]) is None
    assert len(calls) == 1 and client.snapshot()['requests'] == 1


@pytest.mark.asyncio
async def test_server_errors_are_counted_once_and_backoff_is_capped(tmp_path):
    calls = []
    client = make_client(tmp_path, lambda request: calls.append(request) or httpx.Response(503))
    for _ in range(8):
        client._cooldown = 0
        assert await client.complete(MESSAGES) is None
    assert len(calls) == 8 and client.snapshot()['requests'] == 8
    assert 899 <= client.snapshot()['cooldown_sec'] <= 900
    assert client.last_error == 'http_503'
