"""Тесты parse_episode — распознавание новостей о выходе эпизодов."""
from anime_news_bot import parse_episode


class TestBasicEpisodes:
    def test_simple_episode(self):
        ep = parse_episode('Detective Conan - Episode 1200')
        assert ep is not None
        assert ep['episode_num'] == '1200'
        assert 'Detective Conan' in ep['anime_title']

    def test_dash_em(self):
        # Длинное тире вместо обычного
        ep = parse_episode('Detective Conan — Episode 1200')
        assert ep is not None

    def test_no_episode_keyword(self):
        # Без слова "episode" — может быть не эпизод
        ep = parse_episode('Detective Conan returns to TV')
        assert ep is None


class TestDubMarkers:
    def test_english_dub(self):
        ep = parse_episode('Show Title - Episode 5 (English Dub)')
        assert ep is not None
        # parse_episode переводит метку дубляжа на русский
        dub = ep.get('dub', '')
        assert 'английск' in dub.lower()

    def test_russian_dub(self):
        ep = parse_episode('Show Title - Episode 5 (Russian Dub)')
        assert ep is not None
        dub = ep.get('dub', '')
        assert 'русск' in dub.lower()


class TestNonEpisodes:
    def test_news_post_not_episode(self):
        ep = parse_episode('MAPPA studio announces new anime project')
        assert ep is None

    def test_random_text(self):
        ep = parse_episode('Some random news headline')
        assert ep is None
