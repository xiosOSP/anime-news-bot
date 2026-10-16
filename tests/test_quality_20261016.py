"""Пустой слот канала объясняет себя; пересказ заголовка другими словами убирается.

Анализ канала (октябрь 2026, пересказ): несколько часовых слотов подряд бот
ничего не выложил, и по каналу было не понять, кончились ли новости или
проверки отсеяли всё, что лежало в ветке. А под заголовком «Питер Сафран
подтвердил, что решение о втором сезоне «Фонари» будет принято до конца года»
вышла строка «По словам продюсера Питера Сафрана, решение… ожидается к концу
года» — тот же факт другими словами.
"""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot


# ---------------------------------------------------------------- пустой слот

@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=48, auto_enabled=True))
    store = bot.PendingPosts(tmp_path / 'pending.json')
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, '_autopost_empty', {})
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)

    async def due():
        return True
    monkeypatch.setattr(bot, '_channel_autopost_due', due)
    return store


def test_empty_slot_tells_why(pending, monkeypatch):
    western = pending.add({'title': 'Трейлер «Мстителей»', 'link': 'https://a.test/1'})
    pending.add({'title': 'Кадры «Ледяной стены»', 'link': 'https://a.test/2'})
    old = pending.add({'title': 'Старая новость', 'link': 'https://a.test/3'})
    pending._items[old]['ts'] = time.time() - 72 * 3600
    monkeypatch.setattr(bot, '_autopost_skip_reason',
                        lambda news: 'западное кино сверх лимита' if 'Мстител' in news['title']
                        else 'про тайтл уже был пост' if 'стены' in news['title'] else '')
    assert asyncio.run(bot._autopost_one_from_thread(object())) is None
    line = bot._autopost_empty_status_line()
    assert 'в ветке 3' in line
    assert 'западное кино сверх лимита — 1' in line and 'про тайтл уже был пост — 1' in line
    assert 'старше 48 ч — 1' in line
    assert western in pending._items


def test_empty_thread_says_so(pending):
    assert asyncio.run(bot._autopost_one_from_thread(object())) is None
    assert 'в ветке нет постов' in bot._autopost_empty_status_line()


def test_reasons_come_from_each_check(monkeypatch):
    news = {'title': 'x'}
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda n: False)
    monkeypatch.setattr(bot, '_work_repeat_blocks', lambda n: True)
    monkeypatch.setattr(bot, '_tg_account_media_waits', lambda n: False)
    assert bot._autopost_skip_reason(news) == 'про тайтл уже был пост'
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda n: True)
    assert bot._autopost_skip_reason(news) == 'западное кино сверх лимита'
    assert bot._autopost_skip(news) is True


def test_repeated_empty_slot_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(bot, '_autopost_empty', {})
    with caplog.at_level('INFO'):
        for _ in range(3):
            bot._note_empty_slot({'про тайтл уже был пост': 2}, 2)
    assert sum('подходящего нет' in r.getMessage() for r in caplog.records) == 1


def test_status_mentions_the_empty_slot():
    import inspect
    assert '_autopost_empty_status_line()' in inspect.getsource(bot.status)


# ---------------------------------------------------------------- пересказ заголовка

FONARI = 'Питер Сафран подтвердил, что решение о втором сезоне «Фонари» будет принято до конца года.'


def test_paraphrase_with_source_attribution_is_an_echo():
    body = 'По словам продюсера Питера Сафрана, решение о втором сезоне «Фонари» ожидается к концу года.'
    assert bot._drop_repetitive_paragraphs(FONARI, [body]) == []


def test_declensions_of_a_short_word_are_one_word():
    assert bot._content_stems('до конца года') == bot._content_stems('к концу года')


@pytest.mark.parametrize('body', [
    'Решение о втором сезоне «Фонари» Питер Сафран примет вместе с HBO.',
    'Решение о втором сезоне «Фонари» будет принято до конца года, а съёмки начнутся весной.',
    'Решение о втором сезоне «Фонари» не будет принято до конца года.',
    'По словам Питера Сафрана из HBO, решение о втором сезоне «Фонари» будет принято до конца года.',
    'Питер Сафран подтвердил: решение о втором сезоне «Фонари» будет принято весной.',
])
def test_new_name_time_or_negation_keeps_the_line(body):
    assert bot._drop_repetitive_paragraphs(FONARI, [body]) == [body]


def test_short_line_with_one_new_word_stays():
    title = 'Анонсирован второй сезон аниме «Ледяная стена»'
    body = 'Второй сезон аниме «Ледяной стены» официально анонсирован студией.'
    assert bot._drop_repetitive_paragraphs(title, [body]) == [body]
