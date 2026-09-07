"""Формат поста: без ссылок, без хвостов, с законченным смыслом.

Пост в канал читают подписчики, а не редакторы. Им некуда идти по ссылке и
незачем видеть обрывок предложения: пост должен быть законченным сам по себе.
"""
import pytest

import anime_news_bot as bot


# ---------- ссылок в тексте поста нет ----------

@pytest.mark.parametrize('text,forbidden', [
    ('Смотрите на https://www.crunchyroll.com/news/1 подробности.', 'crunchyroll.com'),
    ('Читайте на animenewsnetwork.com', 'animenewsnetwork'),
    ('Подписывайтесь на t.me/somechannel', 't.me'),
    ('Трейлер тут www.youtube.com/watch?v=x', 'youtube.com'),
])
def test_links_are_removed(text, forbidden):
    """Чужой t.me в своём канале — прямая реклама конкурента."""
    assert forbidden not in bot._strip_links(text).lower()


def test_meaningful_part_of_the_sentence_survives():
    """Выбрасывать смысл вместе со ссылкой нельзя.

    «Подробности на сайте X — премьера в апреле» несёт факт, который читателю
    и нужен: дату. Удалять предложение целиком значит терять её.
    """
    out = bot._strip_links('Подробности на сайте https://shikimori.one/a/1 — премьера в апреле.')
    assert 'премьера в апреле' in out.lower()
    assert 'shikimori' not in out


def test_sentence_that_was_only_a_link_disappears():
    """«Читайте на» без адреса — не текст, а огрызок."""
    assert bot._strip_links('Читайте подробности на animenewsnetwork.com') == ''


def test_leadin_does_not_stay_dangling():
    """После удаления ссылки не должно оставаться предлога в никуда."""
    out = bot._strip_links('Премьера сегодня. Смотреть на https://x.com/1 и читать на y.org.')
    assert not out.rstrip('.').endswith(('на', 'в', 'и', 'при'))
    assert 'x.com' not in out and 'y.org' not in out


def test_text_without_links_is_untouched():
    """Чистка не должна портить обычный текст."""
    text = 'Премьера 4 апреля, студия подтвердила состав.'
    assert bot._strip_links(text) == text


def test_dates_are_not_mistaken_for_domains():
    """«4.04» и подобное — не адрес сайта."""
    text = 'Выйдет 4.04 в 20.00 по московскому времени.'
    assert bot._strip_links(text) == text


# ---------- хвостов нет ----------

def test_unfinished_tail_is_cut_to_the_last_whole_sentence():
    """RSS обрывается на полуслове, и в пост уходил обрывок."""
    out = bot._drop_unfinished_tail('Первое предложение целое. Второе оборвалось на союзе и')
    assert out == 'Первое предложение целое.'


def test_fragment_without_any_whole_sentence_is_dropped():
    """Лучше короче, но целиком: заголовок самодостаточен."""
    assert bot._drop_unfinished_tail('Совсем нет границы и текст обрывается на союзе и') == ''


def test_finished_text_is_left_alone():
    text = 'Всё хорошо, предложение закончено.'
    assert bot._drop_unfinished_tail(text) == text


def test_post_has_no_dangling_tail(monkeypatch):
    """Проверка на сквозном пути, а не только на помощнике."""
    monkeypatch.setattr(bot, 'settings', bot.BotSettings.__new__(bot.BotSettings))
    monkeypatch.setattr(bot.BotSettings, 'llm_enabled', property(lambda self: False))
    news = {'title': 'Новое аниме анонсировано', 'lang': 'ru',
            'summary': 'Студия подтвердила производство и назвала состав, а также сообщила что',
            'link': 'https://example.com/a', 'source': 'X'}
    body = bot.format_news_short(news)
    assert not body.rstrip().endswith((' и', ' а', ' что', ' но'))


# ---------- ссылка на видео: ветка да, канал нет ----------

def test_video_link_is_not_added_for_the_channel(monkeypatch):
    """Подписчику нужен готовый пост, а не адрес, по которому надо идти."""
    monkeypatch.setattr(bot, 'CHANNEL_ID', '-1001234567890')
    out = bot._add_video_link_to_text('Текст', 'https://youtube.com/watch?v=x',
                                      target='-1001234567890')
    assert 'youtube.com' not in out
    assert out == 'Текст'


def test_video_link_is_still_added_for_the_thread(monkeypatch):
    """В ветке сидят модераторы: для них это способ проверить ролик."""
    monkeypatch.setattr(bot, 'CHANNEL_ID', '-1001234567890')
    out = bot._add_video_link_to_text('Текст', 'https://youtube.com/watch?v=x',
                                      target='-1009999999999')
    assert 'youtube.com' in out


# ---------- ссылки не проходят ни одним из путей конвейера ----------

@pytest.fixture
def _ru(monkeypatch, tmp_path):
    """Русская новость: переводчик не нужен, проверяем только формат."""
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    bot.settings.llm_enabled = False
    return monkeypatch


def test_link_from_the_model_does_not_reach_the_post(_ru):
    """Модель пересказывает статью и иногда переносит из неё адрес.

    Этот путь идёт мимо разбора описания: текст модели уходил в пост как есть.
    """
    news = {'title': 'Трейлер', 'lang': 'ru', 'summary': '', 'source': 'X',
            'link': 'https://example.com/a',
            '_llm_text': 'Вышел трейлер. Подробности на https://crunchyroll.com/news/1'}
    body = bot.format_news_short(news)
    assert 'crunchyroll' not in body.lower()
    assert 'трейлер' in body.lower()


def test_link_from_the_summary_does_not_reach_the_post(_ru):
    """Описание из ленты — самый частый источник ссылок в посте."""
    news = {'title': 'Аниме анонсировано', 'lang': 'ru', 'source': 'X',
            'link': 'https://example.com/a',
            'summary': 'Премьера в апреле. Подписывайтесь на t.me/чужойканал.'}
    body = bot.format_news_short(news)
    assert 't.me' not in body
    assert 'апрел' in body.lower(), 'вместе со ссылкой потерян смысл'


def test_edited_text_is_left_as_the_admin_wrote_it(_ru):
    """Ручную правку не трогаем: админ видел, что писал."""
    news = {'title': 'x', 'lang': 'ru', 'summary': '', 'source': 'X',
            '_edited_text': 'Текст с https://example.com/a оставлен намеренно'}
    assert bot.format_news_short(news) == news['_edited_text']


def test_links_never_reach_the_translator(_ru, monkeypatch):
    """Чистка до перевода — не дубль чистки после.

    Переводчик коверкает домены («www.Crunchyroll.com» с заглавной) и тратит
    на них лимит символов, который у DeepL считается помесячно. Адрес не
    должен доходить до него вообще.
    """
    seen: list[str] = []

    def fake_translate(text, input_limit=None):
        seen.append(text)
        return text

    monkeypatch.setattr(bot, 'translate_text', fake_translate)
    news = {'title': 'Trailer revealed', 'lang': 'en', 'source': 'X',
            'link': 'https://example.com/a',
            'summary': 'Watch it at https://www.crunchyroll.com/news/1 today. '
                       'Madhouse returns for the sequel.'}
    bot.format_news_short(news)
    assert seen, 'переводчик не вызывался — тест ничего не проверил'
    for chunk in seen:
        assert 'crunchyroll' not in chunk.lower(), f'адрес ушёл в переводчик: {chunk!r}'
