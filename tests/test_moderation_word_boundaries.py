"""Оскорбление ищется словом, а не подстрокой.

Живой случай из чата: «самые ожидаемые события финала ткв» бот посчитал
оскорблением группы людей — из-за «о-ЖИД-аемые». Категория hate мутит сразу,
без ступени предупреждения, то есть человек получил бы час немоты за слово
«ожидание».

Цена ошибки здесь несимметрична. Пропущенное оскорбление увидит человек и
позовёт админа. Несправедливый мут не увидит никто: наказанный молчит.
"""
import pytest

import anime_news_bot as bot

# Каждое слово содержит запрещённый корень подстрокой и не является
# оскорблением. Список растёт от жалоб, а не от фантазии.
INNOCENT = [
    'самые ожидаемые события финала ткв',
    'ожидание премьеры затянулось',
    'жидкий чай остыл',
    'жидкость пролилась на стол',
    'жидковато вышло, но сойдёт',
    'сделал педикюр перед отпуском',
    'хачапури по-аджарски',
    'поехал в Нигерию снимать документалку',
    'нигерийский фильм взял приз',
    'подожди немного, скоро начнём',
]

SLURS = [
    'ты пидор',
    'пидорасы кругом',
    'педики везде',
    'жиды виноваты',
    'какой ты хач',
    'чурка тупая',
    'nigger',
    'faggot',
]


@pytest.mark.parametrize('text', INNOCENT)
def test_ordinary_words_are_not_a_slur(text):
    assert bot._mod_hard_slur(text) == '', f'мут за обычное слово: {text}'


@pytest.mark.parametrize('text', SLURS)
def test_real_slurs_are_still_caught(text):
    assert bot._mod_hard_slur(text) == 'hate', f'оскорбление прошло мимо: {text}'


class TestObfuscationStillWorks:
    """Границы слов не должны открыть дорогу обходу подстановкой букв."""

    @pytest.mark.parametrize('text', ['пид0р', 'п и д о р', 'п-и-д-о-р', 'пииидор'])
    def test_letter_tricks_are_caught(self, text):
        assert bot._mod_hard_slur(text) == 'hate', text

    @pytest.mark.parametrize(('text', 'expected'), [
        ('ж и д ы виноваты', 'hate'),
        ('текст ж и д ы', 'hate'),
        ('о ж и д а е м', ''),
        ('о ж и д а н и е финала', ''),
        ('х а ч а п у р и', ''),
    ])
    def test_split_words_are_glued_back_without_swallowing_neighbours(self, text, expected):
        """Склеивается разбитое слово, а не весь текст.

        Склейка целиком давала слово, которого в сообщении не было: «ж и д ы
        виноваты» превращалось в «жидывиноваты», а «о ж и д а е м» — в строку,
        где корень стоит посреди слова. Первое теряло оскорбление, второе
        выдавало мут за «ожидаем».
        """
        assert bot._mod_hard_slur(text) == expected, bot._mod_variants(text)

    def test_ordinary_text_is_not_glued_at_all(self):
        assert bot._mod_variants('обычный текст без разбиения') == (
            'обычный текст без разбиения',)


class TestExemptionsSurvive:
    """Жалоба, отрицание и разговор о слове наказания не заслуживают."""

    @pytest.mark.parametrize('text', [
        'он написал «пидор», забаньте его',
        'я не пидор если что',
        'нельзя говорить пидор в чате',
    ])
    def test_quoting_and_negation_are_not_punished(self, text):
        assert bot._mod_hard_slur(text) == ''


def test_endings_are_a_closed_list():
    """«Любые буквы после корня» — это и есть исходная ошибка.

    Проверка на конкретном слове, а не на самом списке: список можно
    расширить, но «жидкий» обязан остаться обычным словом.
    """
    assert 'кий' not in bot._MOD_SLUR_ENDINGS
    assert bot._MOD_HARD_SLUR_RE.search('жидкий') is None
    assert bot._MOD_HARD_SLUR_RE.search('жиды') is not None
