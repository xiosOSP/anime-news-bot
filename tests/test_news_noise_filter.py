"""Отсев не-новостей по заголовку.

Ленты наполовину состоят из материала, который новостью не является:
подборки «10 лучших», тесты, рецензии, объяснялки, годовщины, скидки. Опасность
здесь не в пропущенном шуме, а в обратном: слишком жадное правило молча съедает
настоящую новость, и заметить это невозможно — пост просто не выходит. Поэтому
на каждый вид шума здесь стоит парный заголовок-двойник, который обязан пройти.
"""
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot

NOISE = [
    ('10 Best Anime of 2026 You Must Watch', 'подборка'),
    ('Top 10 Isekai Series, Ranked', 'подборка'),
    ('Every Studio Ghibli Film, Ranked', 'подборка'),
    ('5 лучших аниме осени', 'подборка'),
    ('Топ-10 манги месяца', 'подборка'),
    ('Лучшие моменты «Атаки титанов»', 'подборка'),
    ('Quiz: Which Jujutsu Kaisen Character Are You?', 'тест или опрос'),
    ('Poll: What is the best opening of the season?', 'тест или опрос'),
    ('Тест: какой ты персонаж «Блича»', 'тест или опрос'),
    ('Review: Dandadan Season 2 Premiere', 'рецензия или колонка'),
    ('Chainsaw Man Episode 5 Review', 'рецензия или колонка'),
    ('Opinion — Why anime is better on TV', 'рецензия или колонка'),
    ('Обзор: новый сезон «Магической битвы»', 'рецензия или колонка'),
    ('Мнение: почему ремейк не нужен', 'рецензия или колонка'),
    ('Everything We Know About One Piece Season 3', 'объяснялка, а не новость'),
    ('Demon Slayer ending explained', 'объяснялка, а не новость'),
    ('How to watch Fate series in order', 'объяснялка, а не новость'),
    ('Всё, что известно о втором сезоне', 'объяснялка, а не новость'),
    ('Attack on Titan: 10 Years Later', 'годовщина и ностальгия'),
    ('Looking back at Cowboy Bebop', 'годовщина и ностальгия'),
    ('Crunchyroll merch 30% off this weekend', 'скидки и распродажа'),
    ('Скидки на фигурки в честь праздника', 'скидки и распродажа'),
    ('Fan art of Gojo goes viral', 'фан-контент'),
    ('This cosplay of Frieren nails the details', 'фан-контент'),
    ('Косплей дня: Макима', 'фан-контент'),
    # Живые ленты 22 сентября 2026: всё ниже бот собирал как новость.
    ("TRIGUN STAMPEDE (Thai Dub) - Episode 1 - NOMAN'S LAND", 'серия чужого дубляжа'),
    ('Attack on Titan Final Season (Polish Dub) - Episode 85 - Traitor',
     'серия чужого дубляжа'),
    ('Dan Da Dan (Latin American Spanish Dub) - Episode 2', 'серия чужого дубляжа'),
    ('🎉22 сентября - день рождения Шикамару из аниме «Наруто»', 'годовщина и ностальгия'),
    ('🎁 КАЛЕНДАРЬ НА 22 СЕНТЯБРЯ.', 'годовщина и ностальгия'),
    ('С днём рождения, Гоку!', 'годовщина и ностальгия'),
    ('WilliamEst主演タイBL『You Maniac』第1話レビュー', 'рецензия или колонка'),
    ('【PLAVE Column Vol.2 EUNHO編】メンバーは日本で何したい？', 'рецензия или колонка'),
    ('【テーマ別】カラオケで気持ちよく歌える夏アニメOP／ED主題歌・アニソン特集！', 'подборка'),
    ('『スーパーの裏でヤニすうふたり』佐々木を推したい5つの理由', 'подборка'),
    ('2026秋アニメ（10月期放送）・最速放送・配信＆放送日順まとめ一覧！', 'подборка'),
    ('『ハイキュー!!』キャラクター（登場人物）一覧・紹介＆解説', 'подборка'),
]

# Двойники: те же слова, но это настоящие новости. Каждая строка здесь однажды
# едва не была отсеяна — правило приходилось сужать.
REAL_NEWS = [
    'Attack on Titan Ranked #1 on Oricon Weekly Chart',
    'Review copies of the manga sent to press',
    'Solo Leveling Season 2 Gets 10 New Episodes',
    '2026 Anime Awards Winners Announced',
    '5 Centimeters per Second live action film sets date',
    '3 Days of Happiness anime announced',
    '86 Eighty-Six Season 3 confirmed',
    'Chainsaw Man film tops the box office',
    'MAPPA announces new original anime',
    'Kagurabachi resumes serialization on September 27',
    'Netflix acquires streaming rights to Sakamoto Days',
    'Тестирование новой игры по «Наруто» началось',
    'Косплей-фестиваль объявил даты',
    'Аниме «Ван-Пис» получит новый сезон',
    'Мангака Кагурабати вернулся к работе',
    'Обзорные продажи тома выросли вдвое',
    # Двойники новых правил: дубляжи, которые бот оформляет, день рождения
    # не в начале заголовка, японские «спецпоказ» и «выложат все серии разом».
    'Frieren (English Dub) - Episode 3',
    'Frieren (Russian Dub) - Episode 1',
    'Dub cast for Frieren season 2 announced',
    'В день рождения Оды анонсировали новый фильм One Piece',
    'Календарь релизов Crunchyroll пополнился пятью тайтлами',
    'Happy Birthday to You! anime gets a sequel',
    '『ぐらんぶる』Season 4制作決定＆ビジュアル解禁！',
    '劇場版『響け！ユーフォニアム』特集上映決定',
    '全話まとめて一挙配信決定',
]


@pytest.mark.parametrize(('title', 'reason'), NOISE)
def test_noise_is_recognised_with_its_reason(title, reason):
    """Причина возвращается, а не флаг: отсев уходит в лог и в метрику.

    Пост, исчезнувший без объяснения, невозможно ни проверить, ни обжаловать.
    """
    assert bot.noise_reason({'title': title}) == reason


@pytest.mark.parametrize('title', REAL_NEWS)
def test_real_news_survives(title):
    assert bot.noise_reason({'title': title}) == ''


def test_only_the_headline_decides():
    """В тексте статьи упоминание подборки — обычное дело.

    Жанр материала объявляет заголовок; если смотреть в тело, отсев начнёт
    выбрасывать новости, которые всего лишь ссылаются на чужой список.
    """
    # В тексте нарочно то, что в заголовке отсеялось бы сразу: «Review:» и
    # подборка. Пока проверка смотрит только заголовок, новость проходит.
    news = {'title': 'MAPPA announces new original anime',
            'summary': 'Review: see also our top 10 isekai series, ranked'}
    assert bot.noise_reason(news) == ''


def test_empty_title_is_not_noise():
    assert bot.noise_reason({}) == ''


@pytest.mark.parametrize(('tags', 'reason'), [
    (['арт', 'kusuriya'], 'фан-контент'),
    (['календарь', 'аниме'], 'годовщина и ностальгия'),
    (['Реклама'], 'реклама'),
    (['опрос'], 'тест или опрос'),
])
def test_channel_rubric_marks_noise(tags, reason):
    """Фан-арт «В ночь с Женькой и Маомао» заголовком себя не выдаёт.

    Зато канал сам ставит под ним «#арт | #kusuriya» — это и есть жанр поста.
    """
    news = {'title': '🐸 В ночь с Женькой и Маомао от CL_opium.', '_source_tags': tags}
    assert bot.noise_reason(news) == reason


@pytest.mark.parametrize('tags', [['новость'], ['анонсы', 'новости'], ['аниме'], []])
def test_news_rubric_is_not_noise(tags):
    """«#новость» и «#аниме» о жанре не говорят ничего — пост проходит."""
    news = {'title': 'Анонсирован 4 сезон «Необъятного океана»', '_source_tags': tags}
    assert bot.noise_reason(news) == ''


class TestInTheCollectionFilter:
    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings', MagicMock(local_noise_filter=True))
        monkeypatch.setattr(bot, 'KEYWORDS', [])
        return bot

    def test_noise_never_reaches_the_queue(self, env):
        """Отсев стоит на сборе — до очереди и до вызова модели.

        Разбирать моделью заголовок «10 лучших» значит платить за вывод,
        который виден в самом заголовке, и делать это в каждом цикле.
        """
        assert env.matches_keywords({'title': 'Top 10 Isekai Series, Ranked',
                                     'summary': ''}) is False

    def test_news_passes(self, env):
        assert env.matches_keywords({'title': 'MAPPA announces new original anime',
                                     'summary': ''}) is True

    def test_switch_off_returns_the_old_behaviour(self, env, monkeypatch):
        env.settings.local_noise_filter = False
        assert env.matches_keywords({'title': 'Top 10 Isekai Series, Ranked',
                                     'summary': ''}) is True

    def test_missing_settings_do_not_break_collection(self, monkeypatch):
        """Сбор идёт и до инициализации настроек — падать тут нельзя."""
        monkeypatch.setattr(bot, 'settings', None)
        monkeypatch.setattr(bot, 'KEYWORDS', [])
        assert bot.matches_keywords({'title': 'MAPPA announces new anime',
                                     'summary': ''}) is True


def test_every_rule_is_covered_by_a_test():
    """Новое правило без примера — правило, которое никто не проверял."""
    covered = {reason for _, reason in NOISE}
    declared = {reason for reason, _ in bot.NOISE_TITLE_RULES}
    assert declared == covered, f'без примеров остались: {declared - covered}'
