"""Бот подбирает темп под провайдера сам.

Единая пауза для всех означала, что предел провайдера бот узнаёт единственным
способом — упираясь в него: разогнался, получил 429, потерял вызов, и по
кругу. Для бесплатных тарифов, где темп у каждого свой и меняется в течение
дня, это и есть причина того, что модели «то есть, то нет».
"""
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, '_llm_pace', {})
    monkeypatch.setattr(bot, '_llm_wait_hint_sec', 0.0)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 1.0)
    monkeypatch.setattr(bot, 'LLM_PACE_MAX_SEC', 60.0)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    return monkeypatch


def test_default_pace_is_the_configured_one():
    assert bot._llm_pace_for('primary') == 1.0


def test_rate_limit_slows_down_that_provider_only():
    """Лимит одного провайдера ничего не говорит о темпе другого."""
    bot._llm_pace_slower('primary')
    assert bot._llm_pace_for('primary') > 1.0
    assert bot._llm_pace_for('fallback') == 1.0


def test_slowdown_doubles_rather_than_creeps():
    """Мелкий шаг означал бы череду отказов, пока пауза доползёт до нужной."""
    first = bot._llm_pace_slower('primary')
    second = bot._llm_pace_slower('primary')
    assert second >= first * 2


def test_provider_hint_is_respected_as_a_floor():
    """Провайдер знает свой лимит лучше нас."""
    assert bot._llm_pace_slower('primary', retry_after=30) >= 30


def test_pace_never_grows_without_bound():
    """Пауза в час — это уже не темп, а остановка."""
    for _ in range(20):
        bot._llm_pace_slower('primary')
    assert bot._llm_pace_for('primary') <= bot.LLM_PACE_MAX_SEC


def test_success_returns_the_pace_back_gradually():
    """Разогнаться обратно можно долго, а упереться — один раз и сразу.

    Поэтому шаг вниз мельче шага вверх: после одного успеха темп не должен
    прыгать обратно к исходному.
    """
    bot._llm_pace_slower('primary', retry_after=10)
    after_limit = bot._llm_pace_for('primary')
    bot._llm_pace_faster('primary')
    once = bot._llm_pace_for('primary')
    assert once < after_limit
    assert once > bot.LLM_MIN_INTERVAL, 'вернулись к исходному с одного успеха'


def test_pace_eventually_returns_to_normal():
    """Иначе один всплеск ограничивал бы бота до перезапуска."""
    bot._llm_pace_slower('primary', retry_after=3)
    for _ in range(60):
        bot._llm_pace_faster('primary')
    assert bot._llm_pace_for('primary') == bot.LLM_MIN_INTERVAL


# ---------- повтор, когда идти некуда ----------

def _reply(status, text='{}', headers=None):
    r = MagicMock(status_code=status, text=text, headers=headers or {})
    r.json.return_value = {'choices': [{'message': {'content': 'готово'}}]}
    return r


@pytest.fixture
def alone(_clean, monkeypatch):
    """Один провайдер: уходить некуда, и в этом весь смысл проверки."""
    for name, value in (
        ('LLM_BASE_URL', 'https://one.test/v1'), ('LLM_API_KEY', 'key-one-aaaa'),
        ('LLM_MODEL', 'model-one'), ('LLM_MODEL_ALTERNATES', ()),
        ('LLM_FALLBACK_API_KEY', ''), ('LLM_FAST_API_KEY', ''),
        ('LLM_MIN_INTERVAL', 0.0), ('LLM_INLINE_RETRY_MAX_SEC', 20.0),
        ('_llm_using_fallback', False), ('_llm_candidate', ()),
        ('_llm_tried_candidates', set()), ('_llm_disabled_runtime', False),
        ('_llm_disabled_reason', ''), ('_llm_fail_streak', 0),
        ('_llm_circuit_until', 0.0), ('_llm_last_call', 0.0),
        ('_llm_json_mode', False), ('_queue_admin_alert', lambda _m: None),
    ):
        monkeypatch.setattr(bot, name, value)
    bot._llm_last_failure.clear()
    return monkeypatch


@pytest.mark.asyncio
async def test_single_provider_is_waited_out_instead_of_dropped(alone, monkeypatch):
    """Отдать пост без перевода хуже, чем подождать пять секунд.

    Когда провайдер один, 429 означал потерянный вызов: пост уходил без
    перевода и тегов. Провайдер при этом сам сказал, сколько ждать.
    """
    calls = []
    slept = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _reply(429, 'Rate limit exceeded', {'Retry-After': '5'})
        return _reply(200)

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(bot.requests, 'post', post)
    monkeypatch.setattr(bot.asyncio, 'sleep', fake_sleep)
    result = await bot._llm_call([{'role': 'user', 'content': 'hi'}], 100)
    assert result == 'готово', 'вызов потерян там, где его можно было спасти'
    assert len(calls) == 2, 'повтора не было'
    assert 5 in slept, 'ждали не столько, сколько попросил провайдер'


@pytest.mark.asyncio
async def test_long_wait_is_not_held_inline(alone, monkeypatch):
    """Держать цикл сбора десять минут ради одного поста нельзя."""
    calls = []

    def post(*a, **k):
        calls.append(1)
        return _reply(429, 'Rate limit exceeded', {'Retry-After': '600'})

    monkeypatch.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([{'role': 'user', 'content': 'hi'}], 100) is None
    assert len(calls) == 1, 'ждали дольше разумного вместо того, чтобы отступить'
