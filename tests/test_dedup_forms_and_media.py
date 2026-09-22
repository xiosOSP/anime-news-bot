"""Одна новость из двух источников — один пост; две новости — два поста.

Замер без модели на реалистичных парах: из десяти пар «одно событие разными
словами» семь уходили в канал дважды, а две пары разных событий склеивались —
вторая новость тихо терялась. Причины:

- ядро названия сравнивалось на точное равенство, и одна буква разводила
  сюжет надвое: confirms/confirmed, weeks/week, ends/end;
- синонимы событий были разными маркерами: delayed/postponed, film/movie,
  premiere/premieres;
- «аниме» и «манга» выброшены из токенов как стоп-слова, и перерыв манги
  совпадал с перерывом аниме на 1.00;
- кластеризация пачки не смотрела на тип события: трейлер и ключевой визуал
  одного сезона склеивались при сходстве 0.91.
"""
import pytest

import anime_news_bot as bot
import news_stories


def _row(title, markers=None):
    news = {'title': title}
    return {'delivered_title': title,
            'delivered_numbers': sorted(bot._story_numbers(news)),
            'delivered_markers': sorted(markers if markers is not None
                                        else bot._story_event_markers(news))}


def _delivered_twice(first, second):
    """Вторая новость признана повтором уже доставленной первой."""
    return bot.StoryRegistry._delivery_match({'title': second}, _row(first))


def _one_post(a, b):
    items = [{'title': a, 'link': 'https://a.example/1', 'source': 'A', 'summary': ''},
             {'title': b, 'link': 'https://b.example/2', 'source': 'B', 'summary': ''}]
    return len(bot._cluster_news(items, persist_intelligence=False)) == 1


class TestSameEventInOtherWords:
    @pytest.mark.parametrize('first, second', [
        ('One Piece Manga Goes on Break for Three Weeks', 'One Piece Manga Takes Three-Week Break'),
        ('Solo Leveling Season 3 Confirmed', 'Solo Leveling Anime Confirms Season 3'),
        ('Jujutsu Kaisen Manga Ends in September', 'Jujutsu Kaisen Manga to End This September'),
        ('Kaiju No. 8 Season 2 Premieres in July', 'Kaiju No. 8 Season 2 Premiere Set for July'),
        ('Blue Lock Season 3 Delayed to 2027', 'Blue Lock Season 3 Postponed Until 2027'),
        ('Gundam Movie Postponed to 2027', 'Gundam Film Delayed to 2027'),
    ])
    def test_it_is_recognised_as_a_repeat(self, first, second):
        assert _delivered_twice(first, second)

    def test_markers_saved_before_the_fix_still_match(self):
        """В сохранённых записях маркеры лежат в старом написании («delayed»)."""
        row = _row('Blue Lock Season 3 Delayed to 2027', markers=['delayed', 'season'])
        assert bot.StoryRegistry._delivery_match(
            {'title': 'Blue Lock Season 3 Postponed Until 2027'}, row)


class TestDifferentEventsStaySeparate:
    @pytest.mark.parametrize('a, b', [
        ('One Piece Manga Goes on Break', 'One Piece Anime Goes on Break'),
        ('Attack on Titan Manga Ends', 'Attack on Titan Anime Ends'),
        ('Манга «Ван-Пис» уходит на перерыв', 'Аниме «Ван-Пис» уходит на перерыв'),
    ])
    def test_anime_and_manga_are_two_news(self, a, b):
        assert not _one_post(a, b)
        assert not _delivered_twice(a, b)

    @pytest.mark.parametrize('a, b', [
        ('Frieren Season 2 Trailer Revealed', 'Frieren Season 2 Key Visual Revealed'),
        ('Gachiakuta Season 2 Trailer', 'Gachiakuta Season 2 Teaser'),
        ('Gundam Game Delayed to 2027', 'Gundam Movie Delayed to 2027'),
    ])
    def test_different_event_types_are_not_one_post(self, a, b):
        """Пачка обязана проверять тип события так же, как доставка."""
        assert not _one_post(a, b)

    def test_a_spinoff_still_is_not_the_original(self):
        """Равенство ядер — намеренно строгое: основы слов его не размывают."""
        assert not _delivered_twice('Solo Leveling Season 2 Trailer',
                                    'Solo Leveling Ragnarok Trailer')

    def test_a_title_mentioning_both_media_is_no_conflict(self):
        assert not bot._story_events_conflict('One Piece Anime and Manga Hit Records',
                                              'One Piece Manga Hits Records')


class TestEventSynonyms:
    @pytest.mark.parametrize('title, marker', [
        ('Gundam Movie Delayed', 'delay'), ('Studio Postpones Gundam Movie', 'delay'),
        ('Gundam Movie Postponed', 'delay'), ('Gundam Film Out Now', 'movie'),
        ('Pokemon Horizons Premieres Today', 'premiere'), ('Game Cancelled', 'canceled'),
        ('Сериал отложен', 'перенос'),
    ])
    def test_synonyms_share_one_marker(self, title, marker):
        assert marker in bot._story_event_markers(title)


class TestWordForms:
    @pytest.mark.parametrize('a, b', [('confirms', 'confirmed'), ('weeks', 'week'),
                                      ('ends', 'end'), ('premieres', 'premiere'),
                                      ('stories', 'story')])
    def test_forms_share_a_stem(self, a, b):
        assert news_stories._story_anchor_stem(a) == news_stories._story_anchor_stem(b)

    @pytest.mark.parametrize('word', ['boss', 'ваншот', 'mha', 'одна'])
    def test_short_double_s_and_cyrillic_words_are_untouched(self, word):
        assert news_stories._story_anchor_stem(word) == word
