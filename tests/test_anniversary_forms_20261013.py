"""Юбилей в творительном падеже и с неразрывным дефисом.

В канал ушёл пост «Премьера аниме «Паразит» отмечена 11‑летием»: дефис там
неразрывный (U+2011), а «летием» правило не знало. Пример — пересказ поста.
"""
import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('title', [
    'Премьера аниме «Паразит» отмечена 11‑летием',
    'Премьера аниме «Паразит» отмечена 11-летием',
    'Аниме «Паразит» отмечает 11‑летие',
])
def test_anniversary_forms_are_noise(title):
    assert bot.noise_reason({'title': title}) == 'годовщина и ностальгия'


def test_anniversary_with_real_event_stays():
    # Юбилей как повод для события — новость.
    assert bot.noise_reason({'title': 'К 10‑летию аниме «Паразит» выйдет фильм'}) == ''


def test_ten_year_old_hero_is_not_anniversary():
    # «10-летний» — возраст героя, не юбилей.
    assert bot.noise_reason({'title': 'Герой аниме — 10-летний мальчик из деревни'}) == ''
