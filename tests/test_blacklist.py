"""Тесты фильтра blacklist."""
from anime_news_bot import matches_blacklist


def make(title, summary=''):
    return {'title': title, 'summary': summary}


class TestBlacklistBlocks:
    def test_figure_release(self):
        assert matches_blacklist(make('New figure release announced')) is not None

    def test_pre_order(self):
        assert matches_blacklist(make('Pre-order opens for Chainsaw Man figurine')) is not None

    def test_plushie(self):
        assert matches_blacklist(make('Demon Slayer plushie hits stores')) is not None

    def test_raffle(self):
        assert matches_blacklist(make('Spy x Family raffle event')) is not None

    def test_crypto(self):
        assert matches_blacklist(make('Anime crypto NFT collection')) is not None

    def test_in_summary(self):
        assert matches_blacklist(make('Some article', 'merchandise drop next month')) is not None

    def test_case_insensitive(self):
        assert matches_blacklist(make('NEW FIGURE RELEASE')) is not None


class TestBlacklistAllows:
    def test_normal_news(self):
        assert matches_blacklist(make('Wit Studio announces One Piece remake')) is None

    def test_demon_slayer_episode(self):
        assert matches_blacklist(make('New episode of Demon Slayer airs Sunday')) is None

    def test_japanese_title(self):
        assert matches_blacklist(make('Tonari no Wakao-kun manga gets TV anime')) is None

    def test_word_within_word_not_matched(self):
        # 'keychain' внутри 'keychainmaker' не должно срабатывать (word boundaries)
        assert matches_blacklist(make('The keychainmaker story')) is None
