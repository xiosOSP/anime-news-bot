"""LLM_EXTRA_PARAMS одна на всех, а провайдеров трое.

Параметр вроде reasoning_effort нужен одному провайдеру и ломает запрос у
другого. Раньше повтор после 400 снимал только строгий JSON, и такой отказ
выглядел неустранимым: провайдер жив, ключ верный, а бот его списывал.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv('LLM_EXTRA_PARAMS', '{"reasoning_effort":"none"}')
    for name, value in (
        ('LLM_BASE_URL', 'https://one.test/v1'), ('LLM_API_KEY', 'key-one-aaaa'),
        ('LLM_MODEL', 'model-one'), ('LLM_MODEL_ALTERNATES', ()),
        ('LLM_FALLBACK_API_KEY', ''), ('LLM_FAST_API_KEY', ''),
        ('_llm_extra_ok', {}), ('_llm_extra_override', {}), ('_llm_candidate', ()),
        ('_llm_using_fallback', False),
        ('LLM_MIN_INTERVAL', 0), ('_llm_last_call', 0),
        ('_llm_lock', asyncio.Lock()), ('_llm_json_mode', False), ('_llm_fail_streak', 0),
        ('_llm_disabled_runtime', False), ('_llm_disabled_reason', ''),
        ('settings', bot.BotSettings(tmp_path / 's.json')),
        ('_queue_admin_alert', lambda _m: None),
        ('_llm_circuit_until', 0.0), ('_llm_circuit_level', 0), ('_llm_last_failure', {}),
        ('_llm_last_provider_error', ''),
    ):
        monkeypatch.setattr(bot, name, value)
    return monkeypatch


def _reply(status, text='{}'):
    r = MagicMock(status_code=status, text=text, headers={})
    r.json.return_value = {'choices': [{'message': {'content': 'готово'}}]}
    return r


def test_extra_params_are_sent_by_default():
    assert bot._llm_extra_params('primary') == {'reasoning_effort': 'none'}


def test_provider_that_refused_them_stops_getting_them(_env):
    """Отказ запоминается по провайдеру, а не глобально.

    Иначе один капризный роутер лишил бы параметра того, кому он нужен.
    """
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs.get('json') or {})
        return _reply(400, 'unknown parameter reasoning_effort') if len(sent) == 1 else _reply(200)

    _env.setattr(bot.requests, 'post', post)
    assert asyncio.run(bot._llm_call([{'role': 'user', 'content': 'hi'}], 100)) == 'готово'
    assert len(sent) == 2, 'повтора без лишних параметров не было'
    assert 'reasoning_effort' in sent[0]
    assert 'reasoning_effort' not in sent[1], 'повторили с тем же параметром'


def test_refusal_is_remembered_for_next_calls(_env):
    """Иначе бот наступал бы на те же грабли каждым запросом."""
    _env.setattr(bot.requests, 'post', lambda *a, **k: _reply(400, 'bad param'))
    bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
    assert bot._llm_extra_params('primary') == {}


def test_other_providers_keep_their_params(_env):
    """Отказ одного не должен отнимать параметр у остальных."""
    bot._llm_extra_ok['primary'] = False
    assert bot._llm_extra_params('primary') == {}
    assert bot._llm_extra_params('fallback') == {'reasoning_effort': 'none'}


def test_no_endless_retry_when_params_are_not_the_cause(_env):
    """Если дело не в параметрах, второй повтор ничего не даст."""
    calls = []

    def post(*a, **k):
        calls.append(1)
        return _reply(400, 'что-то другое')

    _env.setattr(bot.requests, 'post', post)
    assert asyncio.run(bot._llm_call([{'role': 'user', 'content': 'hi'}], 100)) is None
    assert len(calls) == 2, f'повторов должно быть ровно два, было {len(calls)}'


def test_provider_name_that_contradicts_the_address_is_flagged(_env):
    """LLM_PROVIDER даёт только умолчания, а адрес может вести к другому сервису.

    Живая конфигурация: LLM_PROVIDER=mistral при LLM_BASE_URL от orcarouter.
    Работает, но в отчётах написан один провайдер, а запросы уходят другому —
    и разбирать поломку становится не по чему.
    """
    _env.setattr(bot, 'LLM_PROVIDER', 'mistral')
    _env.setattr(bot, 'LLM_BASE_URL', 'https://api.orcarouter.ai/v1')
    preset_url = bot.LLM_PRESETS['mistral'][0]
    assert preset_url.rstrip('/') != bot._llm_slot_env('primary')[0].rstrip('/')


GROQ_REFUSAL = ('{"error":{"message":"`reasoning_effort` must be one of `low`, `medium`, or `high`",'
                '"type":"invalid_request_error"}}')


def test_provider_named_values_replace_the_param(_env):
    """Пересказ случая (октябрь 2026): «none» писали под Mistral, Groq его не
    принимает. Без параметра gpt-oss рассуждал в полную силу, съедал лимит
    токенов и отдавал пустой ответ. Берём самое лёгкое из разрешённых."""
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs.get('json') or {})
        return _reply(400, GROQ_REFUSAL) if len(sent) == 1 else _reply(200)

    _env.setattr(bot, '_llm_json_mode', True)
    _env.setattr(bot.requests, 'post', post)
    assert asyncio.run(bot._llm_call([{'role': 'user', 'content': 'hi'}], 100)) == 'готово'
    assert [p.get('reasoning_effort') for p in sent] == ['none', 'low']
    assert bot._llm_json_mode is True, 'строгий JSON сняли по чужой вине'
    assert bot._llm_extra_params('primary') == {'reasoning_effort': 'low'}
    assert bot._llm_extra_params('fallback') == {'reasoning_effort': 'none'}


def test_allowed_value_already_sent_is_not_a_loop(_env):
    bot._llm_extra_override['primary'] = {'reasoning_effort': 'low'}
    assert not bot._llm_extra_fix_from_error('primary', GROQ_REFUSAL)


def test_refusal_about_other_param_is_not_ours(_env):
    body = '{"error":{"message":"`service_tier` must be one of `auto` or `flex`"}}'
    assert not bot._llm_extra_fix_from_error('primary', body)
    assert bot._llm_extra_override == {}


def test_lightest_value_is_chosen(_env):
    body = '{"error":{"message":"`reasoning_effort` must be one of `high`, `medium`, `minimal`"}}'
    assert bot._llm_extra_fix_from_error('primary', body)
    assert bot._llm_extra_params('primary') == {'reasoning_effort': 'minimal'}


def test_empty_answer_names_finish_reason(_env):
    r = _reply(200)
    r.json.return_value = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]}
    _env.setattr(bot.requests, 'post', lambda *a, **k: r)
    assert bot._llm_request([{'role': 'user', 'content': 'hi'}], 100) is None
    assert 'finish_reason=length' in bot._llm_last_failure['detail']


def test_pause_alert_names_the_real_last_error(_env):
    """В сообщении о паузе стоял давний отказ про reasoning_effort, хотя пять
    ошибок подряд были пустыми ответами."""
    alerts = []
    _env.setattr(bot, '_queue_admin_alert', alerts.append)
    _env.setattr(bot, '_llm_last_provider_error', 'HTTP 400: reasoning_effort must be one of')
    _env.setattr(bot, '_llm_fail_streak', bot.LLM_FAIL_PAUSE_AFTER)
    bot._llm_note_failure('bad_body', 'ValueError: empty or non-text completion (finish_reason=length)')
    assert not bot._llm_can_call()
    text = alerts[-1]
    assert 'finish_reason=length' in text
    assert text.index('finish_reason=length') < text.index('reasoning_effort')


def test_provider_reset_forgets_adjusted_values(_env):
    """Подобранное значение относилось к прежнему провайдеру."""
    bot._llm_extra_override['primary'] = {'reasoning_effort': 'low'}
    bot._llm_reset_provider_state('смена провайдера')
    assert bot._llm_extra_params('primary') == {'reasoning_effort': 'none'}
