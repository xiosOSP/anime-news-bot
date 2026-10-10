"""Провайдер не обслуживает страну сервера: перейти дальше, а не застрять на нём.

Живой случай (октябрь 2026): Gemini на хостинге в Нидерландах отвечает
«User location is not supported» — адреса хостинга числятся за Россией.
Раньше бот принимал этот 400 за «не понял строгий JSON», выключал JSON-режим
для всех моделей и оставался на отказывающем провайдере.
"""
import json
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot

GEMINI_REFUSAL = json.dumps([{'error': {'code': 400, 'status': 'FAILED_PRECONDITION',
                                        'message': 'User location is not supported for the API use.'}}])


def _reply(status, text):
    r = MagicMock(status_code=status, text=text, headers={})
    r.json.return_value = {'choices': [{'message': {'content': 'готово'}}]}
    return r


def test_region_refusal_is_permanent():
    fatal = bot._llm_fatal_reason(400, GEMINI_REFUSAL)
    assert fatal['reason'] == 'region'
    assert not fatal.get('retry_after_sec')


@pytest.mark.parametrize('status,body', [
    (403, '{"error":{"code":"unsupported_country_region_territory"}}'),
    (400, 'This model is not available in your region'),
])
def test_other_providers_wording_is_recognised(status, body):
    assert bot._llm_region_refused(status, body)


def test_rate_limit_and_plain_400_are_not_region():
    assert not bot._llm_region_refused(429, 'Rate limit exceeded')
    assert not bot._llm_region_refused(400, 'response_format is not supported')


def test_successful_answer_quoting_the_words_is_not_region():
    assert not bot._llm_region_refused(200, 'Сервис пишет: user location is not supported')


def test_key_variable_name_for_extra_and_main_slots():
    assert bot._llm_slot_key_env('extra_gemini') == 'GEMINI_API_KEY'
    assert bot._llm_slot_key_env('fallback') == 'LLM_FALLBACK_API_KEY'


@pytest.fixture
def chain(monkeypatch, tmp_path):
    store = bot.LLMKeyHealth(tmp_path / 'health.json')
    for name, value in (
        ('llm_key_health', store),
        ('settings', bot.BotSettings(tmp_path / 's.json')),
        ('LLM_BASE_URL', 'https://gemini.test/v1'), ('LLM_API_KEY', 'gem-key-aaaa'),
        ('LLM_MODEL', 'gemini-3.8-flash'), ('LLM_MODEL_ALTERNATES', ()),
        ('LLM_FALLBACK_BASE_URL', 'https://groq.test/v1'), ('LLM_FALLBACK_API_KEY', 'groq-key-bbbb'),
        ('LLM_FALLBACK_MODEL', 'openai/gpt-oss-120b'), ('LLM_FAST_API_KEY', ''),
        ('LLM_MIN_INTERVAL', 0.0), ('_llm_using_fallback', False), ('_llm_candidate', ()),
        ('_llm_tried_candidates', set()), ('_llm_disabled_runtime', False),
        ('_llm_disabled_reason', ''), ('_llm_fail_streak', 0), ('_llm_circuit_until', 0.0),
        ('_llm_last_call', 0.0), ('_llm_json_mode', True), ('_llm_pace', {}),
        ('_llm_failover_level', 0), ('_llm_failover_alert_key', ''),
        ('_queue_admin_alert', lambda _m: None),
    ):
        monkeypatch.setattr(bot, name, value)
    bot._llm_last_failure.clear()
    return store


@pytest.mark.asyncio
async def test_region_refusal_moves_to_next_provider_and_keeps_json_mode(chain, monkeypatch):
    urls = []

    def post(url, *a, **k):
        urls.append(url)
        if 'gemini' in url:
            return _reply(400, GEMINI_REFUSAL)
        return _reply(200, '')
    monkeypatch.setattr(bot.requests, 'post', post)
    await bot._llm_call([{'role': 'user', 'content': 'hi'}], 100)
    assert bot._llm_json_mode is True, 'строгий JSON выключен для всех из-за чужого отказа'
    assert bot._llm_slot_rejected('primary'), 'отказ по стране не запомнен'
    assert ('primary', 'gemini-3.8-flash') not in bot._llm_candidates()


def test_probe_remembers_region_refusal_and_cleanup_says_remove(chain, monkeypatch):
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: _reply(400, GEMINI_REFUSAL))
    row = bot._llm_probe_slot('primary')
    assert row['region'] and not row['temporary']
    assert bot._llm_slot_rejected('primary')
    plan = bot._llm_cleanup_plan([row])
    assert any('LLM_API_KEY' in item and 'стереть' in item for item in plan['remove'])
    assert not plan['wait']
