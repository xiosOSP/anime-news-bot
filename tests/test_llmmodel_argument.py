"""Аргумент /llmmodel: сброс со знаком препинания и чужое слово вместо модели.

На проде у бота оказалась модель с названием «сброс.». Подсказка в самом
отчёте выглядит как «сбросить к переменным окружения — /llmmodel сброс», и
точка в конце фразы уезжает в команду вместе со словом. Слово не совпало со
списком, ушло в ветку «имя модели» и сохранилось.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def store(monkeypatch, tmp_path):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, '_llm_reset_provider_state', lambda reason: None)
    monkeypatch.setattr(bot, '_audit_update', lambda *a, **k: None)
    monkeypatch.setattr(bot, '_llm_model_view', lambda: 'вид')
    monkeypatch.setattr(bot, '_llm_model_menu', lambda: None)
    return settings


async def _run(args):
    reply = AsyncMock()
    await bot.llmmodel_command(NS(message=NS(reply_text=reply), effective_user=NS(id=1)),
                               NS(args=args))
    return reply.await_args.args[0]


class TestResetSurvivesPunctuation:
    @pytest.mark.parametrize('word', ['сброс', 'сброс.', 'сброс!', '«сброс»', 'Сброс,',
                                      'reset.', 'сбросить'])
    @pytest.mark.asyncio
    async def test_the_reset_word_is_recognised(self, store, word):
        store.llm_model_override = 'openai/gpt-oss-120b'
        store.llm_primary_slot = 'fallback'
        await _run([word])
        assert store.llm_model_override == ''
        assert store.llm_primary_slot == ''

    @pytest.mark.asyncio
    async def test_a_two_word_reset_still_works(self, store):
        store.llm_model_override = 'openai/gpt-oss-120b'
        await _run(['по', 'умолчанию'])
        assert store.llm_model_override == ''


class TestAWordIsNotAModelName:
    @pytest.mark.parametrize('arg', ['мгте', 'какая-то модель', 'привет', 'сброс модели'])
    @pytest.mark.asyncio
    async def test_a_typo_is_refused_instead_of_saved(self, store, arg):
        """Опечатка молча становилась моделью, и каждый запрос уходил в 404.

        Хуже того, /llm показывал её как «выбрано вручную» — будто так и
        задумано, и владелец искал поломку где угодно, кроме этого места.
        """
        store.llm_model_override = 'openai/gpt-oss-120b'
        report = await _run(arg.split())
        assert store.llm_model_override == 'openai/gpt-oss-120b', 'опечатка сохранилась'
        assert '❌' in report and 'сброс' in report

    @pytest.mark.parametrize('model', [
        'openai/gpt-oss-120b', 'mistral-small-latest', 'gemini-2.5-flash',
        'deepseek/deepseek-v4-flash-free', 'google/gemma-4-31b-it:free',
        'meta/llama-3.3-70b-instruct',
    ])
    @pytest.mark.asyncio
    async def test_real_model_names_are_accepted(self, store, model):
        """Проверка обязана пропускать все формы имён из пресетов."""
        await _run([model])
        assert store.llm_model_override == model


class TestAStoredTypoDoesNotBreakEveryRequest:
    def test_an_impossible_override_is_ignored(self, monkeypatch, tmp_path):
        """Настройка переживает перезапуск.

        Один раз сохранённое «сброс.» иначе ломало бы каждый запрос до тех
        пор, пока владелец не догадается его сбросить, — а догадаться не по
        чему: отчёт показывает это как осознанный выбор.
        """
        settings = bot.BotSettings(tmp_path / 'settings.json')
        monkeypatch.setattr(bot, 'settings', settings)
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.groq.com/openai/v1')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'openai/gpt-oss-120b')
        settings.llm_model_override = 'сброс.'
        assert bot._llm_slot_config('primary')[2] == 'openai/gpt-oss-120b'

    def test_a_real_override_is_still_applied(self, monkeypatch, tmp_path):
        settings = bot.BotSettings(tmp_path / 'settings.json')
        monkeypatch.setattr(bot, 'settings', settings)
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.groq.com/openai/v1')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'openai/gpt-oss-120b')
        settings.llm_model_override = 'openai/gpt-oss-20b'
        assert bot._llm_slot_config('primary')[2] == 'openai/gpt-oss-20b'
