from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import requests

from translation import GoogleTranslator


def test_translator_copies_query_parameters_between_threads(monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs)
        return MagicMock(text=f'<div class="t0">{kwargs["params"]["q"]}</div>')

    monkeypatch.setattr(requests, 'get', get)
    translator = GoogleTranslator(source='en', target='ru')
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(translator.translate, ['first', 'second'])) == ['first', 'second']
    assert {r['params']['q'] for r in seen} == {'first', 'second'}
    assert seen[0]['params'] is not seen[1]['params']
    assert 'q' not in translator._url_params
    assert all(r['timeout'] == (5, 15) for r in seen)


@pytest.mark.parametrize('html', ['<html>captcha</html>', '<div class="t0"></div>'])
def test_missing_translation_does_not_silently_succeed(monkeypatch, html):
    response = MagicMock(text=html)
    calls = []

    def get(*a, **kw):
        calls.append(a)
        return response

    monkeypatch.setattr(requests, 'get', get)
    with pytest.raises(ValueError):
        GoogleTranslator(source='en', target='ru').translate('news')
    # Переводчик теперь пробует два бесплатных endpoint Google по очереди
    # (словарный и мобильный), и мок отдаёт один объект на оба запроса.
    # Смысл проверки прежний: каждый полученный ответ закрыт ровно один раз.
    assert len(calls) == 2
    assert response.close.call_count == len(calls)
