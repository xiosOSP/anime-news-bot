"""Регрессии для ошибок, найденных при полном ревью проекта."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

import anime_news_bot as botmod


@pytest.mark.asyncio
async def test_album_fallback_uses_resolved_direct_video(monkeypatch):
    """После отказа media_group fallback не должен падать с NameError video_media."""
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(
        video_enabled=True, require_image=True,
    ))

    async def resolve_video(_url):
        return b'resolved-video'

    async def resolve_photos(items):
        return list(items)

    monkeypatch.setattr(botmod, '_resolve_video', resolve_video)
    monkeypatch.setattr(botmod, '_resolve_photos_for_album', resolve_photos)

    tg = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=TelegramError('album rejected')),
        send_video=AsyncMock(return_value=object()),
        send_photo=AsyncMock(return_value=object()),
        send_message=AsyncMock(return_value=object()),
    )
    news = {
        'title': 'Test title',
        'summary': 'Summary',
        'source': 'Test',
        'link': 'https://example.com/news',
        'images': ['https://example.com/image.jpg'],
        'video': 'https://example.com/video.mp4',
    }

    assert await botmod._send_post(tg, news, -100, None) is True
    assert tg.send_video.await_args.kwargs['video'] == b'resolved-video'


@pytest.mark.asyncio
async def test_release_removes_fuzzy_claim(tmp_path):
    """Неудачная отправка не должна блокировать повтор похожим заголовком на 48 ч."""
    store = botmod.SentLinksStore(tmp_path / 'sent.json')
    title = 'Long Unique Anime Title Announces Second Season'
    assert await store.claim('https://example.com/1', title) is True
    assert store.has_similar_title(title) is True

    await store.release('https://example.com/1', title)

    assert store.has_similar_title(title) is False
    assert await store.claim('https://example.com/1', title) is True


@pytest.mark.asyncio
async def test_failed_thread_send_removes_ghost_pending(monkeypatch, tmp_path):
    pending = botmod.PendingPosts(tmp_path / 'pending.json')
    sent = botmod.SentLinksStore(tmp_path / 'sent.json')
    stats = SimpleNamespace(
        record_skipped=AsyncMock(),
        record_failed_send=AsyncMock(),
        record_published=AsyncMock(),
    )
    monkeypatch.setattr(botmod, 'pending_posts', pending)
    monkeypatch.setattr(botmod, 'sent_links', sent)
    monkeypatch.setattr(botmod, 'stats', stats)
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(
        video_enabled=False, require_image=False, dedup_final_text=False,
    ))

    async def prepare(_news, _source, count_stats=True):
        return None

    async def video(_news):
        return None

    async def fail_after_pending(_bot, news, _video_file):
        key = pending.add(news)
        news['_pending_key'] = key
        return False

    monkeypatch.setattr(botmod, '_prepare_news_for_send', prepare)
    monkeypatch.setattr(botmod, '_prepare_video_file', video)
    monkeypatch.setattr(botmod, '_send_post_thread_split', fail_after_pending)

    news = {
        'title': 'Thread failure', 'summary': 'x', 'source': 'Test',
        'link': 'https://example.com/fail', 'images': [], 'video': None,
    }
    result = await botmod.send_news_to_thread(object(), news)

    assert result == 'failed'
    assert pending._items == {}
    assert 'https://example.com/fail' not in sent


def test_normalize_url_handles_schemeless_and_relative_urls():
    assert botmod.normalize_url('example.com/path') == 'https://example.com/path'
    assert botmod.normalize_url('/relative/path') == '/relative/path'
    assert botmod.normalize_url('mailto:user@example.com') == 'mailto:user@example.com'


def test_runtime_config_rejects_implicit_legacy_ids(monkeypatch):
    """Новый деплой не должен молча использовать ID из исходного кода."""
    monkeypatch.setattr(botmod, 'ADMIN_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'CHANNEL_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'ALLOW_LEGACY_IDS', False)
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(thread_mode=False))

    with pytest.raises(SystemExit) as exc:
        botmod._validate_runtime_config()
    assert 'ADMIN_ID' in str(exc.value)
    assert 'CHANNEL_ID' in str(exc.value)


def test_runtime_config_silent_when_env_set(monkeypatch):
    monkeypatch.setattr(botmod, 'ADMIN_FROM_ENV', True)
    monkeypatch.setattr(botmod, 'CHANNEL_FROM_ENV', True)
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(thread_mode=False))
    botmod._pending_admin_alerts.clear()
    botmod._validate_runtime_config()
    assert not botmod._pending_admin_alerts


def test_runtime_config_allow_legacy_flag(monkeypatch):
    monkeypatch.setattr(botmod, 'ADMIN_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'CHANNEL_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'ALLOW_LEGACY_IDS', True)
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(thread_mode=False))
    botmod._pending_admin_alerts.clear()
    botmod._validate_runtime_config()
    assert not botmod._pending_admin_alerts   # осознанный запуск — не тревожим


def test_runtime_config_checks_thread_ids(monkeypatch):
    """В режиме ветки отсутствие ID обсуждения тоже останавливает запуск."""
    monkeypatch.setattr(botmod, 'ADMIN_FROM_ENV', True)
    monkeypatch.setattr(botmod, 'CHANNEL_FROM_ENV', True)
    monkeypatch.setattr(botmod, 'DISCUSSION_CHAT_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'DISCUSSION_THREAD_FROM_ENV', False)
    monkeypatch.setattr(botmod, 'ALLOW_LEGACY_IDS', False)
    monkeypatch.setattr(botmod, 'settings', SimpleNamespace(thread_mode=True))
    with pytest.raises(SystemExit) as exc:
        botmod._validate_runtime_config()
    assert 'DISCUSSION_CHAT_ID' in str(exc.value)
    assert 'DISCUSSION_THREAD_ID' in str(exc.value)
