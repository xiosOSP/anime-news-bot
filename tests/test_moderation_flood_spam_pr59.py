"""Regression tests for PR #59 flood/spam precision."""
import anime_news_bot as bot


def _reset():
    bot._moderation_recent.clear()
    bot._moderation_windows.clear()


def test_three_normal_no_replies_are_not_spam():
    """Real screenshot: a plain «Нет» was classified as spam."""
    _reset()
    for _ in range(3):
        bot._mod_note_message(100, 7, 'User', 'Нет')
    assert bot._mod_local_check(100, 7, 'Нет') is None


def test_many_repeated_short_replies_are_still_caught():
    _reset()
    for _ in range(bot.MODERATION_SHORT_REPEAT_LIMIT):
        bot._mod_note_message(101, 7, 'User', 'Нет')
    verdict = bot._mod_local_check(101, 7, 'Нет')
    assert verdict is not None
    assert verdict['category'] == 'spam'
    assert verdict['confident'] is True


def test_interleaved_fast_conversation_is_not_burst_flood():
    _reset()
    chat = 102
    user = 7
    # Eight quick replies from one person used to trip the same coarse threshold.
    # Two other people actively replying makes this look like conversation.
    for index in range(bot.MODERATION_FLOOD_MESSAGES):
        bot._mod_note_message(chat, user, 'User', f'ответ {index}')
        if index in (2, 5):
            bot._mod_note_message(chat, 100 + index, 'Other', 'ответ собеседника')
    verdict = bot._mod_local_check(chat, user, 'последняя реплика')
    assert verdict is None or verdict['category'] != 'flood'


def test_non_interleaved_burst_is_still_flood():
    _reset()
    chat = 103
    for index in range(bot.MODERATION_FLOOD_MESSAGES):
        bot._mod_note_message(chat, 7, 'User', f'сообщение {index}')
    verdict = bot._mod_local_check(chat, 7, 'следующее')
    assert verdict is not None
    assert verdict['category'] == 'flood'
    assert 'быстрый поток' in verdict['reason']


def test_bare_invite_needs_context_but_promo_invite_is_local_spam():
    _reset()
    bare = bot._mod_local_check(104, 7, 'https://t.me/+abcdef')
    assert bare == {
        'category': 'spam',
        'confident': False,
        'reason': 'ссылка-приглашение без явного рекламного контекста',
    }
    promo = bot._mod_local_check(104, 7, 'залетай https://t.me/+abcdef')
    assert promo is not None
    assert promo['category'] == 'spam'
    assert promo['confident'] is True


def test_sustained_stream_is_caught_even_below_burst_rate(monkeypatch):
    _reset()
    clock = [1000.0]
    monkeypatch.setattr(bot.time, 'time', lambda: clock[0])
    chat = 105
    for index in range(bot.MODERATION_FLOOD_SUSTAINED_MESSAGES):
        bot._mod_note_message(chat, 7, 'User', f'длинная реплика {index}')
        clock[0] += 2.5
    verdict = bot._mod_local_check(chat, 7, 'ещё сообщение')
    assert verdict is not None
    assert verdict['category'] == 'flood'
    assert 'устойчивый поток' in verdict['reason']
