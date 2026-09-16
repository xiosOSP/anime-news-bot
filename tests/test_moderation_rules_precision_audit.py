"""Ordinary discussion must not become a confident offline sanction."""
import pytest

from moderation_rules import check_text


@pytest.mark.parametrize('text', [
    'Голосуйте за Джо Джо!',
    'Голосуйте за Наруто в опросе!',
    'Голосуйте за лучший опенинг сезона',
    'Админ, идиотский вопрос: когда новая серия?',
    'Модератор, дебильная серия получилась',
    'https://example.com/путин/',
    'Картинка https://example.com/сдохни/preview.jpg',
    'Смотри https://example.com/?q=убейся',
])
def test_ordinary_words_and_fan_voting_are_allowed(text):
    assert check_text(text) is None


@pytest.mark.parametrize('text', [
    'Я посмотрел сцену, где герой кричит «сдохни»',
    'Я прочитал фразу «я тебя убью» в манге',
    'Я цитирую правило: «заткнись» писать нельзя',
    'Нельзя писать сдохни в чате',
    'Не говори заткнись другим участникам',
    'Введи пароль в официальном приложении',
    'Введите код из смс для входа в свой аккаунт',
    'Введи пароль на https://telegram.org',
    'Введи код из смс на https://unknown.example',
])
def test_ambiguous_context_never_causes_automatic_punishment(text):
    verdict = check_text(text)
    assert verdict is None or not verdict.confident


@pytest.mark.parametrize(('text', 'category'), [
    ('Голосуйте за партию!', 'politics'),
    ('Голосуйте за Путина', 'politics'),
    ('Голосуйте за нашего депутата', 'politics'),
    ('Админ, ты идиот', 'toxic_admin'),
    ('Модератор, дебил', 'toxic_admin'),
    ('Твоя мама идиотка', 'family'),
    ('Я тебе говорю: «сдохни»', 'aggression'),
    ('А я тебе: «сдохни»', 'aggression'),
    ('Я говорю тебе: «убейся»', 'aggression'),
    ('Я сказал: «убью тебя»', 'aggression'),
    ('Нельзя писать сдохни, но ты мразь', 'toxic'),
    ('Нельзя писать сдохни, но убейся', 'aggression'),
    ('Пришли пароль мне', 'scam'),
    ('Пришли код из смс для подтверждения', 'scam'),
    ('Введи пароль и отправь мне', 'scam'),
    ('https://www.pornhub.com/watch', 'nsfw'),
    ('Заспамим https://t.me/target', 'raid'),
    ('https://example.com/путин/ но ты мразь', 'toxic'),
    ('Введи пароль в приложении, но ты мразь', 'toxic'),
    ('Введи пароль в приложении, убейся', 'aggression'),
    ('Началась путина, но ты мразь', 'toxic'),
])
def test_clear_abuse_and_scam_remain_confident(text, category):
    verdict = check_text(text)
    assert verdict is not None and verdict.confident
    assert verdict.category == category
