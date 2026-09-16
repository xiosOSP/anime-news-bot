"""Ordinary speech and opaque media IDs must never become punishments."""
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot
from test_moderation_offline import handle, message, telegram_bot, state as moderation_state


@pytest.fixture
def state(tmp_path, monkeypatch):
    return moderation_state.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('text', [
    'Нежели красную жиду', 'нежели красную жиду!',
    'Расколол чурку для костра', 'Мне принесли чурку для печки',
    'Ожидается новая серия', 'Жидкий чай остыл', 'Купил хачапури',
    'Сделал педикюр', 'Поехали в Нигерию', 'У сервера даунтайм',
    'Скамейка у дома', 'Уродился хороший урожай',
    'Выбор персонажа', 'Политика студии изменилась',
    'Война в сюжете аниме', 'Координация движений',
    'Метод joinchat обрабатывает приглашения',
])
def test_ordinary_words_do_not_spend_a_model_request(text):
    assert bot._mod_local_check(-100, 7, text) is None


@pytest.mark.parametrize('text', [
    'ты жид', 'ты жиду подобен', 'чурка тупая', 'жиды виноваты',
    'ж и д ы виноваты', 'ты пидор', 'пид0р', 'п и д о р',
    'Ты грязный жид', 'Жида на мыло', 'Жида выгоните из чата', '@ivan жид',
])
def test_clear_abuse_still_reaches_moderation(text):
    result = bot._mod_local_check(-100, 7, text)
    assert result is not None


def test_url_path_is_not_a_slur():
    assert bot._mod_hard_slur('https://example.org/пидор/новость') == ''


@pytest.mark.parametrize('link', ['https://t.me/+abcdef', 't.me/joinchat/abcdef', 'discord.gg/abcdef'])
def test_real_invite_is_still_detected(link):
    result = bot._mod_local_check(-100, 7, link)
    assert result['category'] == 'spam' and result['confident']


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Голосуйте за Джо Джо!', 'Нежели красную жиду'])
@pytest.mark.parametrize('llm_enabled', [False, True])
async def test_reported_messages_never_delete_warn_or_call_llm(state, monkeypatch, text, llm_enabled):
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', llm_enabled)
    tg = telegram_bot()
    await handle(message(text), tg)
    assert state.warn_count(-100, 7) == 0
    bot._moderation_classify.assert_not_awaited()
    tg.delete_message.assert_not_awaited()
    tg.restrict_chat_member.assert_not_awaited()
    tg.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticker_file_identifier_cannot_trigger_text_rules(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    tg = telegram_bot()
    await handle(message('', sticker=NS(file_unique_id='nigger', emoji='🙂')), tg)
    bot._moderation_media_scanner.check.assert_awaited_once()
    bot._moderation_classify.assert_not_awaited()
    tg.delete_message.assert_not_awaited()
    assert state.warn_count(-100, 7) == 0
    assert 'nigger' not in bot._moderation_windows[-100][-1]['text']


@pytest.mark.asyncio
async def test_repeated_media_still_counts_as_spam(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_REPEAT_LIMIT', 3)
    monkeypatch.setattr(bot, 'MODERATION_FLOOD_MESSAGES', 20)
    tg = telegram_bot()
    for number in range(1, 4):
        await handle(message('', number, sticker=NS(file_unique_id='same', emoji='🙂')), tg)
    tg.delete_message.assert_awaited_once()
    assert state.warn_count(-100, 7) == 1
