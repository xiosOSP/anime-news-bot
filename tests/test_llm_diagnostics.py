"""Диагностика отказов языковой модели должна доходить до админа.

Причина отказа вычисляется в коде давно, но дважды терялась по дороге: при
удачном переключении на запасного провайдера в личку уходил голый код вроде
«(model)», а сообщение о паузе не называло ни провайдера, ни его ответ. Читать
такие сообщения можно было только вместе с исходниками.
"""
import re

import pytest

import anime_news_bot as bot


@pytest.fixture
def alerts(monkeypatch):
    """Перехватывает сообщения админу и сбрасывает состояние переключения."""
    collected: list[str] = []
    monkeypatch.setattr(bot, '_queue_admin_alert', collected.append)
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    monkeypatch.setattr(bot, '_llm_last_provider_error', '')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'key')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://api.mistral.ai/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'mistral-small-latest')
    return collected


def _plain(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


# ---------- классификация отказов ----------

def test_unknown_model_is_fatal_not_retried():
    """404 по модели повтором не лечится — повторять его значит долбить провайдера."""
    fatal = bot._llm_fatal_reason(404, 'model_not_found')
    assert fatal is not None
    assert fatal['reason'] == 'model'


def test_no_balance_is_fatal():
    assert bot._llm_fatal_reason(402, 'insufficient balance')['reason'] == 'billing'


@pytest.mark.parametrize('status,body', [(500, 'oops'), (503, 'overloaded'), (429, 'slow down')])
def test_temporary_errors_are_not_fatal(status, body):
    """Временные ошибки обязаны остаться повторяемыми, иначе теряем провайдера зря."""
    assert bot._llm_fatal_reason(status, body) is None


def test_fatal_reason_names_what_to_fix():
    """В подсказке должно быть имя переменной, а не только констатация отказа."""
    admin = bot._llm_fatal_reason(404, 'model_not_found')['admin']
    assert 'LLM_MODEL' in admin
    assert 'LLM_BASE_URL' in admin


# ---------- сообщение о переключении ----------

def test_failover_alert_carries_the_hint(alerts):
    """Голый код причины бесполезен: в сообщении должно быть, что чинить."""
    fatal = bot._llm_fatal_reason(404, 'model_not_found')
    assert bot._llm_try_failover(fatal['reason'], fatal['admin']) is True
    assert len(alerts) == 1
    text = _plain(alerts[0])
    assert 'mistral-small-latest' in text        # куда переключились
    assert 'LLM_MODEL' in text                   # что править
    assert 'не починится' in text                # что само не пройдёт


def test_failover_alert_quotes_provider_answer(alerts, monkeypatch):
    """Своя трактовка может быть неполной — текст провайдера тоже нужен."""
    monkeypatch.setattr(bot, '_llm_last_provider_error', 'HTTP 404: model does not exist')
    bot._llm_try_failover('model', 'подсказка')
    assert 'model does not exist' in _plain(alerts[0])


def test_failover_happens_only_once(alerts):
    """Второе переключение некуда делать: запасной уже используется."""
    assert bot._llm_try_failover('model', 'подсказка') is True
    assert bot._llm_try_failover('billing', 'другая подсказка') is False
    assert len(alerts) == 1


def test_no_failover_without_configured_fallback(alerts, monkeypatch):
    """Без запасного провайдера переключаться некуда, и молчать об этом нельзя:
    вызывающий код обязан получить False и показать полный текст отключения."""
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
    assert bot._llm_try_failover('model', 'подсказка') is False
    assert alerts == []


def test_alert_escapes_provider_text(alerts, monkeypatch):
    """Ответ провайдера идёт в Telegram с parse_mode=HTML — теги надо экранировать."""
    monkeypatch.setattr(bot, '_llm_last_provider_error', 'HTTP 404: <b>bad</b> & wrong')
    bot._llm_try_failover('model', 'подсказка')
    assert '<b>bad</b>' not in alerts[0]
    assert '&lt;b&gt;' in alerts[0]
