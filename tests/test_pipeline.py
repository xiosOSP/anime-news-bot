"""Юнит-тесты конвейера постов.

`test_invariants.py` следит за тем, чтобы не вернулись уже исправленные
проблемы. Здесь другое: проверяется поведение, от которого напрямую зависит
качество постов в канале — что отсеивается, что переводится, как режется текст
и как считаются дубли. Раньше эта часть не была покрыта ничем, и сломать её
можно было незаметно.

Все функции здесь чистые: ни сети, ни диска, ни Telegram.
"""
import time

import pytest

import anime_news_bot as bot


# ---------- фильтрация: что вообще попадает в канал ----------

@pytest.mark.parametrize('title', [
    'New figure release for Naruto',
    'Pre-order the new plushie',
    'Anime giveaway: win a keychain',
    'Ichiban Kuji lottery announced',
])
def test_blacklist_catches_merch(title):
    """Товарка и розыгрыши — не новости, они не должны доходить до канала."""
    assert bot.matches_blacklist({'title': title, 'summary': ''})


@pytest.mark.parametrize('title', [
    'One Piece gets new season in 2026',
    'Studio announces anime adaptation',
    'Trailer revealed for the movie',
])
def test_blacklist_passes_real_news(title):
    """Обычная новость не должна отсеиваться: ложное срабатывание дороже пропуска."""
    assert bot.matches_blacklist({'title': title, 'summary': ''}) is None


def test_blacklist_reports_which_word_matched():
    """Причина отсева нужна в логах: без неё непонятно, почему пост исчез."""
    assert bot.matches_blacklist({'title': 'Limited merchandise drop', 'summary': ''}) == 'merchandise'


# ---------- очистка текста ----------

@pytest.mark.parametrize('raw,expected', [
    ('<p>Анонс</p><script>alert(1)</script><p>сезона</p>', 'Анонс сезона'),
    ('<style>.a{color:red}</style>Текст', 'Текст'),
    ('<div>a</div><SCRIPT>bad()</SCRIPT><div>b</div>', 'a b'),
])
def test_clean_html_drops_script_bodies(raw, expected):
    """Тело <script> — это код, а не текст новости.

    Снятие одних лишь угловых скобок оставляло JavaScript в посте: RSS-описания
    с виджетами соцсетей приносили его в канал вперемешку с анонсом.
    """
    assert bot.clean_html(raw) == expected


def test_clean_html_unescapes_entities():
    assert bot.clean_html('Аниме &amp; манга &laquo;X&raquo;') == 'Аниме & манга «X»'


@pytest.mark.parametrize('limit', [2, 5, 20, 100, 1024])
def test_truncation_never_exceeds_limit(limit):
    """Многоточие — тоже символ. Подпись впритык под лимит Telegram обязана влезать."""
    long_text = 'слово ' * 400
    assert len(bot.smart_truncate(long_text, limit)) <= limit
    assert len(bot.fit_to_limit(long_text, limit)) <= limit


def test_truncation_keeps_short_text_intact():
    assert bot.smart_truncate('короткий текст', 100) == 'короткий текст'


def test_caption_fits_telegram_limit_after_escaping():
    """Лимит Telegram считается по тексту после разбора сущностей.

    Поэтому резать надо ДО html.escape: иначе граница попадёт внутрь `&amp;`
    и Bot API получит битую разметку.
    """
    text = 'A & B ' * 500
    escaped = bot._escape_to_limit(text, bot.TG_CAPTION_LIMIT)
    assert '&' in escaped
    assert not escaped.endswith('&am')
    assert not escaped.endswith('&')


# ---------- перевод: названия не должны переводиться ----------

def test_protect_terms_hides_known_titles():
    """Названия франшиз заменяются плейсхолдером, чтобы переводчик их не тронул."""
    protected, mapping = bot.protect_terms('Attack on Titan season 4')
    assert 'Attack on Titan' not in protected
    assert 'Attack on Titan' in mapping.values()


def test_restore_terms_is_exact_inverse():
    """Восстановление обязано вернуть исходное название буква в букву."""
    source = 'Attack on Titan season 4'
    protected, mapping = bot.protect_terms(source)
    assert bot.restore_terms(protected, mapping) == source


def test_restore_terms_survives_spacing_changes():
    """Переводчики меняют пробелы вокруг плейсхолдера — название всё равно должно вернуться."""
    protected, mapping = bot.protect_terms('Attack on Titan season 4')
    mangled = protected.replace('〖', ' 〖').replace('〗', '〗 ')
    assert 'Attack on Titan' in bot.restore_terms(mangled, mapping)


# ---------- дедуп ----------

def test_normalize_url_collapses_tracking_and_case():
    """Один и тот же материал не должен выглядеть разным из-за utm и регистра."""
    a = bot.normalize_url('https://WWW.Example.com/news/1/?utm_source=rss&id=7#top')
    b = bot.normalize_url('http://example.com/news/1?id=7')
    assert a == b


def test_normalize_url_keeps_relative_links_untouched():
    """Относительная ссылка не должна превращаться в выдуманный https-адрес."""
    assert not bot.normalize_url('/local/path').startswith('https:')


def test_story_similarity_separates_seasons():
    """Season 2 и Season 3 — разные события при одинаковом шаблоне заголовка."""
    a = {'title': 'One Piece Season 2 announced by Toei'}
    b = {'title': 'One Piece Season 3 announced by Toei'}
    assert bot._story_similarity(a, b) == 0.0


def test_story_similarity_merges_same_event_from_two_sources():
    a = {'title': 'Bleach TYBW Part 4 opening theme revealed'}
    b = {'title': 'Bleach TYBW Part 4 opening theme revealed by jo0ji'}
    assert bot._story_similarity(a, b) >= bot.STORY_CLUSTER_SIMILARITY


def test_russian_and_english_ordinals_match():
    """«Второй сезон» и «Season 2» должны давать одинаковые числа для дедупа."""
    assert bot._story_numbers({'title': 'Второй сезон аниме'}) == {'2'}
    assert bot._story_numbers({'title': 'Anime Season 2'}) == {'2'}


# ---------- свежесть ----------

def test_fresh_post_is_not_too_old():
    assert bot._is_too_old(time.gmtime()) is False


def test_ancient_post_is_rejected():
    old = time.gmtime(time.time() - (bot.POST_MAX_AGE_HOURS + 24) * 3600)
    assert bot._is_too_old(old) is True


def test_missing_date_is_not_treated_as_ancient():
    """Источник без даты не должен молча терять все свои новости."""
    assert bot._is_too_old(None) is False


# ---------- разбор эпизодов ----------

def test_parse_episode_extracts_title_and_number():
    parsed = bot.parse_episode('Naruto - Episode 5 - The Test')
    assert parsed is not None
    assert parsed['anime_title'] == 'Naruto'
    assert parsed['episode_num'] == '5'


def test_parse_episode_ignores_ordinary_news():
    assert bot.parse_episode('Studio announces new project') is None


# ---------- картинки ----------

def test_thumbnail_urls_are_recognised():
    assert bot._looks_like_thumbnail('https://cdn/x-150x150.jpg')
    assert not bot._looks_like_thumbnail('https://cdn/x-1920x1080.jpg')


def test_upgrade_image_url_strips_size_suffix():
    assert bot.upgrade_image_url('https://cdn/pic-300x200.jpg') == 'https://cdn/pic.jpg'


def test_larger_image_scores_higher():
    small = bot._image_size_score('https://cdn/a-320x180.jpg')
    large = bot._image_size_score('https://cdn/a-1920x1080.jpg')
    assert large > small
