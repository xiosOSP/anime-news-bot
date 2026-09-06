"""Почему модель промолчала.

«Провайдер не ответил на запрос» — честная, но бесполезная строка: под ней
пряталось пять поломок, которые чинятся по-разному. Таймаут — это сеть
хостинга, 502 — сторона провайдера, 429 — темп запросов, 401 — ключ. Тесты
сторожат то, что отличает диагностику от отписки: каждая причина названа
своим именем, и старая причина не выдаётся за текущую.
"""
import pytest
import requests

import anime_news_bot as bot


@pytest.fixture(autouse=True, scope='module')
def _globals():
    bot._init_globals()
    return True


class _Reply:
    def __init__(self, status, text='', payload=None):
        self.status_code = status
        self.text = text
        self.headers = {}
        self._payload = payload or {'choices': [{'message': {'content': 'ok'}}]}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://one.example/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'key-primary-aaaaaaaa')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-one')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', '')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
    monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', '')
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_disabled_reason', '')
    monkeypatch.setattr(bot, '_llm_json_mode', False)
    bot._llm_last_failure.clear()
    return True


def _answer(monkeypatch, reply):
    def _post(*a, **k):
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(bot.requests, 'post', _post)


def test_network_failure_points_at_the_host_not_the_provider(monkeypatch):
    """Таймаут — это сеть хостинга. Раньше он не оставлял вообще ничего."""
    _answer(monkeypatch, requests.exceptions.ConnectTimeout('timed out'))
    assert bot._llm_request([{'role': 'user', 'content': 'x'}]) is None
    text = bot._llm_last_failure_text()
    assert 'не дошёл до провайдера' in text
    assert 'ConnectTimeout' in text


def test_server_error_names_the_status(monkeypatch):
    """5xx — сторона провайдера, и код надо видеть."""
    _answer(monkeypatch, _Reply(502, 'Bad Gateway'))
    assert bot._llm_request([{'role': 'user', 'content': 'x'}]) is None
    assert 'HTTP 502' in bot._llm_last_failure_text()


def test_rate_limit_is_not_reported_as_an_outage(monkeypatch):
    """429 — это темп запросов, а не поломка: чинится ожиданием, а не ключом."""
    _answer(monkeypatch, _Reply(429, 'slow down'))
    assert bot._llm_request([{'role': 'user', 'content': 'x'}]) is None
    assert 'темп запросов' in bot._llm_last_failure_text()


def test_unreadable_body_is_its_own_diagnosis(monkeypatch):
    """Ответ пришёл, но разобрать нечего — это не «провайдер не ответил»."""
    _answer(monkeypatch, _Reply(200, 'not json', payload={'unexpected': True}))
    assert bot._llm_request([{'role': 'user', 'content': 'x'}]) is None
    assert 'не разобрать' in bot._llm_last_failure_text()


def test_success_clears_the_old_reason(monkeypatch):
    """Иначе /llm неделю показывал бы давно ушедший таймаут как текущую проблему."""
    _answer(monkeypatch, requests.exceptions.ConnectTimeout('timed out'))
    bot._llm_request([{'role': 'user', 'content': 'x'}])
    assert bot._llm_last_failure_text()
    _answer(monkeypatch, _Reply(200))
    assert bot._llm_request([{'role': 'user', 'content': 'x'}]) == 'ok'
    assert bot._llm_last_failure_text() == ''


def test_reason_never_leaks_the_key(monkeypatch):
    """Причина уходит админу в личку, а роутеры цитируют ключ в тексте ошибки."""
    _answer(monkeypatch, _Reply(500, 'rejected key key-primary-aaaaaaaa'))
    bot._llm_request([{'role': 'user', 'content': 'x'}])
    assert 'key-primary-aaaaaaaa' not in bot._llm_last_failure_text()


# ---------- прямая проба провайдера ----------

def test_probe_reports_a_dead_network_without_raising(monkeypatch):
    """Проба обязана вернуть результат, а не упасть: её зовут когда всё плохо."""
    _answer(monkeypatch, requests.exceptions.ConnectionError('no route to host'))
    row = bot._llm_probe_slot('primary')
    assert row['ok'] is False and row['status'] == 0
    assert 'ConnectionError' in row['detail']


def test_probe_reports_the_status_code(monkeypatch):
    _answer(monkeypatch, _Reply(402, 'insufficient balance'))
    row = bot._llm_probe_slot('primary')
    assert row['ok'] is False and row['status'] == 402
    assert 'insufficient balance' in row['detail']


def test_probe_says_ok_when_the_provider_answers(monkeypatch):
    _answer(monkeypatch, _Reply(200))
    assert bot._llm_probe_slot('primary')['ok'] is True


def test_probe_does_not_disable_anything(monkeypatch):
    """Проба диагностирует, а не наказывает: она не должна менять состояние."""
    _answer(monkeypatch, _Reply(401, 'bad key'))
    before = (bot._llm_fail_streak, bot._llm_disabled_runtime, bot._llm_using_fallback)
    bot._llm_probe_slot('primary')
    assert (bot._llm_fail_streak, bot._llm_disabled_runtime, bot._llm_using_fallback) == before


def test_probe_redacts_the_key(monkeypatch):
    """Роутеры возвращают присланный ключ в теле ошибки."""
    _answer(monkeypatch, _Reply(401, 'user not found for key-primary-aaaaaaaa'))
    assert 'key-primary-aaaaaaaa' not in bot._llm_probe_slot('primary')['detail']
