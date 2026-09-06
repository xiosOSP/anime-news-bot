"""Тесты retry helper для HTTP-запросов."""
from unittest.mock import patch, MagicMock

import pytest
import requests

import anime_news_bot
from anime_news_bot import http_get_with_retry


@pytest.fixture
def fast_backoffs(monkeypatch):
    """Минимальные паузы чтобы тесты не были медленными."""
    monkeypatch.setattr(anime_news_bot, 'HTTP_RETRY_BACKOFFS', (0.001, 0.001, 0.001))


class TestRetry:
    def test_success_on_first(self, fast_backoffs):
        mock_resp = MagicMock(status_code=200)
        with patch('anime_news_bot.requests.get', return_value=mock_resp) as m:
            result = http_get_with_retry('https://example.com')
            assert result is mock_resp
            assert m.call_count == 1

    def test_retry_on_500(self, fast_backoffs):
        mock_500 = MagicMock(status_code=500)
        mock_200 = MagicMock(status_code=200)
        with patch('anime_news_bot.requests.get', side_effect=[mock_500, mock_500, mock_200]) as m:
            result = http_get_with_retry('https://example.com')
            assert result is mock_200
            assert m.call_count == 3

    def test_retry_on_429(self, fast_backoffs):
        mock_429 = MagicMock(status_code=429)
        mock_200 = MagicMock(status_code=200)
        with patch('anime_news_bot.requests.get', side_effect=[mock_429, mock_200]) as m:
            result = http_get_with_retry('https://example.com')
            assert result is mock_200
            assert m.call_count == 2

    def test_no_retry_on_404(self, fast_backoffs):
        # 404 — не временная ошибка, ретраить не нужно
        mock_404 = MagicMock(status_code=404)
        with patch('anime_news_bot.requests.get', return_value=mock_404) as m:
            result = http_get_with_retry('https://example.com')
            assert result is mock_404
            assert m.call_count == 1

    def test_retry_on_connection_error(self, fast_backoffs):
        mock_200 = MagicMock(status_code=200)
        with patch('anime_news_bot.requests.get',
                   side_effect=[requests.ConnectionError(), mock_200]) as m:
            result = http_get_with_retry('https://example.com')
            assert result is mock_200
            assert m.call_count == 2

    def test_gives_up_after_3(self, fast_backoffs):
        # Все 3 попытки упали
        with patch('anime_news_bot.requests.get',
                   side_effect=requests.ConnectionError()) as m:
            result = http_get_with_retry('https://example.com')
            assert result is None
            assert m.call_count == 3
