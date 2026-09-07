"""Бесплатная квота считается на аккаунт, а не на модель.

Живой отказ: три бесплатные модели одного роутера и Mistral — все четыре
вернули 429. У роутера код был free_rate_limited, то есть «квота аккаунта
исчерпана», а не «эта модель занята». Разница практическая: в первом случае
соседняя модель на том же ключе не поможет никогда.
"""
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot

FREE_LIMIT = ('{"error":{"code":"free_rate_limited","message":"Free model capacity '
              'is limited right now. Retry shortly, or add credits."}}')
MODEL_BUSY = '{"error":{"code":"model_not_found","message":"No available capacity"}}'


@pytest.fixture
def three(monkeypatch):
    for name, value in (
        ('LLM_BASE_URL', 'https://one.test/v1'), ('LLM_API_KEY', 'key-one-aaaa'),
        ('LLM_MODEL', 'deepseek/deepseek-v4-flash-free'),
        ('LLM_MODEL_ALTERNATES', ('qwen/qwen3.8-27b-free', 'tencent/hy3-free')),
        ('LLM_FALLBACK_BASE_URL', 'https://two.test/v1'),
        ('LLM_FALLBACK_API_KEY', 'key-two-bbbb'),
        ('LLM_FALLBACK_MODEL', 'mistral-small-latest'),
        ('LLM_FAST_API_KEY', ''),
        ('_llm_using_fallback', False), ('_llm_candidate', ()),
        ('_llm_tried_candidates', set()), ('_llm_primary_retry_at', 0.0),
        ('_llm_failover_level', 0), ('_llm_failover_alert_key', ''),
        ('_queue_admin_alert', lambda _m: None),
    ):
        monkeypatch.setattr(bot, name, value)
    return monkeypatch


def test_account_wide_limit_is_told_apart_from_a_busy_model():
    """Один и тот же код 429, но лечится по-разному."""
    assert bot._llm_free_quota_exhausted(FREE_LIMIT) is True
    assert bot._llm_free_quota_exhausted(MODEL_BUSY) is False
    assert bot._llm_free_quota_exhausted('') is False


def test_exhausted_quota_skips_the_whole_provider(three, monkeypatch):
    """Соседние модели того же ключа упрутся в тот же потолок.

    Перебирать их — значит бить в исчерпанную квоту ещё дважды и только
    отдалять момент, когда мы дойдём до живого провайдера.
    """
    monkeypatch.setattr(bot, '_llm_last_provider_error', FREE_LIMIT)
    assert bot._llm_try_failover('rate_limit', '', 60.0) is True
    assert bot._llm_current()[2] == 'mistral-small-latest', 'ушли не к другому провайдеру'


def test_busy_model_still_tries_the_neighbour(three, monkeypatch):
    """Обратный случай ломать нельзя: занята модель — сосед помогает."""
    monkeypatch.setattr(bot, '_llm_last_provider_error', MODEL_BUSY)
    assert bot._llm_try_failover('capacity', '', 60.0) is True
    assert bot._llm_current()[2] == 'qwen/qwen3.8-27b-free'


def test_advice_names_the_provider_with_a_shared_limit():
    """Иначе человек будет менять модели по кругу в надежде на любую."""
    results = [
        {'slot': 'primary', 'status': 429, 'detail': FREE_LIMIT},
        {'slot': 'primary', 'status': 429, 'detail': FREE_LIMIT},
        {'slot': 'fallback', 'status': 429, 'detail': 'Rate limit exceeded'},
    ]
    assert bot._llm_shared_free_limit(results)


def test_single_free_refusal_is_not_called_a_shared_limit():
    """Один отказ ещё ничего не доказывает про общий потолок."""
    results = [{'slot': 'primary', 'status': 429, 'detail': FREE_LIMIT}]
    assert bot._llm_shared_free_limit(results) == ''


# ---------- проба не должна сама создавать лимит ----------

@pytest.mark.asyncio
async def test_probe_spaces_requests_to_the_same_provider(three, monkeypatch):
    """Три запроса за полторы секунды — и 429 приходит по нашей же вине.

    Диагностика, создающая ту поломку, о которой докладывает, хуже её
    отсутствия: она уводит в сторону.
    """
    slept: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(bot.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(bot, '_llm_probe_slot',
                        lambda slot, timeout=12.0, model='': {
                            'slot': slot, 'model': model, 'ok': True, 'status': 200,
                            'took': 0.1, 'detail': '', 'suggested': '', 'temporary': False})
    monkeypatch.setattr(bot, 'is_admin', lambda _u: True)

    update = MagicMock()
    update.message.reply_text = _AsyncReturn()
    context = MagicMock(args=[])
    await bot.llmping_command(update, context)

    # Четыре кандидата, из них три у одного провайдера: две паузы между ними.
    assert len(slept) == 2, f'пауз между запросами к одному ключу: {len(slept)}'
    assert all(s >= 1.0 for s in slept)


class _AsyncReturn:
    """reply_text, который возвращает сообщение с работающим edit_text."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args[0] if args else kwargs.get('text'))
        msg = MagicMock()

        async def edit(*a, **k):
            self.calls.append(a[0] if a else k.get('text'))

        msg.edit_text = edit
        return msg
