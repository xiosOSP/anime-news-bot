"""Тесты SentLinksStore — дедупликация по URL и заголовку."""
import json

import pytest

from anime_news_bot import SentLinksStore


@pytest.fixture
def store(tmp_json):
    return SentLinksStore(tmp_json)


class TestClaim:
    async def test_first_claim_succeeds(self, store):
        ok = await store.claim('https://example.com/news/1', 'Title One')
        assert ok is True

    async def test_duplicate_url_fails(self, store):
        await store.claim('https://example.com/1', 'Title')
        ok = await store.claim('https://example.com/1', 'Different Title')
        assert ok is False

    async def test_duplicate_title_fails(self, store):
        await store.claim('https://a.com/1', 'Same Title')
        ok = await store.claim('https://b.com/2', 'Same Title')
        assert ok is False

    async def test_normalized_url_matches(self, store):
        # URL с utm — нормализуется в тот же
        await store.claim('https://example.com/news/1', 'T1')
        ok = await store.claim('https://example.com/news/1?utm_source=rss', 'T2')
        assert ok is False

    async def test_www_normalized(self, store):
        await store.claim('https://www.example.com/news/1', 'T')
        ok = await store.claim('https://example.com/news/1', 'Other')
        assert ok is False


class TestRelease:
    async def test_release_allows_reclaim(self, store):
        await store.claim('https://example.com/1', 'T')
        await store.release('https://example.com/1', 'T')
        ok = await store.claim('https://example.com/1', 'T')
        assert ok is True

    async def test_release_nonexistent_no_error(self, store):
        # Не должно падать
        await store.release('https://never-claimed.com/x', 'X')


class TestContains:
    async def test_contains_after_claim(self, store):
        await store.claim('https://example.com/1', 'T')
        assert 'https://example.com/1' in store

    async def test_contains_normalized(self, store):
        await store.claim('https://example.com/1', 'T')
        assert 'https://www.example.com/1?utm_source=rss' in store


class TestPersistence:
    async def test_persists_to_disk(self, tmp_json):
        store1 = SentLinksStore(tmp_json)
        await store1.claim('https://example.com/1', 'T1')
        await store1.claim('https://example.com/2', 'T2')

        # Новый инстанс с тем же файлом
        store2 = SentLinksStore(tmp_json)
        assert 'https://example.com/1' in store2
        assert 'https://example.com/2' in store2

    async def test_loads_old_format(self, tmp_json):
        # Старый формат: просто list[str]
        tmp_json.write_text(json.dumps([
            'https://www.example.com/old1',
            'https://example.com/old2/',
        ]))
        store = SentLinksStore(tmp_json)
        # Должно мигрировать в новый формат
        assert 'https://example.com/old1' in store
        assert 'https://example.com/old2' in store

        # Файл переписан в новый формат
        data = json.loads(tmp_json.read_text())
        assert isinstance(data, dict)
        assert 'urls' in data
        assert 'titles' in data


class TestTitleNormalization:
    async def test_punctuation_difference_caught(self, store):
        # Эти два — один и тот же заголовок с разной пунктуацией
        await store.claim('https://a.com/1', 'Title here!')
        ok = await store.claim('https://b.com/2', 'Title here.')
        assert ok is False

    async def test_case_difference_caught(self, store):
        await store.claim('https://a.com/1', 'TITLE HERE')
        ok = await store.claim('https://b.com/2', 'title here')
        assert ok is False
