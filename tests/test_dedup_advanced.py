"""Тесты: дедуп размерных вариантов картинок + fuzzy-дедуп похожих постов."""
import time
import tempfile
from pathlib import Path

from anime_news_bot import _dedup_image_variants, SentLinksStore, normalize_title, _title_tokens


class TestImageVariantDedup:
    def test_crunchyroll_five_sizes_to_one(self):
        urls = [f'https://img.cr.com/i/ep26_{s}.jpg'
                for s in ('full', 'large', 'medium', 'small', 'thumb')]
        out = _dedup_image_variants(urls)
        assert len(out) == 1 and out[0].endswith('_full.jpg')

    def test_wxh_suffixes_collapsed_best_kept(self):
        urls = ['https://c.com/pic-640x360.jpg', 'https://c.com/pic-1280x720.jpg']
        out = _dedup_image_variants(urls)
        assert out == ['https://c.com/pic-1280x720.jpg']

    def test_query_width_variants_collapsed(self):
        urls = ['https://c.com/pic.jpg?width=320', 'https://c.com/pic.jpg?width=1200']
        out = _dedup_image_variants(urls)
        assert len(out) == 1 and 'width=1200' in out[0]

    def test_different_images_kept_in_order(self):
        urls = ['https://c.com/a.jpg', 'https://c.com/b.jpg', 'https://c.com/a_thumb.jpg']
        out = _dedup_image_variants(urls)
        assert out == ['https://c.com/a.jpg', 'https://c.com/b.jpg']

    def test_single_url_untouched(self):
        assert _dedup_image_variants(['https://c.com/x.jpg']) == ['https://c.com/x.jpg']


def _store_with(title):
    s = SentLinksStore(Path(tempfile.mktemp(suffix='.json')))
    s._recent_titles.append((time.time(), normalize_title(title), _title_tokens(title)))
    return s


class TestSimilarTitleDedup:
    def test_same_long_title_different_wording(self):
        s = _store_with('Light Novel Mamonotsukai no Musume Gets TV Anime')
        assert s.has_similar_title(
            'Heir to a Mamono Tsukai no Musume MonsterMancer Anime: Cast Announced') is True

    def test_close_wording_jaccard(self):
        s = _store_with('Frieren Season 2 Reveals New Trailer')
        assert s.has_similar_title('Frieren Season 2 Trailer Revealed') is True

    def test_franchise_different_news_not_dup(self):
        s = _store_with('Attack on Titan Movie Announced')
        assert s.has_similar_title('Attack on Titan Manga Gets Spinoff Series') is False

    def test_same_season_different_events_not_dup(self):
        s = _store_with('Frieren Season 2 Premiere Date Revealed')
        assert s.has_similar_title('Frieren Season 2 New Trailer') is False

    def test_outside_window_ignored(self):
        s = SentLinksStore(Path(tempfile.mktemp(suffix='.json')))
        s._recent_titles.append((time.time() - 60 * 3600,
                                 normalize_title('Mamonotsukai no Musume Gets TV Anime'),
                                 _title_tokens('Mamonotsukai no Musume Gets TV Anime')))
        assert s.has_similar_title('Mamono Tsukai no Musume Anime: Cast Announced') is False

    def test_empty_title_false(self):
        s = _store_with('Anything Here At All')
        assert s.has_similar_title('') is False
