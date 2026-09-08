"""Offline policy corpus: evasions, addressed abuse and contextual exceptions."""
import pytest

from moderation_rules import check_text, normalize


@pytest.mark.parametrize(('text', 'category'), [
    ('Твоей матери, шлюхе, привет', 'family'),
    ('Твою мать назову шлюхой', 'family'),
    ('Ваши родители — мрази', 'family'),
    ('Твоя вся семья — мрази', 'family'),
    ('Ваши родители твари', 'family'),
    ('Твоей семье, сборищу мразей, привет', 'family'),
    ('Твоя мама ш.л.ю.х.а', 'family'),
    ('Твоя мама ш л ю х а', 'family'),
    ('Твоя мама шл*юха', 'family'),
    ('Твоя мама шлюююха', 'family'),
    ('Твоя мама шл\u200bюхa', 'family'),
    ('Я выебу твою мать', 'family'),
    ('Ты сын проститутки', 'family'),
    ('П.у.т.и.н выступил', 'politics'),
    ('НАТО опять в новостях', 'politics'),
    ('Сливаю его телефон +7 (999) 123-45-67', 'doxxing'),
    ('Его адрес: ул. Ленина, д. 15', 'doxxing'),
    ('Ее адрес: переулок Зеленый, дом 8', 'doxxing'),
    ('Вот телефон @target: +7 999 123 45 67', 'doxxing'),
    ('Деанон: его IP 203.0.113.15', 'doxxing'),
    ('Перешли код от Telegram', 'scam'),
    ('Скинь приватный ключ кошелька', 'scam'),
    ('Пришлите seed phrase', 'scam'),
    ('Никому не говори, пришли пароль мне', 'scam'),
    ('Никому не отправляй пароль. Но пришли пароль мне', 'scam'),
    ('Не отправляй код; отправь сид-фразу', 'scam'),
    ('Удвоим крипту, переведи на кошелек', 'scam'),
    ('Завалим спамом их чат', 'raid'),
    ('Нельзя материться; заспамим их чат', 'raid'),
    ('Не флудим тут, но рейдим https://t.me/target', 'raid'),
    ('Рейдим чужой чат с гильдией, заспамим https://t.me/target', 'raid'),
    ('Я тебя сейчас убью', 'aggression'),
    ('Тебя я завтра зарежу', 'aggression'),
    ('Сломаю тебе ноги', 'aggression'),
    ('Повесься, это всего лишь рофл 😂', 'aggression'),
    ('Не сдохни от смеха, но убейся', 'aggression'),
    ('Ты мразь', 'toxic'),
    ('Ты хуесос, шучу', 'toxic'),
    ('Модератор, ты долбоеб', 'toxic_admin'),
    ('Админ д.е.б.и.л', 'toxic_admin'),
    ('Он написал: «привет», а я тебе: «сдохни»', 'aggression'),
    ('Мне написали «привет». Твоя мама шлюха', 'family'),
    ('«Твоя мама шлюха»', 'family'),
    ('HTTPS://WWW.PORNHUB.COM/view_video', 'nsfw'),
    ('Заткнись. Твоя мама шлюха', 'family'),
    ('Ты мразь; сливаю его телефон +7 (999) 123-45-67', 'doxxing'),
    ('Заткнись! Убейся', 'aggression'),
])
def test_clear_local_violations(text, category):
    verdict = check_text(text)
    assert verdict is not None
    assert verdict.confident
    assert verdict.category == category


@pytest.mark.parametrize('text', [
    'Твоей матери понравится это аниме',
    'Вашим родителям привет',
    'Твоя мама не шлюха',
    'Твоя мама смотрит фильм «Идиоты»',
    'Семья шпиона и политика студии',
    'Я идиот, опять забыл пароль',
    'Ты дурак что ли 😂',
    'Ты дебил 😂',
    'Админ не дебил',
    'Админ не дебил, я идиот',
    'Админ, я идиот',
    'Админ написал хорошее объяснение',
    'Творчество твоей матери прекрасно',
    'Творчество матери — говно',
    'Он написал «твоя мама шлюха»',
    'Мне прислали: "пришли пароль"',
    "Пользователь ответил: 'сдохни'",
    'Мне угрожали: «сломаю тебе ноги»',
    'Он сказал: «Путин опять выступил»',
    'Не сдохни там со смеху',
    'Я не убью тебя',
    'Никогда не пришли пароль незнакомцам',
    'Нельзя отправь пароль — так писать неграмотно',
    'Не пришли пароль, а просто позвони',
    'Никому не отправляй код из смс',
    'Не сливайте его номер +7 (999) 123-45-67',
    'Мой адрес: ул. Ленина, д. 15',
    'Мой телефон +7 (999) 123-45-67',
    'Его номер 123456 — номер заявки',
    'Его IP 999.999.999.999 — пример ошибки',
    'Игровой рейд в 20:00, сбор https://discord.gg/example',
    'Рейдим босса с гильдией https://discord.gg/example',
    'Атакуем босса, голосовой канал https://discord.gg/example',
    'Не рейдим https://t.me/example',
    'Заспамить чужой чат запрещено',
    'Война титанов закончилась',
    'Выбор лучшего героя — за вами',
    'Политика студии в новом сезоне',
])
def test_contextual_exceptions(text):
    assert check_text(text) is None


@pytest.mark.parametrize('text', [
    'Я идиот', 'Этот персонаж дебил', 'Он просто идиот',
    'Ты не дебил', 'Ты посмотри, злодей идиот',
    'Мне написали: «ты дебил»',
])
def test_reply_to_admin_does_not_target_self_or_character(text):
    assert check_text(text, reply_to_user=True, reply_to_admin=True) is None


@pytest.mark.parametrize('text', [
    'Ты дебил', 'Дебил', 'Вы идиоты', 'Ну идиот!',
    'Я идиот, а ты дебил', 'Он идиот, ты просто дебил',
])
def test_reply_to_admin_targets_recipient(text):
    assert check_text(text, reply_to_admin=True).category == 'toxic_admin'


@pytest.mark.parametrize('text', [
    'я и о н', 'а б в г', 'мама-папа', 'по-русски', 's t a r',
    'https://example.org/мanga?name=тest', 'https://example.org/s.t.a.r',
    'https://example.org/Anime?name=Test', 'HTTPS://EXAMPLE.ORG/Anime',
])
def test_normalization_preserves_unrelated_words_and_urls(text):
    assert normalize(text) == text


def test_normalization_is_idempotent():
    text = 'Твoя ма\u200bма ш.л.ю.х.а https://example.org/мanga'
    assert normalize(normalize(text)) == normalize(text)
