"""Тесты DeepL перевода с fallback на Google."""
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot
from anime_news_bot import _deepl_translate


@pytest.fixture
def deepl_response():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'translations': [{'text': 'Привет мир', 'detected_source_language': 'EN'}]}
    return resp


class TestDeeplTranslate:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        assert _deepl_translate('Hello') is None

    def test_free_key_uses_free_endpoint(self, monkeypatch, deepl_response):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        with patch('anime_news_bot.requests.post', return_value=deepl_response) as m:
            result = _deepl_translate('Hello')
            assert result == 'Привет мир'
            assert 'api-free.deepl.com' in m.call_args[0][0]

    def test_pro_key_uses_pro_endpoint(self, monkeypatch, deepl_response):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'pro-key')
        with patch('anime_news_bot.requests.post', return_value=deepl_response) as m:
            _deepl_translate('Hello')
            assert m.call_args[0][0] == 'https://api.deepl.com/v2/translate'

    def test_quota_exceeded_returns_none(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        mock = MagicMock(status_code=456)
        with patch('anime_news_bot.requests.post', return_value=mock):
            assert _deepl_translate('Hello') is None

    def test_invalid_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'bad-key')
        mock = MagicMock(status_code=403)
        with patch('anime_news_bot.requests.post', return_value=mock):
            assert _deepl_translate('Hello') is None

    def test_empty_translations_returns_none(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        mock = MagicMock(status_code=200)
        mock.json.return_value = {'translations': []}
        with patch('anime_news_bot.requests.post', return_value=mock):
            assert _deepl_translate('Hello') is None

    def test_auth_header_sent(self, monkeypatch, deepl_response):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'secret:fx')
        with patch('anime_news_bot.requests.post', return_value=deepl_response) as m:
            _deepl_translate('Hello')
            headers = m.call_args.kwargs['headers']
            assert 'DeepL-Auth-Key secret:fx' in headers['Authorization']


class TestTranslateTextIntegration:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        # Мокаем Google и AniList
        class FakeGoogle:
            def translate(self, text):
                return 'GOOGLE:' + text
        class FakeAnilist:
            def lookup(self, q):
                return None
        monkeypatch.setattr(anime_news_bot, 'translator', FakeGoogle())
        monkeypatch.setattr(anime_news_bot, 'anilist', FakeAnilist())
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})

    def test_falls_back_to_google_without_deepl(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        result = anime_news_bot.translate_text('Test text')
        assert 'GOOGLE:' in result

    def test_uses_deepl_when_key_set(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        mock = MagicMock(status_code=200)
        mock.json.return_value = {'translations': [{'text': 'DEEPL result'}]}
        with patch('anime_news_bot.requests.post', return_value=mock):
            result = anime_news_bot.translate_text('Test text')
            assert 'DEEPL' in result

    def test_deepl_failure_falls_back_to_google(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        mock = MagicMock(status_code=456)  # лимит исчерпан
        with patch('anime_news_bot.requests.post', return_value=mock):
            result = anime_news_bot.translate_text('Test text')
            assert 'GOOGLE:' in result  # откатился на Google


class TestTranslatorEngineSwitch:
    """Тесты переключателя переводчика (settings.translator_engine)."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        class FakeTr:
            def translate(self, text):
                return 'GOOGLE:' + text
        class FakeAni:
            def lookup(self, q):
                return None
        monkeypatch.setattr(anime_news_bot, 'translator', FakeTr())
        monkeypatch.setattr(anime_news_bot, 'anilist', FakeAni())
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})

    def test_engine_google_skips_deepl(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        fake_settings = MagicMock()
        fake_settings.translator_engine = 'google'
        monkeypatch.setattr(anime_news_bot, 'settings', fake_settings)
        called = []
        monkeypatch.setattr(anime_news_bot, '_deepl_translate',
                            lambda t: called.append(1) or 'DEEPL:' + t)
        result = anime_news_bot.translate_text('Test one')
        assert 'GOOGLE:' in result
        assert not called

    def test_engine_deepl_uses_deepl(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'key:fx')
        fake_settings = MagicMock()
        fake_settings.translator_engine = 'deepl'
        monkeypatch.setattr(anime_news_bot, 'settings', fake_settings)
        monkeypatch.setattr(anime_news_bot, '_deepl_translate', lambda t: 'DEEPL:' + t)
        result = anime_news_bot.translate_text('Test two')
        assert 'DEEPL:' in result

    def test_settings_none_safe(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(anime_news_bot, 'settings', None)
        result = anime_news_bot.translate_text('Test three')
        assert 'GOOGLE:' in result

    def test_setting_persists(self, tmp_path):
        from anime_news_bot import BotSettings
        p = tmp_path / 's.json'
        s = BotSettings(p)
        assert s.translator_engine == 'deepl'
        s.translator_engine = 'google'
        s2 = BotSettings(p)
        assert s2.translator_engine == 'google'

    def test_invalid_value_normalized(self, tmp_path):
        from anime_news_bot import BotSettings
        s = BotSettings(tmp_path / 's.json')
        s.translator_engine = 'nonsense'
        assert s.translator_engine == 'deepl'


class TestDeeplUsage:
    def test_no_key_none(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        data, err = anime_news_bot._deepl_usage()
        assert data is None and err == 'ключ не задан'

    def test_free_endpoint_and_data(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        mock = MagicMock(status_code=200)
        mock.json.return_value = {'character_count': 100, 'character_limit': 500000}
        with patch('anime_news_bot.requests.get', return_value=mock) as m:
            data, err = anime_news_bot._deepl_usage()
            assert data['character_limit'] == 500000 and err == ''
            assert 'api-free.deepl.com/v2/usage' in m.call_args[0][0]
            # WAF-обход: обязателен нормальный User-Agent
            assert 'User-Agent' in m.call_args.kwargs['headers']

    def test_get_403_falls_back_to_post(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        ok = MagicMock(status_code=200)
        ok.json.return_value = {'character_count': 1, 'character_limit': 2}
        with patch('anime_news_bot.requests.get', return_value=MagicMock(status_code=403)), \
             patch('anime_news_bot.requests.post', return_value=ok) as p:
            data, err = anime_news_bot._deepl_usage()
        assert data is not None and err == ''
        assert 'api-free.deepl.com/v2/usage' in p.call_args[0][0]

    def test_403_everywhere_tries_second_endpoint(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        hosts = []
        def fake(url, headers=None, timeout=None):
            hosts.append(url.split('/')[2])
            if 'api-free' in url:
                return MagicMock(status_code=403)
            m = MagicMock(status_code=200)
            m.json.return_value = {'character_count': 1, 'character_limit': 2}
            return m
        with patch('anime_news_bot.requests.get', side_effect=fake), \
             patch('anime_news_bot.requests.post', side_effect=fake):
            data, err = anime_news_bot._deepl_usage()
        assert data is not None and err == ''
        assert 'api-free.deepl.com' in hosts and 'api.deepl.com' in hosts

    def test_error_reports_primary_host(self, monkeypatch):
        # Ошибка в отчёте — от ПЕРВОГО (правильного) endpoint'а, а не от fallback
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        with patch('anime_news_bot.requests.get', return_value=MagicMock(status_code=403)), \
             patch('anime_news_bot.requests.post', return_value=MagicMock(status_code=403)):
            data, err = anime_news_bot._deepl_usage()
        assert data is None
        assert 'api-free.deepl.com' in err

    def test_non_403_stops_immediately(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        with patch('anime_news_bot.requests.get', return_value=MagicMock(status_code=456)) as g, \
             patch('anime_news_bot.requests.post') as p:
            data, err = anime_news_bot._deepl_usage()
        assert data is None and 'HTTP 456' in err
        assert g.call_count == 1 and p.call_count == 0


class TestQuietMode:
    def test_default_on_and_persist(self, tmp_path):
        from anime_news_bot import BotSettings
        p = tmp_path / 's.json'
        s = BotSettings(p)
        assert s.quiet_mode is True
        s.quiet_mode = False
        assert BotSettings(p).quiet_mode is False

    def test_last_daily_summary_persist(self, tmp_path):
        from anime_news_bot import BotSettings
        p = tmp_path / 's.json'
        s = BotSettings(p)
        assert s.last_daily_summary == ''
        s.last_daily_summary = '2026-07-02'
        assert BotSettings(p).last_daily_summary == '2026-07-02'


class TestDeeplXmlPlaceholders:
    """DeepL не должен ломать плейсхолдеры 〖N〗 (уходят как <x>N</x> ignore-теги)."""

    def test_placeholders_survive_roundtrip(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', 'k:fx')
        captured = {}
        def fake_post(url, data=None, headers=None, timeout=None):
            captured['data'] = data
            m = MagicMock(status_code=200)
            m.json.return_value = {'translations': [{'text': 'Аниме <x>0</x> и <x>2000</x>'}]}
            return m
        with patch('anime_news_bot.requests.post', side_effect=fake_post):
            out = anime_news_bot._deepl_translate('Anime 〖0〗 and 〖2000〗')
        assert captured['data']['tag_handling'] == 'xml'
        assert captured['data']['ignore_tags'] == 'x'
        assert '<x>0</x>' in captured['data']['text']
        assert '〖0〗' in out and '〖2000〗' in out


class TestRestoreFallback:
    def test_broken_placeholder_in_quotes_restored(self):
        placeholders = {'〖0〗': 'Chainsaw Man', '〖2000〗': 'Kusuriya no Hitorigoto'}
        broken = 'Манга «2000» и аниме «0» выходят'
        out = anime_news_bot.restore_terms(broken, placeholders)
        assert 'Kusuriya no Hitorigoto' in out and 'Chainsaw Man' in out

    def test_normal_tokens_still_work(self):
        placeholders = {'〖5〗': 'MAPPA'}
        out = anime_news_bot.restore_terms('Студия 〖5〗 анонсировала', placeholders)
        assert out == 'Студия MAPPA анонсировала'
