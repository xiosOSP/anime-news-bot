"""Обрывистый пост и странный заголовок — находки аудита.

Четыре случая, каждый воспроизводился на настоящих функциях: баннер вместо
заголовка, абзац вместо заголовка, многоточие вместо конца мысли и обрывок
на предлоге.
"""
import pytest

import anime_news_bot as bot


class TestBannerIsNotAHeadline:
    """Канал кричит «СРОЧНО», а в наш пост это уезжало заголовком."""

    @pytest.mark.parametrize('banner', ['🔥 СРОЧНО', '⚡️ВАЖНО', 'BREAKING', '❗ ВНИМАНИЕ'])
    def test_caps_banner_gives_way_to_the_news(self, banner):
        """Список рубрик закрыт, а баннеры каналы придумывают свои.

        Общий признак — капслок: строка из одного-двух слов капсом ничего не
        сообщает, поэтому заголовком становится следующая строка.
        """
        title, _summary = bot._tg_title_and_summary(
            f'{banner}\nСтудия MAPPA анонсировала новый проект.', 'ch', 'Канал')
        assert title == 'Студия MAPPA анонсировала новый проект.'

    def test_a_real_headline_in_caps_survives(self):
        """Капслок на весь заголовок — стиль канала, а не рубрика: в нём есть факт."""
        title, _ = bot._tg_title_and_summary(
            'ВЫШЕЛ ТРЕЙЛЕР ВТОРОГО СЕЗОНА\nПремьера в январе.', 'ch', 'Канал')
        assert title == 'ВЫШЕЛ ТРЕЙЛЕР ВТОРОГО СЕЗОНА'

    def test_a_banner_alone_is_not_thrown_away(self):
        """Под баннером ничего нет — отдаём как есть, иначе новость исчезнет."""
        title, _ = bot._tg_title_and_summary('СРОЧНО', 'ch', 'Канал')
        assert title == 'СРОЧНО'


class TestHeadlineIsOneSentence:
    def test_a_paragraph_splits_into_headline_and_body(self):
        """Абзац целиком в заголовке — это стена текста и пустой пост под ней."""
        title, summary = bot._tg_title_and_summary(
            'Новая глава «Магической битвы» выйдет на следующей неделе. '
            'И это отличная новость.', 'ch', 'Канал')
        assert title == 'Новая глава «Магической битвы» выйдет на следующей неделе.'
        assert summary == 'И это отличная новость.'

    def test_an_abbreviation_does_not_split_the_headline(self):
        """«12 окт.» — не конец предложения, рвать заголовок по такой точке нельзя."""
        title, summary = bot._tg_title_and_summary(
            'Премьера состоится 12 окт. на Netflix', 'ch', 'Канал')
        assert title == 'Премьера состоится 12 окт. на Netflix'
        assert summary == ''

    def test_a_two_word_lead_is_not_split_off(self):
        """«Слух.» с текстом под ним — ровно та поломка, от которой уходим."""
        title, summary = bot._tg_title_and_summary(
            'Слух. Netflix готовит экранизацию.', 'ch', 'Канал')
        assert title.startswith('Слух.')
        assert summary == ''


class TestEllipsisIsNotAnEnding:
    def test_a_source_cut_off_sentence_is_dropped(self):
        """«Сериал выйдет...» — это отметка обрыва, а не мысль.

        Источник сам сообщил, что обрезал описание. Пост из такого огрызка и
        есть та «обрывистость», на которую жалуются: лучше один заголовок.
        """
        assert bot._extract_sentences('Сериал выйдет... Подробности позже') == ''

    def test_whole_sentences_before_the_cut_are_kept(self):
        assert bot._extract_sentences('Премьера 4 октября. Сериал выйдет...') \
            == 'Премьера 4 октября.'

    def test_our_own_truncation_rolls_back_to_a_whole_sentence(self):
        """smart_truncate ставит многоточие в середине фразы.

        Раньше защита от обрывков считала такую строку законченной и
        пропускала — в пост уходил текст, оборванный на полуслове.
        """
        text = 'Первое предложение короткое. Второе ' + 'тянется и ' * 30 + 'кончается.'
        assert bot._extract_sentences(text, max_len=200) == 'Первое предложение короткое.'

    def test_a_short_cut_off_fragment_is_not_worth_keeping(self):
        """Отступать до запятой стоит, только если после неё что-то осталось.

        «Сериал выйдет» — не факт, а начало фразы: в посте он выглядит той же
        поломкой, от которой уходим, только с запятой вместо многоточия.
        """
        assert bot._extract_sentences('Сериал выйдет, но...') == ''

    def test_one_long_sentence_falls_back_to_a_clause(self):
        """Одна длинная фраза — выбросить её целиком значит потерять все факты."""
        text = ('Студия MAPPA официально подтвердила производство второго сезона '
                'аниме-адаптации, премьера которого намечена на 2027 год, а режиссёром '
                'вновь выступит Рю Накаяма и вся команда предыдущего сезона.')
        out = bot._extract_sentences(text, max_len=160)
        assert out.endswith('2027 год')
        assert '…' not in out and '...' not in out


class TestFragmentOnAPreposition:
    @pytest.mark.parametrize('text', ['Аниме про', 'Сериал о', 'Манга для', 'Фильм без'])
    def test_a_dangling_preposition_is_rubbish(self, text):
        """Союзы список знал, предлоги — нет, и хвост доезжал до поста."""
        assert bot._drop_unfinished_tail(text) == ''

    def test_a_finished_sentence_is_untouched(self):
        assert bot._drop_unfinished_tail('Сериал выйдет в январе.') \
            == 'Сериал выйдет в январе.'
