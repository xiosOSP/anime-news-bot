import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

import anime_news_bot as bot


def test_stage4_feature_flags_exist():
    for name in ('circuit_breakers', 'adaptive_retry', 'backpressure',
                 'llm_budget', 'error_fingerprinting'):
        assert name in bot.FEATURE_FLAGS


def test_retry_after_accepts_http_date(monkeypatch):
    monkeypatch.setattr(bot, 'HTTP_RETRY_MAX_DELAY', 30.0)
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=8))
    parsed = bot._parse_retry_after(value)
    assert parsed is not None
    assert 0 < parsed <= 30


def test_adaptive_retry_jitter_is_bounded(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_retry', True)
    monkeypatch.setattr(bot, 'HTTP_RETRY_BACKOFFS', (10.0,))
    monkeypatch.setattr(bot, 'HTTP_RETRY_JITTER_RATIO', 0.2)
    monkeypatch.setattr(bot, 'HTTP_RETRY_MAX_DELAY', 30.0)
    monkeypatch.setattr(bot.random, 'uniform', lambda a, b: b)
    assert bot._adaptive_retry_delay(0) == pytest.approx(12.0)
    monkeypatch.setattr(bot.random, 'uniform', lambda a, b: a)
    assert bot._adaptive_retry_delay(0) == pytest.approx(8.0)


def test_source_circuit_breaker_opens_only_on_hard_failures(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'circuit_breakers', True)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_FAIL_THRESHOLD', 3)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_BASE_SEC', 120)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_MAX_SEC', 600)
    health = bot.SourceHealth(tmp_path / 'source.json')
    for _ in range(5):
        health.record_fail('QuietFeed', '0 posts', hard=False)
    assert health.breaker_remaining('QuietFeed') == 0
    for _ in range(3):
        health.record_fail('BrokenFeed', 'Timeout', hard=True)
    assert health.breaker_remaining('BrokenFeed') > 0
    assert health.allow_request('BrokenFeed') is False


def test_source_half_open_failure_reopens_with_longer_cooldown(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'circuit_breakers', True)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_FAIL_THRESHOLD', 2)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_BASE_SEC', 60)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_MAX_SEC', 600)
    health = bot.SourceHealth(tmp_path / 'source.json')
    health.record_fail('S', 'boom', hard=True)
    health.record_fail('S', 'boom', hard=True)
    first_level = health.info('S')['breaker_level']
    health._entry('S')['breaker_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    health.record_fail('S', 'boom again', hard=True)
    row = health.info('S')
    assert row['breaker_level'] > first_level
    assert health.breaker_remaining('S') > 0


@pytest.mark.asyncio
async def test_collect_skips_source_while_breaker_open(tmp_path, monkeypatch):
    calls = 0
    def collector():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setitem(bot.FEATURE_FLAGS, 'circuit_breakers', True)
    monkeypatch.setattr(bot, 'SOURCE_BREAKER_FAIL_THRESHOLD', 2)
    health = bot.SourceHealth(tmp_path / 'source.json')
    health.record_fail('Dead', 'network', hard=True)
    health.record_fail('Dead', 'network', hard=True)
    monkeypatch.setattr(bot, 'source_health', health)
    monkeypatch.setattr(bot, 'SOURCES', [('Dead', collector)])
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(
        is_source_enabled=lambda _name: True,
        require_image=False,
    ))
    rows, stats_lines, errors = await bot.collect_all_news()
    assert calls == 0
    assert rows == [] and errors == []
    assert any('🧯' in line for line in stats_lines)


def test_error_fingerprint_suppresses_repeats_and_groups_numbers(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'error_fingerprinting', True)
    monkeypatch.setattr(bot, 'ERROR_FINGERPRINT_NOTIFY_EVERY', 3)
    store = bot.ErrorFingerprintStore(tmp_path / 'errors.json')
    first = store.record('source:A', 'HTTP 503 for item 12345')
    second = store.record('source:A', 'HTTP 503 for item 67890')
    third = store.record('source:A', 'HTTP 503 for item 99999')
    assert first['notify'] is True
    assert second['notify'] is False
    assert third['notify'] is True
    assert third['count'] == 3
    assert len(store.snapshot()) == 1


def test_error_fingerprint_resolves_after_success(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'error_fingerprinting', True)
    store = bot.ErrorFingerprintStore(tmp_path / 'errors.json')
    store.record('source:A', 'Timeout')
    store.record('source:A', 'Timeout')
    assert store.resolve_scope('source:A') == 1
    assert store.snapshot() == []


def test_backpressure_caps_only_admission(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'backpressure', True)
    monkeypatch.setattr(bot, 'BACKPRESSURE_SOFT_QUEUE', 10)
    monkeypatch.setattr(bot, 'BACKPRESSURE_HARD_QUEUE', 20)
    monkeypatch.setattr(bot, 'BACKPRESSURE_SOFT_NEW', 5)
    monkeypatch.setattr(bot, 'BACKPRESSURE_HARD_NEW', 2)
    news = [{'_priority_score': 100-i, 'title': str(i)} for i in range(12)]
    kept, deferred, level = bot._backpressure_candidates(news, 22, thread_mode=False)
    assert level == 'hard'
    assert len(kept) == 2 and deferred == 10
    assert news[0] in kept
    # Input list is not mutated/marked sent; deferred candidates can return later.
    assert len(news) == 12


def test_thread_backpressure_does_not_cap_prepared_batch(monkeypatch):
    # Thread mode has no persistent queue to protect. Capping raw candidates
    # before final dedup/filtering can yield deferred>0 while sent==0.
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'backpressure', True)
    monkeypatch.setattr(bot, 'BACKPRESSURE_THREAD_MAX_PER_CYCLE', 3)
    items = [{'title': str(i)} for i in range(8)]
    kept, deferred, level = bot._backpressure_candidates(items, 0, thread_mode=True)
    assert level == 'thread_off'
    assert kept == items
    assert deferred == 0


@pytest.mark.asyncio
async def test_llm_budget_denies_call_before_provider(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.llm_day = bot._local_now().strftime('%Y-%m-%d')
    settings.llm_calls_today = 0
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 50)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_last_call', 0.0)
    budget = bot.LLMBudgetStore(tmp_path / 'budget.json')
    monkeypatch.setattr(bot, 'llm_budget', budget)
    calls = 0
    def request(_messages, _max_tokens):
        nonlocal calls
        calls += 1
        return 'ok'
    monkeypatch.setattr(bot, '_llm_request', request)
    assert await bot._llm_call([], 10) == 'ok'
    assert await bot._llm_call([], 10) is None
    assert calls == 1
    assert budget.snapshot()['denied'] == 1


@pytest.mark.asyncio
async def test_llm_budget_reconciles_provider_usage(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 1000)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_last_call', 0.0)
    budget = bot.LLMBudgetStore(tmp_path / 'budget.json')
    monkeypatch.setattr(bot, 'llm_budget', budget)
    def request(_messages, _max_tokens):
        bot._llm_last_usage_tokens = 12
        return 'ok'
    monkeypatch.setattr(bot, '_llm_request', request)
    assert await bot._llm_call([], 10) == 'ok'
    assert budget.snapshot()['tokens'] == 12


def test_golden_runner_never_counts_case_twice():
    passed, total, failures = bot._run_golden_dataset()
    assert passed <= total
    assert not failures
    assert passed == total

def test_retry_after_jitter_never_shortens_server_delay(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'adaptive_retry', True)
    monkeypatch.setattr(bot, 'HTTP_RETRY_JITTER_RATIO', 0.5)
    monkeypatch.setattr(bot, 'HTTP_RETRY_MAX_DELAY', 30.0)
    monkeypatch.setattr(bot.random, 'uniform', lambda a, b: a)
    assert bot._adaptive_retry_delay(0, retry_after=10.0) == pytest.approx(10.0)

@pytest.mark.asyncio
async def test_llm_budget_is_rechecked_inside_existing_lock(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_budget', True)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 90)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_last_call', 0.0)
    monkeypatch.setattr(bot, 'llm_budget', bot.LLMBudgetStore(tmp_path / 'budget.json'))
    calls = 0
    def request(_messages, _max_tokens):
        nonlocal calls
        calls += 1
        return 'ok'
    monkeypatch.setattr(bot, '_llm_request', request)
    results = await asyncio.gather(*(bot._llm_call([], 10) for _ in range(10)))
    # Estimate for this request is 42 tokens: budget 90 admits exactly two.
    assert calls == 2
    assert sum(x == 'ok' for x in results) == 2
    assert bot.llm_budget.snapshot()['tokens'] == 84

def test_llm_transient_circuit_auto_recovers(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(llm_enabled=True))
    monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
    monkeypatch.setattr(bot, '_llm_disabled_reason', 'circuit')
    monkeypatch.setattr(bot, '_llm_circuit_until', bot.time.monotonic() - 1)
    monkeypatch.setattr(bot, '_llm_fail_streak', 5)
    assert bot._llm_active() is True
    assert bot._llm_disabled_runtime is False
    assert bot._llm_fail_streak == 0
