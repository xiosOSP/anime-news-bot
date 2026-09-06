"""Тесты AniListClient с mock API (без реальных HTTP запросов)."""

import pytest

from anime_news_bot import AniListClient


# Mock-база
MOCK_DB = {
    'tonari no wakao-kun': {'romaji': 'Tonari no Wakao-kun', 'english': 'My Neighbor Wakao', 'native': 'となりの若男くん'},
    'one piece': {'romaji': 'ONE PIECE', 'english': 'One Piece', 'native': 'ワンピース'},
    'demon slayer': {'romaji': 'Kimetsu no Yaiba', 'english': 'Demon Slayer', 'native': '鬼滅の刃'},
}


@pytest.fixture
def client(tmp_json, monkeypatch):
    def mock_query(self, search, manga=False):
        return MOCK_DB.get(search.lower().strip())
    monkeypatch.setattr(AniListClient, '_query_api', mock_query)
    return AniListClient(tmp_json)


class TestLookup:
    def test_finds_known(self, client):
        result = client.lookup('Tonari no Wakao-kun')
        assert result is not None
        assert result['romaji'] == 'Tonari no Wakao-kun'

    def test_returns_none_for_unknown(self, client):
        assert client.lookup('Nonexistent XYZ Show') is None

    def test_case_insensitive(self, client):
        a = client.lookup('ONE PIECE')
        b = client.lookup('one piece')
        c = client.lookup('One Piece')
        assert a == b == c

    def test_too_short_returns_none(self, client):
        assert client.lookup('a') is None
        assert client.lookup('') is None

    def test_too_long_returns_none(self, client):
        long_str = 'x' * 200
        assert client.lookup(long_str) is None


class TestCache:
    def test_cache_hit_avoids_api(self, client, monkeypatch):
        # Первый запрос — API
        client.lookup('Tonari no Wakao-kun')

        # Считаем сколько раз вызывается _query_api после
        calls = [0]

        def counting_mock(self, search, manga=False):
            calls[0] += 1
            return MOCK_DB.get(search.lower())

        monkeypatch.setattr(AniListClient, '_query_api', counting_mock)
        result = client.lookup('Tonari no Wakao-kun')
        assert result['romaji'] == 'Tonari no Wakao-kun'
        assert calls[0] == 0  # не было вызовов API

    def test_negative_cache(self, client, monkeypatch):
        # Сохраняем отрицательный результат
        client.lookup('Nonexistent')

        calls = [0]

        def counting_mock(self, search, manga=False):
            calls[0] += 1
            return MOCK_DB.get(search.lower())

        monkeypatch.setattr(AniListClient, '_query_api', counting_mock)
        result = client.lookup('Nonexistent')
        assert result is None
        assert calls[0] == 0  # негативный кеш тоже работает


class TestPersistence:
    def test_survives_restart(self, tmp_json, monkeypatch):
        def mock_query(self, search, manga=False):
            return MOCK_DB.get(search.lower().strip())
        monkeypatch.setattr(AniListClient, '_query_api', mock_query)

        c1 = AniListClient(tmp_json)
        c1.lookup('Tonari no Wakao-kun')

        # Сбрасываем API чтобы убедиться что используется кеш
        def fail_query(self, search, manga=False):
            raise AssertionError(f"Should not call API for '{search}'")
        monkeypatch.setattr(AniListClient, '_query_api', fail_query)

        c2 = AniListClient(tmp_json)
        result = c2.lookup('Tonari no Wakao-kun')
        assert result['romaji'] == 'Tonari no Wakao-kun'
