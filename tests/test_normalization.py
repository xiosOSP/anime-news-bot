"""Тесты нормализации URL и заголовков."""
from anime_news_bot import normalize_url, normalize_title


class TestNormalizeURL:
    def test_removes_www(self):
        assert normalize_url('https://www.example.com/news/1') == 'https://example.com/news/1'

    def test_removes_trailing_slash(self):
        assert normalize_url('https://example.com/news/1/') == 'https://example.com/news/1'

    def test_keeps_root_slash(self):
        # Корневой слэш должен остаться
        assert normalize_url('https://example.com/') == 'https://example.com/'

    def test_removes_utm(self):
        result = normalize_url('https://example.com/news/1?utm_source=rss&utm_medium=email')
        assert result == 'https://example.com/news/1'

    def test_removes_fbclid(self):
        result = normalize_url('https://example.com/news/1?fbclid=abc&id=123')
        assert result == 'https://example.com/news/1?id=123'

    def test_removes_ref(self):
        result = normalize_url('https://example.com/news/1?ref=twitter')
        assert result == 'https://example.com/news/1'

    def test_keeps_meaningful_params(self):
        result = normalize_url('https://example.com/news?page=2&sort=newest')
        # Параметры в порядке как пришли (или не в порядке — но оба должны быть)
        assert 'page=2' in result
        assert 'sort=newest' in result

    def test_lowercase_scheme_and_host(self):
        result = normalize_url('HTTPS://Example.COM/News/1')
        # Scheme и host в нижнем регистре, path — нет
        assert result.startswith('https://example.com/')
        assert '/News/1' in result

    def test_strips_fragment(self):
        assert normalize_url('https://example.com/news/1#section') == 'https://example.com/news/1'

    def test_empty_returns_empty(self):
        assert normalize_url('') == ''
        assert normalize_url('   ') == ''

    def test_invalid_returns_input(self):
        # Не падает даже на мусоре
        result = normalize_url('not a url at all')
        assert isinstance(result, str)


class TestNormalizeTitle:
    def test_lowercase(self):
        assert normalize_title('Hello World') == 'helloworld'

    def test_removes_punctuation(self):
        assert normalize_title('Hello, World!') == 'helloworld'

    def test_removes_spaces(self):
        assert normalize_title('  spaces  here  ') == 'spaceshere'

    def test_unicode_preserved(self):
        # Кириллица сохраняется
        assert normalize_title('Привет, мир!') == 'приветмир'

    def test_empty(self):
        assert normalize_title('') == ''
        assert normalize_title(None) == ''

    def test_same_after_normalization(self):
        # Эти два заголовка отличаются только пунктуацией — после норм. одинаковые
        a = normalize_title('GameStop submits offer to acquire eBay!')
        b = normalize_title('GameStop submits offer to acquire eBay.')
        assert a == b


class TestNormalizeImageURL:
    """Тесты нормализации URL картинок (фикс битых URL от ANN)."""
    BASE = 'https://www.animenewsnetwork.com/news/2026-01-01/article'

    def test_relative_path_gets_domain(self):
        from anime_news_bot import _normalize_image_url
        result = _normalize_image_url('/images/pic.jpg', self.BASE)
        assert result == 'https://www.animenewsnetwork.com/images/pic.jpg'

    def test_protocol_relative(self):
        from anime_news_bot import _normalize_image_url
        result = _normalize_image_url('//cdn.example.com/pic.jpg', self.BASE)
        assert result == 'https://cdn.example.com/pic.jpg'

    def test_absolute_unchanged(self):
        from anime_news_bot import _normalize_image_url
        url = 'https://cdn.example.com/full.jpg'
        assert _normalize_image_url(url, self.BASE) == url

    def test_empty_returns_none(self):
        from anime_news_bot import _normalize_image_url
        assert _normalize_image_url('', self.BASE) is None
        assert _normalize_image_url(None, self.BASE) is None

    def test_empty_host_returns_none(self):
        from anime_news_bot import _normalize_image_url
        # Это была ошибка "url host is empty"
        assert _normalize_image_url('https:///pic.jpg', self.BASE) is None

    def test_non_http_scheme_rejected(self):
        from anime_news_bot import _normalize_image_url
        assert _normalize_image_url('ftp://example.com/pic.jpg', self.BASE) is None
        assert _normalize_image_url('data:image/png;base64,xxx', self.BASE) is None

    def test_no_base_url_keeps_absolute(self):
        from anime_news_bot import _normalize_image_url
        url = 'https://cdn.example.com/pic.jpg'
        assert _normalize_image_url(url, None) == url
