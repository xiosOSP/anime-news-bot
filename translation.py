"""Google fallback with finite timeouts and no shared mutable request parameters."""
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator as BaseGoogleTranslator


class GoogleTranslator(BaseGoogleTranslator):
    def translate(self, text, **kwargs):
        text = str(text or '').strip()
        if not text or self._source == self._target:
            return text
        if len(text) > 5000:
            raise ValueError('Google translation input exceeds 5000 characters')
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
