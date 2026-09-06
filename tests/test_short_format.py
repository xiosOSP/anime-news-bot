"""Тесты короткого формата постов (заголовок + предложение + дата из текста)."""
import pytest

import anime_news_bot
from anime_news_bot import (
    _extract_first_sentence,
    extract_release_date_from_text,
)


class TestExtractFirstSentence:
    def test_simple(self):
        text = "First sentence here. Second sentence. Third."
        assert _extract_first_sentence(text) == "First sentence here."

    def test_exclamation(self):
        text = "Big news today! More details follow."
        assert _extract_first_sentence(text) == "Big news today!"

    def test_removes_bracket_ellipsis(self):
        text = "A new movie is in production. [...]"
        result = _extract_first_sentence(text)
        assert '[...]' not in result

    def test_no_sentence_boundary(self):
        text = "Just a fragment without ending"
        assert _extract_first_sentence(text) == "Just a fragment without ending"

    def test_empty(self):
        assert _extract_first_sentence('') == ''

    def test_long_sentence_truncated(self):
        text = "word " * 100
        result = _extract_first_sentence(text, max_len=50)
        assert len(result) <= 51

    def test_read_more_removed(self):
        text = "News content here Read more"
        result = _extract_first_sentence(text)
        assert 'Read more' not in result


class TestExtractReleaseDate:
    def test_month_day_year(self):
        assert extract_release_date_from_text('premieres August 12, 2026') == '12 августа 2026'

    def test_day_month_year(self):
        assert extract_release_date_from_text('opens 12 August 2026') == '12 августа 2026'

    def test_month_year(self):
        assert extract_release_date_from_text('coming in May 2027') == 'май 2027'

    def test_season_year_russian(self):
        assert extract_release_date_from_text('debuts Spring 2027') == 'весна 2027'
        assert extract_release_date_from_text('coming Fall 2026') == 'осень 2026'
        assert extract_release_date_from_text('Winter 2027 premiere') == 'зима 2027'

    def test_month_day_no_year(self):
        assert extract_release_date_from_text('airs July 5 on TV') == '5 июля'

    def test_abbreviated_month(self):
        assert extract_release_date_from_text('Aug. 3, 2026 release') == '3 августа 2026'

    def test_ordinal_suffix(self):
        assert extract_release_date_from_text('on October 15th, 2026') == '15 октября 2026'

    def test_no_date_empty(self):
        assert extract_release_date_from_text('Studio announced a new project') == ''

    def test_old_year_rejected(self):
        assert extract_release_date_from_text('launched in May 1999') == ''

    def test_first_date_by_position_wins(self):
        text = 'Season 2 premieres May 2027. Season 1 aired January 5, 2026.'
        assert extract_release_date_from_text(text) == 'май 2027'

    def test_empty_input(self):
        assert extract_release_date_from_text('') == ''


class TestFormatNewsShort:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        class FakeTr:
            def translate(self, text):
                return text
        class FakeAni:
            def lookup(self, q):
                return None
        monkeypatch.setattr(anime_news_bot, 'translator', FakeTr())
        monkeypatch.setattr(anime_news_bot, 'anilist', FakeAni())
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')

    def test_date_from_text_not_rss(self):
        # Дата берётся из ТЕКСТА новости, не из published_parsed
        import time
        rss_date = time.struct_time((2026, 7, 1, 12, 0, 0, 0, 0, 0))  # 1 июля — дата публикации
        news = {
            'title': 'Anime announced',
            'summary': 'The series premieres on August 12, 2026 in Japan. More info soon.',
            'published_parsed': rss_date,
        }
        result = anime_news_bot.format_news_short(news)
        assert '12 августа 2026' in result   # дата события из текста
        assert '1 июля' not in result         # дата публикации НЕ используется

    def test_no_date_in_text_no_date_line(self):
        import time
        rss_date = time.struct_time((2026, 7, 1, 12, 0, 0, 0, 0, 0))
        news = {
            'title': 'Studio announces new project',
            'summary': 'Details will be revealed later. Nothing specific yet.',
            'published_parsed': rss_date,
        }
        result = anime_news_bot.format_news_short(news)
        assert '📅' not in result  # нет даты в тексте — нет строки даты

    def test_up_to_three_sentences(self):
        news = {
            'title': 'News title',
            'summary': 'First sentence stays. Second also stays. Third stays too. '
                       'Fourth sentence is dropped entirely.',
            'published_parsed': None,
        }
        result = anime_news_bot.format_news_short(news)
        assert 'First sentence stays' in result
        assert 'Third stays too' in result
        assert 'Fourth sentence' not in result  # 4-е отрезано

    def test_post_is_short(self):
        news = {
            'title': 'News title',
            'summary': 'First sentence is here. ' + ('Filler sentence. ' * 50),
            'published_parsed': None,
        }
        result = anime_news_bot.format_news_short(news)
        assert len(result) < 400


class TestSentenceEdgeCases:
    def test_digit_dot_not_boundary(self):
        s = _extract_first_sentence('Akuma de Sourou 4. Doctor Stone also ships.')
        assert not s.endswith('4.')

    def test_year_dot_is_boundary(self):
        s = _extract_first_sentence('The film premieres in 2026. A new season follows.')
        assert s == 'The film premieres in 2026.'

    def test_trailing_comma_ellipsis_removed(self):
        s = _extract_first_sentence('Official teaser with Naruto,…')
        assert not s.endswith(',…')

    def test_unclosed_paren_tail_removed(self):
        s = _extract_first_sentence('Directs anime at studio TriF.(с')
        assert '(с' not in s


class TestDigestSkip:
    def test_promo_and_digest_filtered(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'KEYWORDS', [])
        monkeypatch.setattr(anime_news_bot, 'matches_blacklist', lambda n: None)
        promo = {'title': 'All of our Anime Expo 2026 news and reviews',
                 'summary': 'Come visit us at our panels, events, and booth.'}
        digest = {'title': 'North American Anime, Manga Releases, June 28', 'summary': 'x'}
        normal = {'title': 'Chainsaw Man Season 2 announced', 'summary': 'MAPPA confirmed.'}
        assert anime_news_bot.matches_keywords(promo) is False
        assert anime_news_bot.matches_keywords(digest) is False
        assert anime_news_bot.matches_keywords(normal) is True


class TestExtractSentences:
    def test_three_sentences_max(self):
        from anime_news_bot import _extract_sentences
        t = "One here. Two here. Three here. Four is cut."
        r = _extract_sentences(t, max_sentences=3)
        assert r == "One here. Two here. Three here."

    def test_fewer_than_max(self):
        from anime_news_bot import _extract_sentences
        assert _extract_sentences("Only one.", max_sentences=3) == "Only one."

    def test_japanese_boundaries(self):
        from anime_news_bot import _extract_sentences
        r = _extract_sentences("一つ目。二つ目。三つ目。四つ目。", max_sentences=3)
        assert r.count('。') == 3

    def test_ellipsis_tail_removed(self):
        from anime_news_bot import _extract_sentences
        r = _extract_sentences("One. Two. [...]", max_sentences=3)
        assert '[...]' not in r

    def test_max_len_truncates(self):
        from anime_news_bot import _extract_sentences
        t = "word " * 300  # без границ предложений
        r = _extract_sentences(t, max_sentences=3, max_len=100)
        assert len(r) <= 101

    def test_empty(self):
        from anime_news_bot import _extract_sentences
        assert _extract_sentences('') == ''
