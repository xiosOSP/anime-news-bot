"""Правки админов в постах бота и недельная сводка (post_feedback).

Примеры правок пересказаны: так админы правили посты бота в ноябре 2026 —
стирали значок 🔄, меняли «Выдали» на «Опубликован», убирали цитату «о чём».
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

import anime_news_bot as bot
import post_feedback as pf
from publication_delivery import SentRecorder


# ---------- что поменял админ ----------

@pytest.mark.parametrize('before, after, about, changes', [
    ('🔄 Вышел трейлер аниме «Ледяная стена».\n\nПремьера в январе.',
     'Вышел трейлер аниме «Ледяная стена».\n\nПремьера в январе.', '', ['emoji_removed']),
    ('🔄 Вышел трейлер аниме «Ледяная стена».', '🎬 Вышел трейлер аниме «Ледяная стена».', '',
     ['emoji_changed']),
    ('🔥 Выдали постер к аниме «Ледяная стена».', '🔥 Опубликован постер к аниме «Ледяная стена».', '',
     ['headline']),
    ('🔥 Анонсировано аниме «Ледяная стена».\n\nСтудия MAPPA.\nИстория о стене изо льда.',
     '🔥 Анонсировано аниме «Ледяная стена».\n\nСтудия MAPPA.', 'История о стене изо льда.',
     ['about_removed']),
    ('🔥 Анонс.\n\nТекст.\n\n#аниме #анонс', '🔥 Анонс.\n\nТекст.', '', ['hashtags_removed']),
    ('🔥 Анонс.\n\nТекст.\n\n📅 январь 2027', '🔥 Анонс.\n\nТекст.', '', ['date_removed']),
    ('🔥 Анонс.\n\nПервая строка.\nВторая строка.', '🔥 Анонс.\n\nПервая строка.', '', ['line_removed']),
    ('🔥 Анонс.\n\nТекст.', '🔥 Анонс.\n\nТекст.\nДописано админом.', '', ['line_added']),
    ('🔥 Анонс.\n\nСтудия снимет сезон.', '🔥 Анонс.\n\nСтудия MAPPA снимет второй сезон.', '', ['body']),
])
def test_edit_kinds(before, after, about, changes):
    assert pf.classify_edit(before, after, about=about)['changes'] == changes


def test_replaced_words_are_collected():
    diff = pf.classify_edit('🔥 Выдали постер к аниме «Ледяная стена».',
                            '🔥 Опубликован постер к аниме «Ледяная стена».')
    assert diff['pairs'] == [['Выдали', 'Опубликован']]


def test_whole_rewrite_is_one_kind():
    diff = pf.classify_edit('🔥 Анонсировано аниме по манге о школе магии.\n\nСтудия снимет сериал.',
                            '🎬 Трейлер фильма вышел вчера вечером на сайте студии.')
    assert diff['changes'][0] == 'rewritten' and 'headline' not in diff['changes']


def test_whitespace_is_not_an_edit():
    assert pf.classify_edit('🔥 Анонс.\n\nТекст.', '🔥 Анонс.\nТекст.  ')['changes'] == []


@pytest.mark.parametrize('head, latin', [
    ('Опубликованы тизеры к аниме «Kekkaishi no Ichirinka».', True),
    ('Вышел трейлер «Рубеж Шангри-Ла» (Shangri-La Frontier).', False),   # русское название, оригинал в скобках
    ('Студия «MAPPA» снимет сериал.', False),                              # одно слово — не тайтл
    ('Сериал «Код Гиас: Lelouch Lamperouge» продлён.', False),             # есть кириллица
    ('Без кавычек: Kekkaishi no Ichirinka.', False),
])
def test_latin_title_in_headline(head, latin):
    assert pf.latin_title_in_headline(head) is latin


# ---------- журнал постов и правок ----------

def test_edit_of_a_bot_post_is_recorded_and_survives_a_restart(tmp_path):
    log = pf.ChannelPostLog(tmp_path / 'channel_posts.json')
    assert log.record_post(101, '🔄 Вышел трейлер.\n\nТекст.', {'topic': 'аниме', 'source': 'TG: A'})
    assert log.record_edit(999, 'чужой пост') is None             # пост админа — не наш
    event = log.record_edit(101, 'Вышел трейлер.\n\nТекст.')
    assert event['changes'] == ['emoji_removed'] and event['topic'] == 'аниме'
    again = pf.ChannelPostLog(tmp_path / 'channel_posts.json')
    posts, edits = again.window(7)
    assert len(posts) == 1 and [e['changes'] for e in edits] == [['emoji_removed']]


def test_second_edit_replaces_the_first_and_undo_clears_it(tmp_path):
    log = pf.ChannelPostLog(tmp_path / 'p.json')
    log.record_post(5, '🔥 Выдали постер.\n\nТекст.')
    log.record_edit(5, 'Выдали постер.\n\nТекст.')
    log.record_edit(5, '🔥 Опубликован постер.\n\nТекст.')      # сравнение — с текстом бота
    assert [e['changes'] for e in log.window(7)[1]] == [['headline']]
    log.record_edit(5, '🔥 Выдали постер.\n\nТекст.')            # вернули как было
    assert log.window(7)[1] == []


def test_log_keeps_only_recent_rows_and_reads_a_broken_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, 'POSTS_MAX', 3)
    path = tmp_path / 'p.json'
    log = pf.ChannelPostLog(path)
    for i in range(5):
        log.record_post(i, f'Пост {i}')
    assert len(log.window(7)[0]) == 3
    path.write_text('{битый', encoding='utf-8')
    assert pf.ChannelPostLog(path).window(7) == ([], [])            # справка, не история: с чистого листа


def test_window_and_digest_week(tmp_path):
    log = pf.ChannelPostLog(tmp_path / 'p.json')
    log.record_post(1, 'Старый пост')
    log._data['posts']['1']['at'] -= 8 * 86400
    log.record_post(2, 'Свежий пост')
    assert [p['text'] for p in log.window(7)[0]] == ['Свежий пост']
    assert log.mark_digest_week('2026-W48')
    assert pf.ChannelPostLog(tmp_path / 'p.json').digest_week() == '2026-W48'


@pytest.mark.parametrize('message_id, text', [(None, 'текст'), ('7', 'текст'), (True, 'текст'), (7, '  ')])
def test_bad_post_is_not_recorded(tmp_path, message_id, text):
    assert pf.ChannelPostLog(tmp_path / 'p.json').record_post(message_id, text) is False


# ---------- текст сводки ----------

def _week():
    posts = [{'text': '🔥 Анонс «Kekkaishi no Ichirinka».', 'topic': 'аниме'},
             {'text': '🎮 Игра «Grand Theft Auto» вышла.', 'topic': 'игры'},
             {'text': '🎬 Трейлер «Ледяной стены».', 'topic': 'аниме'},
             {'text': '🔥 Сезон «Рубеж Шангри-Ла» (Shangri-La Frontier).', 'topic': 'аниме'}]
    edits = [{'changes': ['emoji_removed'], 'pairs': [['Выдали', 'Опубликован']]},
             {'changes': ['emoji_removed', 'headline'], 'pairs': [['выдали', 'опубликован']]}]
    return posts, edits


def test_digest_counts_what_admins_fixed():
    posts, edits = _week()
    text = pf.weekly_digest(posts, edits, ['📚 Shikimori: последний ответ 3 мин назад'],
                            now=datetime(2026, 11, 30, 11, 0))
    assert 'Постов бота в канале: 4' in text and 'Правили админы: 2 (50%)' in text
    assert '• убран значок перед заголовком — 2' in text
    assert '«выдали» → «опубликован» (2)' in text
    assert 'Название латиницей в заголовке: 1 — Анонс «Kekkaishi no Ichirinka»' in text   # игры не в счёт
    assert 'Техника:' in text and 'Shikimori' in text


def test_single_replacement_is_not_called_frequent():
    text = pf.weekly_digest([{'text': 'Пост'}], [{'changes': ['headline'], 'pairs': [['а', 'б']]}])
    assert 'Частые замены' not in text


def test_empty_week_says_so():
    assert 'Данных пока нет' in pf.weekly_digest([], [])


# ---------- самописец отправки ----------

def test_recorder_keeps_what_telegram_answered():
    single = NS(message_id=1, caption='Подпись')
    album = [NS(message_id=2, caption='Альбом'), NS(message_id=3, caption=None)]
    api = NS(send_photo=AsyncMock(return_value=single), send_media_group=AsyncMock(return_value=album),
             get_file=AsyncMock(return_value='файл'))
    recorder = SentRecorder(api)

    async def main():
        assert await recorder.send_photo(chat_id=1, photo='x') is single
        assert await recorder.send_media_group(chat_id=1, media=[]) is album
        assert await recorder.get_file('id') == 'файл'               # не отправка — не записывается
    asyncio.run(main())
    assert [m.message_id for m in recorder.messages] == [1, 2, 3]


# ---------- бот целиком ----------

@pytest.fixture
def log(tmp_path, monkeypatch):
    store = pf.ChannelPostLog(tmp_path / 'channel_posts.json')
    monkeypatch.setattr(bot, 'channel_posts', store)
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'post_feedback', True)
    return store


def _edit_update(message_id, text, chat_id=-1001):
    return NS(edited_channel_post=NS(message_id=message_id, text=text, caption=None,
                                     chat=NS(id=chat_id, username='')))


def test_channel_post_is_remembered_and_its_edit_is_caught(log, monkeypatch):
    async def fake_send_post(api, news, target, video_file):
        await api.send_photo(chat_id=target, photo='x', caption='🔄 Вышел трейлер.')
        return True
    monkeypatch.setattr(bot, '_send_post', fake_send_post)
    monkeypatch.setattr(bot, '_about_line', lambda news: '')
    api = NS(send_photo=AsyncMock(return_value=NS(message_id=77, caption='🔄 Вышел трейлер.', text=None)))
    news = {'title': 'Trailer', '_llm_topic': 'аниме', 'source': 'TG: A', '_story_id': 's1'}
    assert asyncio.run(bot._send_channel_post(api, news)) is True
    posts, _ = log.window(7)
    assert posts[0]['topic'] == 'аниме' and posts[0]['story_id'] == 's1'

    asyncio.run(bot.channel_post_edit_handler(_edit_update(77, 'Вышел трейлер.'), None))
    assert [e['changes'] for e in log.window(7)[1]] == [['emoji_removed']]


def test_edits_elsewhere_or_with_the_feature_off_are_ignored(log, monkeypatch):
    log.record_post(5, '🔄 Пост.')
    asyncio.run(bot.channel_post_edit_handler(_edit_update(5, 'Пост.', chat_id=-2002), None))
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'post_feedback', False)
    asyncio.run(bot.channel_post_edit_handler(_edit_update(5, 'Пост.'), None))
    assert log.window(7)[1] == []


def test_failed_publication_is_not_remembered(log, monkeypatch):
    async def fake_send_post(api, news, target, video_file):
        await api.send_message(chat_id=target, text='Пост')
        return False
    monkeypatch.setattr(bot, '_send_post', fake_send_post)
    api = NS(send_message=AsyncMock(return_value=NS(message_id=9, text='Пост', caption=None)))
    assert asyncio.run(bot._send_channel_post(api, {'title': 'T'})) is False
    assert log.window(7)[0] == []


@pytest.fixture
def digest_env(log, monkeypatch):
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))
    api = NS(send_message=send_message)
    monkeypatch.setattr(bot, '_weekly_digest_tech_lines', lambda: [])
    log.record_post(1, '🔥 Анонс.')
    return NS(sent=sent, ctx=NS(bot=api))


def _at(monkeypatch, moment):
    monkeypatch.setattr(bot, '_local_now', lambda: moment)


def test_digest_goes_out_once_on_monday(digest_env, monkeypatch, log):
    _at(monkeypatch, datetime(2026, 11, 30, 11, 20))               # понедельник
    asyncio.run(bot.weekly_digest_job(digest_env.ctx))
    asyncio.run(bot.weekly_digest_job(digest_env.ctx))             # тот же понедельник
    assert len(digest_env.sent) == 1 and digest_env.sent[0][0] == bot.ADMIN_ID
    assert 'Неделя бота' in digest_env.sent[0][1]
    assert log.digest_week() == '2026-W49'


@pytest.mark.parametrize('moment', [datetime(2026, 11, 29, 12, 0), datetime(2026, 11, 30, 10, 59)])
def test_no_digest_on_other_days_or_too_early(digest_env, monkeypatch, moment):
    _at(monkeypatch, moment)
    asyncio.run(bot.weekly_digest_job(digest_env.ctx))
    assert digest_env.sent == []


def test_undelivered_digest_is_retried(digest_env, monkeypatch, log):
    _at(monkeypatch, datetime(2026, 11, 30, 12, 0))

    async def broken(chat_id, text):
        raise TelegramError('Forbidden')
    digest_env.ctx.bot.send_message = broken
    asyncio.run(bot.weekly_digest_job(digest_env.ctx))
    assert log.digest_week() == ''                                  # следующий час попробует снова


def test_digest_command_answers_now(log, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, '_weekly_digest_tech_lines', lambda: ['📚 Shikimori: в порядке'])
    log.record_post(1, '🔥 Анонс «Kekkaishi no Ichirinka».', {'topic': 'аниме'})
    reply = AsyncMock()
    update = NS(message=NS(reply_text=reply), effective_user=NS(id=bot.ADMIN_ID), effective_chat=NS(id=1, type='private'))
    asyncio.run(bot.digest_command(update, NS()))
    text = reply.await_args.args[0]
    assert 'Постов бота в канале: 1' in text and 'Название латиницей' in text


def test_tech_lines_name_disk_and_silent_sources(monkeypatch):
    import time as time_mod
    monkeypatch.setattr(bot, 'work_titles', None)
    monkeypatch.setattr(bot, 'SOURCES', [('Лента А', None), ('Лента Б', None)])
    monkeypatch.setattr(bot, 'settings', NS(is_source_enabled=lambda name: True))
    monkeypatch.setattr(bot, 'source_health', NS(silent_hours=lambda name: 30 if name == 'Лента А' else None))
    monkeypatch.setattr(bot, '_disk_write_failures', __import__('collections').deque(
        [(time_mod.monotonic(), 'sent_links.json', 'OSError')]))
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    monkeypatch.setattr(bot, '_instance_unguarded_reason', lambda: '')
    lines = bot._weekly_digest_tech_lines()
    assert any('Сбоев записи на диск: 1' in line and 'sent_links.json' in line for line in lines)
    assert any('Молчат больше суток: 1 — Лента А' in line for line in lines)
