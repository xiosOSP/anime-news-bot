"""Пост ждёт живую модель, а не выходит сырым.

Отказ бесплатных тарифов длится минуты, а пост остаётся в канале навсегда.
Раньше при молчащей модели новость публиковалась без перевода, тегов и
отсева — и именно это владелец описывал словами «бот становится максимально
примитивным».
"""
import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, '_llm_deferred', {})
    monkeypatch.setattr(bot, 'LLM_DEFER_MAX_ATTEMPTS', 3)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://one.test/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'key-one-aaaa')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-one')
    monkeypatch.setattr(bot, '_llm_candidate', ())
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    return monkeypatch


def _news(link='https://x.test/1'):
    return {'title': 'Аниме анонсировано', 'summary': '', 'link': link, 'source': 'X'}


@pytest.mark.asyncio
async def test_configured_but_silent_model_defers_the_post(_clean):
    """Модель настроена и включена — значит её отсутствие временно."""
    _clean.setattr(bot, '_llm_disabled_runtime', True)
    _clean.setattr(bot, '_llm_disabled_reason', 'circuit')
    assert await bot._llm_enrich(_news()) == 'defer'


@pytest.mark.asyncio
async def test_unconfigured_model_does_not_defer(_clean):
    """Без модели работать нормально — так и задумано, ждать нечего."""
    _clean.setattr(bot, 'LLM_API_KEY', '')
    assert await bot._llm_enrich(_news()) == 'off'


@pytest.mark.asyncio
async def test_model_switched_off_does_not_defer(_clean):
    """Выключил осознанно — значит посты должны идти без неё."""
    bot.settings.llm_enabled = False
    assert await bot._llm_enrich(_news()) == 'off'


@pytest.mark.asyncio
async def test_deferral_is_not_endless(_clean):
    """Свежая новость без тегов лучше идеальной, но вчерашней."""
    _clean.setattr(bot, '_llm_disabled_runtime', True)
    _clean.setattr(bot, '_llm_disabled_reason', 'circuit')
    news = _news()
    results = [await bot._llm_enrich(dict(news)) for _ in range(5)]
    assert results[:3] == ['defer'] * 3
    assert results[3:] == ['off'] * 2, 'новость откладывается бесконечно'


def test_attempts_are_counted_per_news_not_globally(_clean):
    """Иначе одна залежавшаяся новость лишала бы отсрочки все остальные."""
    for _ in range(3):
        bot._llm_defer_news(_news('https://x.test/1'))
    assert bot._llm_defer_news(_news('https://x.test/1')) is False
    assert bot._llm_defer_news(_news('https://x.test/2')) is True


def test_counter_is_bounded(_clean):
    """Структура без потолка в долгоживущем процессе однажды выстреливает."""
    for i in range(2000):
        bot._llm_defer_news(_news(f'https://x.test/{i}'))
    assert len(bot._llm_deferred) <= 501


def test_every_send_path_releases_a_deferred_link():
    """reject хоронит ссылку — новость не вернулась бы никогда.

    Здесь же мы всего лишь ждём модель, и второй шанс обязателен. Путей
    отправки два — в ветку и в канал; забыть про один из них означает терять
    половину отложенных новостей молча.
    """
    import re
    from pathlib import Path

    source = Path(bot.__file__).read_text(encoding='utf-8')
    branches = re.findall(r"if skip == 'deferred':(.{0,300})", source, re.S)
    assert len(branches) == 2, f'путей отправки должно быть два, найдено {len(branches)}'
    for branch in branches:
        assert 'sent_links.release' in branch, 'отложка помечает ссылку отказом'
