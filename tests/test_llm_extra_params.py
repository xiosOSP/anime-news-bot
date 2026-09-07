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
        ('_llm_extra_ok', {}), ('_llm_candidate', ()), ('_llm_using_fallback', False),
        ('LLM_MIN_INTERVAL', 0), ('_llm_last_call', 0),
        ('_llm_lock', asyncio.Lock()), ('_llm_json_mode', False), ('_llm_fail_streak', 0),
        ('_llm_disabled_runtime', False), ('_llm_disabled_reason', ''),
        ('settings', bot.BotSettings(tmp_path / 's.json')),
        ('_queue_admin_alert', lambda _m: None),
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
