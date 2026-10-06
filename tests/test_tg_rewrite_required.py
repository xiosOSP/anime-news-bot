"""Пост чужого Telegram-канала уходит только пересказанным моделью.

«Красная Шапка» ушла в канал слово в слово с поста другого канала (октябрь
2026): модель не ответила, и после 15 минут ожидания бот публиковал копию.
Модераторы попросили, чтобы пересказывала и следила за качеством модель.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', MagicMock(local_topic_filter=False, llm_enabled=True,
                                                   llm_rewrite=True, dedup_final_text=False))
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bot, '_rewrite_deferral_store', None)
    monkeypatch.setattr(bot, '_llm_configured', lambda: True)
    for name in ('_improve_thumb', '_discover_article_video', '_ensure_youtube_cover',
                 '_optimize_news_media', '_image_duplicate', '_video_duplicate'):
        monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    monkeypatch.setattr(bot, '_left_untranslated', lambda news: False)
    monkeypatch.setattr(bot, 'published_texts', None)
    # Ответ модели задаёт каждый тест: что вернул разбор и какие поля проставил.
    reply = {'answer': 'off', 'fields': {}}

    async def enrich(item, side_effects=True):
        item.update(reply['fields'])
        return reply['answer']
    monkeypatch.setattr(bot, '_llm_enrich', enrich)
    bot._test_reply = reply
    yield bot
    del bot._test_reply


def _prepare(env, news, answer, **fields):
    env._test_reply.update(answer=answer, fields=fields)
    return asyncio.run(env._prepare_news_for_send(news, news['source']))


def _post(source='TG: Advance', link='https://t.me/ch/1'):
    return {'title': 'Следствие ведёт Красная Шапка', 'summary': 'В январе смотрим аниме.',
            'source': source, 'link': link, 'lang': 'ru'}


def test_silent_model_makes_the_post_wait(env):
    assert _prepare(env, _post(), 'off') == 'deferred'


def test_after_the_wait_the_copy_is_dropped_not_published(env, monkeypatch):
    monkeypatch.setattr(env, 'TG_REWRITE_MAX_ATTEMPTS', 2)
    news = _post()
    assert _prepare(env, news, 'off') == 'deferred'
    assert _prepare(env, news, 'off') == 'deferred'
    assert _prepare(env, news, 'off') == 'skipped_filter'


def test_rejected_rewrite_is_not_replaced_by_the_copy(env):
    assert _prepare(env, _post(), 'ok', _editorial_rejection='unsupported_names') == 'skipped_filter'
    assert _prepare(env, _post(link='https://t.me/ch/2'), 'ok',
                    _llm_judge_status='rejected') == 'skipped_filter'


def test_rewritten_post_goes_out(env):
    assert _prepare(env, _post(), 'ok', _llm_text='Анонсировано аниме о Красной Шапочке.') is None


@pytest.mark.parametrize('source, settings_patch, configured', [
    ('Crunchyroll', {}, True),                  # сайт, а не чужой Telegram-пост
    ('TG: Advance', {'llm_rewrite': False}, True),   # переписывание выключено владельцем
    ('TG: Advance', {}, False),                 # модели нет — канал живёт без неё
])
def test_rule_only_where_a_rewrite_is_possible(env, monkeypatch, source, settings_patch, configured):
    for key, value in settings_patch.items():
        setattr(env.settings, key, value)
    monkeypatch.setattr(env, '_llm_configured', lambda: configured)
    assert _prepare(env, _post(source=source), 'off') is None


def test_admin_edit_counts_as_a_rewrite(env):
    assert _prepare(env, _post(), 'off', _edited_text='Текст админа') is None
