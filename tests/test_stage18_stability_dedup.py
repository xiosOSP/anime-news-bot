import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anime_news_bot as bot


def test_story_registry_blocks_same_delivered_story_next_cycle(tmp_path):
    store = bot.StoryRegistry(tmp_path / 'stories.json')
    first = {
        'title': 'Chainsaw Man movie reveals new trailer',
        '_llm_subject': 'Chainsaw Man',
    }
    memory = store.observe(first, ['Source A'], ['https://a.test/1'])
    first['_story_registry_id'] = memory['registry_id']
    assert store.mark_delivery(first) is True

    second = {
        'title': 'New trailer revealed for Chainsaw Man movie',
        '_llm_subject': 'Chainsaw Man',
    }
    memory2 = store.observe(second, ['Source B'], ['https://b.test/2'])
    assert memory2['registry_id'] == memory['registry_id']
    assert memory2['delivery_duplicate'] is True


def test_story_registry_keeps_distinct_editorial_events(tmp_path):
    store = bot.StoryRegistry(tmp_path / 'stories.json')
    trailer = {
        'title': 'Chainsaw Man movie reveals new trailer',
        '_llm_subject': 'Chainsaw Man',
    }
    memory = store.observe(trailer, ['A'], ['https://a.test/trailer'])
    trailer['_story_registry_id'] = memory['registry_id']
    store.mark_delivery(trailer)

    visual = {
        'title': 'Chainsaw Man movie reveals new visual',
        '_llm_subject': 'Chainsaw Man',
    }
    memory2 = store.observe(visual, ['B'], ['https://b.test/visual'])
    assert memory2['registry_id'] == memory['registry_id']
    assert memory2['delivery_duplicate'] is False


def test_pending_creation_does_not_claim_successful_moderation(tmp_path, monkeypatch):
    yield_store = bot.SourceYieldStore(tmp_path / 'yield.json')
    monkeypatch.setattr(bot, 'source_yield', yield_store)
    pending = bot.PendingPosts(tmp_path / 'pending.json')
    pending.add({'title': 'Not sent yet', 'source': 'A'})
    row = {item['source']: item for item in yield_store.snapshot()}.get('A', {})
    assert row.get('moderation_sent', 0) == 0


def test_confirmed_thread_send_counts_moderation_not_publication(tmp_path, monkeypatch):
    yield_store = bot.SourceYieldStore(tmp_path / 'yield.json')
    registry = bot.StoryRegistry(tmp_path / 'stories.json')
    news = {
        'title': 'Frieren season 2 release date announced',
        'summary': 'Details', 'source': 'A', 'link': 'https://a.test/frieren',
        'images': [], 'video': None, '_llm_subject': 'Frieren',
    }
    memory = registry.observe(news, ['A'], [news['link']])
    news['_story_registry_id'] = memory['registry_id']

    pending = bot.PendingPosts(tmp_path / 'pending.json')
    monkeypatch.setattr(bot, 'pending_posts', pending)
    monkeypatch.setattr(bot, 'sent_links', bot.SentLinksStore(tmp_path / 'sent.json'))
    monkeypatch.setattr(bot, 'source_yield', yield_store)
    monkeypatch.setattr(bot, 'story_registry', registry)
    monkeypatch.setattr(bot, 'stats', SimpleNamespace(
        record_skipped=AsyncMock(), record_failed_send=AsyncMock(),
        record_published=AsyncMock()))
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(
        video_enabled=False, require_image=False, dedup_final_text=False,
        last_publish_at=''))
    monkeypatch.setattr(bot, 'matches_keywords', lambda _news: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))

    async def confirmed(_bot, item, _video):
        item['_pending_key'] = pending.add(item)
        return True

    monkeypatch.setattr(bot, '_send_post_thread_split', confirmed)
    result = asyncio.run(bot.send_news_to_thread(object(), news))
    assert result == 'sent'
    row = {item['source']: item for item in yield_store.snapshot()}['A']
    assert row['moderation_sent'] == 1
    assert row['published'] == 0


def test_queue_deduplicates_http_https_and_registry_id(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    queue = bot.PostQueue(tmp_path / 'queue.json')

    async def run():
        added = await queue.push_many([
            {'title': 'A', 'link': 'http://example.test/story?utm_source=rss',
             '_story_registry_id': 'story-1'},
            {'title': 'A mirror', 'link': 'https://example.test/story',
             '_story_registry_id': 'story-1'},
        ])
        return added, await queue.peek_size()

    assert asyncio.run(run()) == (1, 1)


def test_polling_conflict_record_does_not_mark_process_stopped(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    path.write_text(json.dumps({'state': 'running', 'pid': bot.os.getpid()}),
                    encoding='utf-8')
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    monkeypatch.setattr(bot, '_lifecycle_started', True)
    bot._mark_polling_conflict('busy', 2)
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['state'] == 'running'
    assert data['polling_conflict_attempts'] == 2


def test_instance_lock_waits_for_rolling_deploy_owner(tmp_path, monkeypatch):
    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4

        def __init__(self):
            self.acquire_calls = 0

        def flock(self, _fd, operation):
            if operation == self.LOCK_UN:
                return
            self.acquire_calls += 1
            if self.acquire_calls < 3:
                raise OSError('busy')

    fake = FakeFcntl()
    bot._release_instance_lock()
    monkeypatch.setattr(bot, 'fcntl', fake)
    monkeypatch.setattr(bot, 'DATA_DIR', Path(tmp_path))
    monkeypatch.setattr(bot.time, 'sleep', lambda _seconds: None)
    bot._acquire_instance_lock(wait_seconds=10)
    try:
        assert fake.acquire_calls == 3
        assert bot._instance_lock_handle is not None
    finally:
        bot._release_instance_lock()
