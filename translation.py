"""Google fallback with finite timeouts and no shared mutable request parameters."""
import json
import threading
import time

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator as BaseGoogleTranslator


# Бесплатный словарный endpoint Google (им пользуется расширение Chrome). Он
# отвечает серверным адресам, когда мобильная страница translate.google.com/m
# уже отдаёт 429: живой прогон 24.09 — 29 постов подряд отложены с 429 от /m,
# тот же текст через этот endpoint переводится с ответом 200.
DICT_URL = 'https://clients5.google.com/translate_a/t'
# Сколько не ходить в endpoint, ответивший 429. Лимит у Google держится
# долго; без паузы каждый пост тратил бы на заведомый отказ ещё один запрос
# и ещё раз продлевал бан.
RATE_LIMIT_PAUSE_SEC = 600


class GoogleTranslator(BaseGoogleTranslator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paused_until: dict = {}
        self._pause_lock = threading.Lock()

    def _paused(self, name: str) -> bool:
        with self._pause_lock:
            return self._paused_until.get(name, 0.0) > time.monotonic()

    def _pause(self, name: str) -> None:
        with self._pause_lock:
            self._paused_until[name] = time.monotonic() + RATE_LIMIT_PAUSE_SEC

    def translate(self, text, **kwargs):
        text = str(text or '').strip()
        if not text or self._source == self._target:
            return text
        if len(text) > 5000:
            raise ValueError('Google translation input exceeds 5000 characters')
        last_error = None
        for name, attempt in (('dict', self._translate_dict), ('mobile', self._translate_mobile)):
            if self._paused(name):
                continue
            try:
                return attempt(text)
            except requests.HTTPError as e:
                if getattr(e.response, 'status_code', None) == 429:
                    self._pause(name)
                last_error = e
            except (requests.RequestException, ValueError) as e:
                last_error = e
        if last_error is not None:
            raise last_error
        raise RuntimeError('Google Translate: все бесплатные endpoint на паузе после 429')

    def _translate_dict(self, text: str) -> str:
        params = {'client': 'dict-chrome-ex', 'sl': self._source, 'tl': self._target, 'q': text}
        response = requests.get(DICT_URL, params=params, proxies=self.proxies, timeout=(5, 15))
        try:
            response.raise_for_status()
            data = json.loads(response.text)
            # sl=auto: [["перевод","en"]]; явный язык: ["перевод"].
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, list) and first and isinstance(first[0], str):
                    first = first[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
            raise ValueError('Google dict translation response has no translated text')
        finally:
            response.close()

    def _translate_mobile(self, text: str) -> str:
        params = dict(self._url_params, sl=self._source, tl=self._target, q=text)
        response = requests.get(self._base_url, params=params, proxies=self.proxies,
                                timeout=(5, 15))
        try:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            for selector in ('.t0', '.result-container'):
                result = soup.select_one(selector)
                if result is not None:
                    translated = result.get_text(strip=True)
                    if translated:
                        return translated
            raise ValueError('Google translation response has no translated text')
        finally:
            response.close()
