"""Дополнительные бесплатные провайдеры по ключу <ИМЯ>_API_KEY.

Трёх слотов не хватало: бесплатные пулы упираются в лимит почти
одновременно, и пост из Telegram-канала уходил копией без пересказа.
Ключ в переменной — и провайдер встаёт в цепочку запасных.
"""
import pytest

import anime_news_bot as bot

GROQ_URL, GROQ_MODEL = bot.LLM_PRESETS['groq']


@pytest.fixture
def slots(monkeypatch):
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.mistral.ai/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'main-key')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'mistral-small-latest')
    for name in ('FALLBACK', 'FAST'):
        monkeypatch.setattr(bot, f'LLM_{name}_BASE_URL', '')
        monkeypatch.setattr(bot, f'LLM_{name}_API_KEY', '')
        monkeypatch.setattr(bot, f'LLM_{name}_MODEL', '')
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ())
    monkeypatch.setattr(bot, 'llm_key_health', None)

    def use(env):
        extra = bot._llm_extra_slots(env)
        monkeypatch.setattr(bot, 'LLM_EXTRA_SLOTS', extra)
        monkeypatch.setattr(bot, 'LLM_SLOTS', ('primary', 'fallback', 'fast') + tuple(extra))
        return extra
    return use


def test_key_in_the_environment_adds_a_provider(slots):
    extra = slots({'groq': ('gk', ''), 'gemini': ('', '')})
    assert extra == {'extra_groq': (GROQ_URL.rstrip('/'), 'gk', GROQ_MODEL)}
    assert bot._llm_slot_env('extra_groq') == (GROQ_URL.rstrip('/'), 'gk', GROQ_MODEL)


def test_model_can_be_changed_by_variable(slots):
    assert slots({'groq': ('gk', 'llama-4-scout')})['extra_groq'][2] == 'llama-4-scout'


def test_same_provider_and_key_as_the_main_slot_is_not_added_twice(slots):
    # Те же лимиты под другим именем — не запасной.
    assert slots({'mistral': ('main-key', '')}) == {}
    assert 'extra_mistral' in slots({'mistral': ('другой-ключ', '')})


def test_extra_providers_join_the_failover_chain_after_the_main_one(slots):
    slots({'groq': ('gk', ''), 'cerebras': ('ck', '')})
    chain = bot._llm_all_candidates()
    assert chain[0] == ('primary', 'mistral-small-latest')
    assert ('extra_groq', GROQ_MODEL) in chain and ('extra_cerebras', bot.LLM_PRESETS['cerebras'][1]) in chain
    assert bot._llm_backup_slot() == 'extra_groq'


def test_extra_slot_has_a_readable_name_for_the_owner():
    # /llm показывает провайдера и переменную, а не служебный идентификатор.
    assert bot._llm_extra_label('extra_openrouter') == 'Openrouter (OPENROUTER_API_KEY)'


def test_every_extra_provider_has_a_preset():
    assert set(bot._LLM_EXTRA_ENV) <= set(bot.LLM_PRESETS)


def test_daily_limits_grow_with_each_configured_provider(slots, monkeypatch):
    # Лимит «40 вызовов» из инструкции под один Groq останавливал модель к
    # середине дня, сколько бы запасных ключей ни было.
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 40)
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 180000)
    slots({})
    assert (bot._llm_daily_limit(), bot._llm_token_budget()) == (40, 180000)
    slots({'groq': ('gk', ''), 'gemini': ('gm', '')})
    assert (bot._llm_daily_limit(), bot._llm_token_budget()) == (120, 540000)


def test_quota_counts_against_the_grown_limit(slots, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 40)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    slots({'groq': ('gk', '')})
    assert bot._llm_quota_left() == 80


def test_no_token_budget_stays_unlimited(slots, monkeypatch):
    monkeypatch.setattr(bot, 'LLM_DAILY_TOKEN_BUDGET', 0)
    slots({'groq': ('gk', '')})
    assert bot._llm_token_budget() == 0
