"""Тесты отложенной публикации (📅) и ручного редактирования постов (✏️)."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import FakeHTTPResponse
from telegram.ext import ApplicationHandlerStop

import anime_news_bot
from anime_news_bot import (
    PendingPosts,
    ScheduledPosts,
    _human_delta,
    _local_now,
    _parse_schedule_time,
    _utc_to_local,
)


class FakeTr:
    def translate(self, text, input_limit=None):
        return text


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Чистое окружение: пояс UTC+3, свои хранилища, без реального перевода."""
    monkeypatch.setattr(anime_news_bot, 'settings',
                        MagicMock(tz_offset=3, translator_engine='google',
                                  extra_admins=[], open_moderation=True))
    # Тестовые «место» кнопок: чат -100, ветка 10138 (см. _query/_msg_update)
    monkeypatch.setattr(anime_news_bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(anime_news_bot, 'DISCUSSION_THREAD_ID', 10138)
    monkeypatch.setattr(anime_news_bot, 'pending_posts', PendingPosts(tmp_path / 'p.json'))
    monkeypatch.setattr(anime_news_bot, 'scheduled_posts', ScheduledPosts(tmp_path / 's.json'))
    monkeypatch.setattr(anime_news_bot, 'translator', FakeTr())
    monkeypatch.setattr(anime_news_bot, 'anilist', MagicMock(lookup=lambda q: None))
    monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
    monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
    monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
    return anime_news_bot


def _bot():
    bot = MagicMock()
    for m in ('send_message', 'edit_message_caption', 'edit_message_text',
              'edit_message_reply_markup'):
        setattr(bot, m, AsyncMock())
    return bot


def _msg_update(text, chat_id=-100, thread_id=10138):
    """Сообщение админа из той же ветки, где нажимали кнопку."""
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=chat_id)
    upd.effective_user = MagicMock(id=555, full_name='Dobe', username='dobe')
    upd.message = MagicMock(text=text, message_thread_id=thread_id)
    upd.message.reply_text = AsyncMock()
    return upd


def _await(mode, key, chat_id=-100, thread_id=10138):
    return {'mode': mode, 'key': key, 'chat_id': chat_id, 'thread_id': thread_id}


def _query(data):
    q = MagicMock(data=data)
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_caption = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock(text='Пост', caption=None, chat_id=-100,
                          message_id=55, message_thread_id=10138)
    return q


def _guest_update(q):
    """Update от обычного участника ветки (не админа)."""
    upd = MagicMock(callback_query=q)
    upd.effective_user = MagicMock(id=777001, full_name='Гость Вася', username='vasya')
    return upd


def _real_is_admin(u):
    """Честная проверка админа для гостевых тестов (env мокает is_admin=True)."""
    return bool(getattr(u, 'effective_user', None)) and \
        u.effective_user.id in anime_news_bot._all_admin_ids()


class TestParseScheduleTime:
    def test_relative_hours(self, env):
        w = _parse_schedule_time('+2ч')
        mins = (w - datetime.now(timezone.utc)).total_seconds() / 60
        assert 115 < mins < 125

    def test_relative_minutes(self, env):
        w = _parse_schedule_time('+30м')
        mins = (w - datetime.now(timezone.utc)).total_seconds() / 60
        assert 25 < mins < 35

    def test_tomorrow_keeps_local_hour(self, env):
        w = _parse_schedule_time('завтра 10:00')
        local = _utc_to_local(w)
        assert local.hour == 10 and local.minute == 0
        assert (local.date() - _local_now().date()).days == 1

    def test_timezone_applied(self, env):
        # 12:00 по МСК (UTC+3) должно стать 09:00 UTC — сервер живёт в UTC
        w = _parse_schedule_time('завтра 12:00')
        assert w.hour == 9
        assert w.tzinfo is not None

    def test_date_with_time(self, env):
        w = _parse_schedule_time('12.09 18:30')
        local = _utc_to_local(w)
        assert (local.day, local.month, local.hour, local.minute) == (12, 9, 18, 30)

    def test_bare_time_always_future(self, env):
        w = _parse_schedule_time('00:01')
        assert w > datetime.now(timezone.utc)

    def test_garbage_rejected(self, env):
        assert _parse_schedule_time('когда-нибудь') is None
        assert _parse_schedule_time('99:99') is None
        assert _parse_schedule_time('') is None

    def test_past_rejected(self, env):
        assert _parse_schedule_time('01.01.2020 10:00') is None


class TestHumanDelta:
    def test_rounds_to_nearest_minute(self, env):
        # +2ч минус миллисекунды не должно превращаться в «1 ч 59 мин»
        w = datetime.now(timezone.utc) + timedelta(hours=2) - timedelta(milliseconds=5)
        assert _human_delta(w) == '2 ч'

    def test_days(self, env):
        w = datetime.now(timezone.utc) + timedelta(days=1, hours=3)
        assert _human_delta(w) == '1 д 3 ч'


class TestScheduledPosts:
    def test_add_sorted_and_due(self, tmp_path):
        sp = ScheduledPosts(tmp_path / 's.json')
        sp.add({'title': 'Будущий', 'link': 'a', 'published_parsed': 'STRIP'},
               datetime.now(timezone.utc) + timedelta(hours=2))
        sp.add({'title': 'Пора', 'link': 'b'},
               datetime.now(timezone.utc) - timedelta(minutes=1))
        assert [n['title'] for _, n, _ in sp.all()] == ['Пора', 'Будущий']
        due = sp.due()
        assert len(due) == 1 and due[0][1]['title'] == 'Пора'

    def test_published_parsed_stripped(self, tmp_path):
        sp = ScheduledPosts(tmp_path / 's.json')
        k = sp.add({'title': 'T', 'published_parsed': 'X'},
                   datetime.now(timezone.utc) + timedelta(hours=1))
        assert 'published_parsed' not in sp.get(k)

    def test_persists_across_restart(self, tmp_path):
        p = tmp_path / 's.json'
        ScheduledPosts(p).add({'title': 'T', 'link': 'a'},
                              datetime.now(timezone.utc) + timedelta(hours=1))
        assert len(ScheduledPosts(p).all()) == 1

    def test_pop_and_tries(self, tmp_path):
        sp = ScheduledPosts(tmp_path / 's.json')
        k = sp.add({'title': 'T'}, datetime.now(timezone.utc) + timedelta(hours=1))
        assert sp.mark_try(k) == 1 and sp.mark_try(k) == 2
        assert sp.pop(k)['title'] == 'T'
        assert sp.get(k) is None


class TestScheduleButton:
    def test_asks_for_time(self, env):
        key = env.pending_posts.add({'title': 'Новость', 'link': 'x', 'lang': 'ru'})
        q = _query(f'sch:{key}')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert ctx.user_data['await_input'] == _await('schedule', key)
        hint = ctx.bot.send_message.await_args.kwargs['text']
        assert 'Во сколько опубликовать' in hint
        assert ctx.bot.send_message.await_args.kwargs['reply_to_message_id'] == 55

    def test_stale_key_alerts(self, env):
        q = _query('sch:999')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert q.answer.await_args.kwargs.get('show_alert') is True
        assert 'await_input' not in ctx.user_data


class TestAwaitingInputSchedule:
    def test_valid_time_schedules_and_clears_buttons(self, env):
        key = env.pending_posts.add({'title': 'Новость', 'link': 'x', 'lang': 'ru'})
        env.pending_posts.set_preview(key, -100, 55)
        upd = _msg_update('+2ч')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert len(env.scheduled_posts.all()) == 1
        assert env.pending_posts.get(key) is None
        assert 'await_input' not in ctx.user_data
        assert 'через 2 ч' in upd.message.reply_text.await_args.args[0]
        # пометка проставлена и кнопки сняты (важно: до pop, иначе превью не найти)
        cap = ctx.bot.edit_message_caption.await_args.kwargs
        assert 'В отложке на' in cap['caption']
        assert cap['reply_markup'] is None

    def test_bad_time_keeps_state(self, env):
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        upd = _msg_update('абырвалг')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert 'await_input' in ctx.user_data     # ждём корректный ввод
        assert env.scheduled_posts.all() == []
        assert 'Не понял время' in upd.message.reply_text.await_args.args[0]

    def test_passthrough_when_not_awaiting(self, env):
        upd = _msg_update('📰 Новости')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(env.awaiting_input_handler(upd, ctx))   # без ApplicationHandlerStop
        assert upd.message.reply_text.await_count == 0


class TestEditFlow:
    def test_edit_button_asks_text(self, env):
        key = env.pending_posts.add({'title': 'Original', 'link': 'x', 'lang': 'ru'})
        q = _query(f'edit:{key}')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert ctx.user_data['await_input'] == _await('edit', key)
        assert 'новый текст поста' in ctx.bot.send_message.await_args.kwargs['text']

    def test_edited_text_saved_and_used(self, env):
        key = env.pending_posts.add({'title': 'Original', 'link': 'x',
                                     'summary': 'Some.', 'lang': 'ru'})
        env.pending_posts.set_preview(key, -100, 55)
        upd = _msg_update('Мой текст.\n\nВторая строка.')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('edit', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        saved = env.pending_posts.get(key)
        assert saved['_edited_text'] == 'Мой текст.\n\nВторая строка.'
        # ключевое: пост уйдёт именно правленым текстом
        assert env.format_news_short(saved) == 'Мой текст.\n\nВторая строка.'
        assert ctx.bot.edit_message_caption.await_count == 1

    def test_format_ignores_empty_edit(self, env):
        news = {'title': 'Title', 'summary': '', 'lang': 'ru',
                'published_parsed': None, '_edited_text': ''}
        assert 'Title' in env.format_news_short(news)


class TestPublishScheduledJob:
    def test_publishes_due_only(self, env):
        env.scheduled_posts.add({'title': 'Пора', 'link': 'a'},
                                datetime.now(timezone.utc) - timedelta(seconds=5))
        env.scheduled_posts.add({'title': 'Рано', 'link': 'b'},
                                datetime.now(timezone.utc) + timedelta(hours=5))
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock(return_value=True)) as sp, \
             patch.object(env, 'notify_admin', new=AsyncMock()), \
             patch.object(env.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(env.publish_scheduled(ctx))
        assert sp.await_count == 1
        assert sp.await_args.args[2] == env.CHANNEL_ID
        left = env.scheduled_posts.all()
        assert len(left) == 1 and left[0][1]['title'] == 'Рано'

    def test_drops_after_max_tries(self, env):
        k = env.scheduled_posts.add({'title': 'Сломанный', 'link': 'a'},
                                    datetime.now(timezone.utc) - timedelta(seconds=5))
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock(return_value=False)), \
             patch.object(env, 'notify_admin', new=AsyncMock()) as na, \
             patch.object(env.asyncio, 'sleep', new=AsyncMock()):
            for _ in range(ScheduledPosts.MAX_TRIES):
                asyncio.run(env.publish_scheduled(ctx))
        assert env.scheduled_posts.get(k) is None
        assert 'снят после' in na.await_args.args[1]

    def test_noop_when_empty(self, env):
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock()) as sp:
            asyncio.run(env.publish_scheduled(ctx))
        assert sp.await_count == 0


class TestScheduledListButtons:
    def test_publish_now(self, env):
        k = env.scheduled_posts.add({'title': 'X', 'link': 'a'},
                                    datetime.now(timezone.utc) + timedelta(hours=3))
        q = _query(f'snow:{k}')
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock(return_value=True)):
            asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert env.scheduled_posts.get(k) is None
        q.answer.assert_awaited_with('📢 Опубликовано!')

    def test_cancel(self, env):
        k = env.scheduled_posts.add({'title': 'Y', 'link': 'b'},
                                    datetime.now(timezone.utc) + timedelta(hours=3))
        q = _query(f'scan:{k}')
        ctx = MagicMock(bot=_bot())
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert env.scheduled_posts.get(k) is None
        q.answer.assert_awaited_with('Снято с отложки')

    def test_stale_key(self, env):
        q = _query('snow:999')
        ctx = MagicMock(bot=_bot())
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert q.answer.await_args.kwargs.get('show_alert') is True


class TestModerationMarkup:
    def test_four_buttons(self):
        markup = anime_news_bot._moderation_markup('7')
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert data == ['pub:7', 'sch:7', 'edit:7', 'dis:7']


class TestTzCommand:
    def test_sets_offset(self, env):
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock(args=['5'])
        asyncio.run(env.tz_command(upd, ctx))
        assert env.settings.tz_offset == 5

    def test_rejects_out_of_range(self, env):
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(env.tz_command(upd, MagicMock(args=['99'])))
        assert 'от -12 до +14' in upd.message.reply_text.await_args.args[0]


class TestCancelCommand:
    def test_clears_state(self, env):
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock(user_data={'await_input': {'mode': 'edit', 'key': '1'}})
        asyncio.run(env.cancel_command(upd, ctx))
        assert 'await_input' not in ctx.user_data
        assert 'Отменил' in upd.message.reply_text.await_args.args[0]

    def test_nothing_to_cancel(self, env):
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(env.cancel_command(upd, MagicMock(user_data={})))
        assert 'Нечего отменять' in upd.message.reply_text.await_args.args[0]


class TestThreadIsolation:
    """Бот должен реагировать только в той ветке, где нажали кнопку."""

    def test_message_from_other_thread_ignored(self, env):
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        upd = _msg_update('+2ч', thread_id=999)          # другая ветка
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        asyncio.run(env.awaiting_input_handler(upd, ctx))  # без ApplicationHandlerStop
        assert env.scheduled_posts.all() == []
        assert 'await_input' in ctx.user_data              # ждём ответ в своей ветке
        assert upd.message.reply_text.await_count == 0     # чужим не отвечаем

    def test_message_from_other_chat_ignored(self, env):
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        upd = _msg_update('+2ч', chat_id=-777)           # другой чат
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert env.scheduled_posts.all() == []
        assert upd.message.reply_text.await_count == 0

    def test_same_thread_accepted(self, env):
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        upd = _msg_update('+2ч')                          # та же ветка
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert len(env.scheduled_posts.all()) == 1

    def test_await_ctx_records_place(self):
        msg = MagicMock(chat_id=-100, message_thread_id=10138)
        assert anime_news_bot._await_ctx('edit', '3', msg) == {
            'mode': 'edit', 'key': '3', 'chat_id': -100, 'thread_id': 10138}


class TestDetectLang:
    def test_russian_with_latin_titles(self):
        assert anime_news_bot._detect_lang(
            'Тизер и кадры ко 2-й серии. Снимает Kinema Citrus.') == 'ru'

    def test_italian_translated(self):
        # Реальный пост @VanitasNews — раньше уходил без перевода
        assert anime_news_bot._detect_lang(
            "Teaser visual e PV dell'adattamento anime della light novel Mercedes") is None

    def test_english_translated(self):
        assert anime_news_bot._detect_lang('Chainsaw Man Season 2 announced by MAPPA') is None

    def test_empty(self):
        assert anime_news_bot._detect_lang('') is None

    def test_tg_parser_sets_lang_by_content(self, monkeypatch):
        html = ('<div class="tgme_widget_message" data-post="c/1">'
                '<div class="tgme_widget_message_text">'
                "Il manga SAKAMOTO DAYS con il capitolo in uscita la prossima settimana"
                '</div><time datetime="2026-07-24T11:00:00+00:00"></time></div>'
                '<div class="tgme_widget_message" data-post="c/2">'
                '<div class="tgme_widget_message_text">'
                'Новый сезон аниме выйдет весной по данным студии'
                '</div><time datetime="2026-07-24T12:00:00+00:00"></time></div>')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(status_code=200, text=html))
        monkeypatch.setattr(anime_news_bot, '_is_too_old', lambda *_: False)
        posts = anime_news_bot.get_telegram_channel('c', 'TG: T')
        assert posts[0]['lang'] is None      # итальянский → переводим
        assert posts[1]['lang'] == 'ru'      # русский → не переводим


class TestScheduledAuthorAndCard:
    def test_author_saved(self, tmp_path):
        sp = ScheduledPosts(tmp_path / 's.json')
        k = sp.add({'title': 'T'}, datetime.now(timezone.utc) + timedelta(hours=1),
                   by={'id': 555, 'name': 'Dobe'})
        assert sp.meta(k)['by'] == {'id': 555, 'name': 'Dobe'}
        assert sp.meta(k)['at'] is not None

    def test_author_recorded_from_update(self, env):
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        upd = _msg_update('+2ч')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('schedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        skey = env.scheduled_posts.all()[0][0]
        assert env.scheduled_posts.meta(skey)['by']['name'] == 'Dobe'

    def test_card_has_details(self, env):
        news = {'title': 'Sakamoto Days новости', 'link': 'https://t.me/c/1',
                'source': 'TG: VanitasNews'}
        meta = {'by': {'name': 'Dobe'}, 'at': datetime.now(timezone.utc)}
        card = anime_news_bot._post_card(news, meta)
        assert 'Sakamoto Days' in card
        assert 'TG: VanitasNews' in card
        assert 'Dobe' in card
        assert 'https://t.me/c/1' in card

    def test_card_marks_edited(self, env):
        card = anime_news_bot._post_card({'_edited_text': 'Правленый', 'title': 'X'}, {})
        assert 'Правленый' in card and 'правился вручную' in card


class TestJobResilience:
    def test_exception_does_not_kill_job(self, env):
        """Раньше исключение молча убивало джоб и пост висел вечно."""
        env.scheduled_posts.add({'title': 'Проблемный', 'link': 'a'},
                                datetime.now(timezone.utc) - timedelta(seconds=5))
        ctx = MagicMock(bot=_bot())
        boom = AsyncMock(side_effect=RuntimeError('что-то сломалось'))
        with patch.object(env, '_send_post', new=boom), \
             patch.object(env, 'notify_admin', new=AsyncMock()) as na, \
             patch.object(env.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(env.publish_scheduled(ctx))      # не должно упасть
        assert len(env.scheduled_posts.all()) == 1       # остался на повтор
        text = na.await_args.args[1]
        assert 'RuntimeError' in text and 'что-то сломалось' in text

    def test_notifies_on_each_attempt(self, env):
        env.scheduled_posts.add({'title': 'T', 'link': 'a'},
                                datetime.now(timezone.utc) - timedelta(seconds=5))
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock(return_value=False)), \
             patch.object(env, 'notify_admin', new=AsyncMock()) as na, \
             patch.object(env.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(env.publish_scheduled(ctx))
        assert na.await_count == 1
        assert 'попытка 1/3' in na.await_args.args[1]

    def test_success_notification_has_author(self, env):
        env.scheduled_posts.add({'title': 'Готов', 'link': 'https://t.me/c/9',
                                 'source': 'TG: X'},
                                datetime.now(timezone.utc) - timedelta(seconds=5),
                                by={'id': 555, 'name': 'Dobe'})
        ctx = MagicMock(bot=_bot())
        with patch.object(env, '_send_post', new=AsyncMock(return_value=True)), \
             patch.object(env, 'notify_admin', new=AsyncMock()) as na, \
             patch.object(env.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(env.publish_scheduled(ctx))
        text = na.await_args.args[1]
        assert 'Опубликован отложенный пост' in text
        assert 'Dobe' in text and 'TG: X' in text and 'https://t.me/c/9' in text


class TestJobKwargs:
    def test_misfire_grace_is_generous(self):
        """APScheduler по умолчанию выбрасывает тики, опоздавшие на 1с."""
        assert anime_news_bot.JOB_KWARGS['misfire_grace_time'] >= 600


class TestScheduledCard:
    """Карточка в /scheduled: подробности ДО публикации."""

    def _news(self):
        return {
            'title': 'Il manga SAKAMOTO DAYS',
            '_edited_text': 'Манга SAKAMOTO DAYS приблизится к кульминации.',
            'link': 'https://t.me/VanitasNews/1234',
            'source': 'TG: VanitasNews',
            'images': ['https://cdn.tg/1.jpg', 'https://cdn.tg/2.jpg',
                       'https://cdn.tg/1_thumb.jpg'],
            'video': 'https://cdn.tg/v.mp4',
        }

    def test_full_card(self, env):
        k = env.scheduled_posts.add(
            self._news(), datetime.now(timezone.utc) + timedelta(hours=2, minutes=15),
            by={'id': 555, 'name': 'Dobe'})
        card = anime_news_bot._post_card(
            env.scheduled_posts.get(k), env.scheduled_posts.meta(k),
            countdown=True, with_body=True)
        assert 'SAKAMOTO DAYS приблизится' in card   # текст, который уйдёт
        assert 'TG: VanitasNews' in card
        assert 'Dobe' in card
        assert 'через 2 ч' in card
        assert '2 фото + видео' in card              # медиа после дедупа
        assert 'правился вручную' in card

    def test_overdue_marked(self, env):
        k = env.scheduled_posts.add({'title': 'Overdue', 'link': 'x'},
                                    datetime.now(timezone.utc) - timedelta(minutes=20))
        card = anime_news_bot._post_card(
            env.scheduled_posts.get(k), env.scheduled_posts.meta(k),
            countdown=True, with_body=True)
        assert 'время наступило' in card

    def test_scheduled_command_shows_overview(self, env):
        env.scheduled_posts.add(self._news(),
                                datetime.now(timezone.utc) + timedelta(hours=1),
                                by={'id': 1, 'name': 'Dobe'})
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(env.scheduled_command(upd, MagicMock()))
        text = upd.message.reply_text.await_args.args[0]
        assert 'В отложке: 1' in text
        assert 'Dobe' in text
        assert upd.message.reply_text.await_args.kwargs.get('reply_markup')


class TestMediaSummary:
    def test_photos_and_video(self):
        s = anime_news_bot._media_summary(
            {'images': ['https://c/1.jpg', 'https://c/2.jpg'], 'video': 'v'})
        assert s == '2 фото + видео'

    def test_dedup_applied(self):
        s = anime_news_bot._media_summary(
            {'images': ['https://c/p_full.jpg', 'https://c/p_thumb.jpg']})
        assert s == '1 фото'

    def test_none(self):
        assert anime_news_bot._media_summary({'images': [], 'video': None}) == 'нет'


class TestScheduledStatusBlock:
    def test_job_alive(self, env):
        ctx = MagicMock()
        ctx.application.job_queue.get_jobs_by_name.return_value = [
            MagicMock(next_t=datetime.now(timezone.utc))]
        block = anime_news_bot._scheduled_status_block(ctx)
        assert 'РАБОТАЕТ' in block

    def test_job_missing_warns(self, env):
        ctx = MagicMock()
        ctx.application.job_queue.get_jobs_by_name.return_value = []
        block = anime_news_bot._scheduled_status_block(ctx)
        assert 'НЕ ЗАРЕГИСТРИРОВАН' in block


class TestChannelPublishCdnPhotos:
    """Сценарий из продовых логов: отложенный TG-пост с фото на cdn-telegram.org
    падал в канал 3 попытки подряд (webpage_curl_failed / Wrong type of the web
    page content). Теперь такие картинки заранее качаются байтами."""

    def _news(self):
        return {
            'title': 'Дженна Ортега и Роуз Бирн сыграют главные роли',
            'link': 'https://t.me/kinonews/999', 'source': 'TG: KinoNews',
            'summary': '', 'lang': 'ru',
            'images': ['https://cdn4.cdn-telegram.org/file/a.jpg',
                       'https://cdn4.cdn-telegram.org/file/b.jpg',
                       'https://cdn4.cdn-telegram.org/file/c.jpg'],
            'video': None,
        }

    def test_scheduled_tg_post_published_first_try(self, env, monkeypatch):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(tz_offset=3, translator_engine='google',
                                      video_enabled=True, require_image=True))
        env.scheduled_posts.add(self._news(),
                                datetime.now(timezone.utc) - timedelta(seconds=5),
                                by={'id': 555, 'name': 'Dobe'})

        def album_gate(chat_id=None, media=None, **kw):
            # Bot API отвергает cdn-telegram по URL, принимает байты
            for m in media:
                if isinstance(getattr(m, 'media', None), str) and 'cdn-telegram' in m.media:
                    raise TelegramError('webpage_curl_failed')
            return [MagicMock()]

        fake_img = FakeHTTPResponse(b'JPEG', headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: fake_img)
        bot = MagicMock()
        bot.send_media_group = AsyncMock(side_effect=album_gate)
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na, \
             patch.object(anime_news_bot.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(anime_news_bot.publish_scheduled(MagicMock(bot=bot)))
        assert env.scheduled_posts.all() == []          # ушёл с первой попытки
        media = bot.send_media_group.await_args.kwargs['media']
        assert len(media) == 3                          # все фото на месте
        assert not any(isinstance(m.media, str) and 'cdn-telegram' in m.media
                       for m in media)                  # URL заменены байтами
        assert 'Опубликован отложенный пост' in na.await_args.args[1]

    def test_single_photo_bytes_retry_in_channel(self, env, monkeypatch):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(video_enabled=True, require_image=True,
                                      translator_engine='google', tz_offset=3))
        fake_img = FakeHTTPResponse(b'JPEG', headers={'Content-Type': 'image/jpeg'})
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: fake_img)
        bot = MagicMock()
        bot.send_photo = AsyncMock(
            side_effect=[TelegramError('Wrong type of the web page content'), None])
        news = {'title': 'T', 'link': 'https://s/a', 'summary': '', 'lang': 'ru',
                'images': ['https://other-cdn.com/pic.jpg'], 'video': None,
                'source': 'X'}
        ok = asyncio.run(anime_news_bot._send_post(bot, news, -100500, None))
        assert ok is True
        assert bot.send_photo.await_count == 2
        # второй вызов — байтами
        assert bot.send_photo.await_args.kwargs['photo'] == b'JPEG'


class TestUnreachableAdmin:
    def test_single_warning_no_error_spam(self, env, monkeypatch):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(extra_admins=[8421105674]))
        anime_news_bot._unreachable_admins.clear()
        bot = MagicMock()
        async def send(chat_id=None, text=None, **kwargs):
            if chat_id == 8421105674:
                raise TelegramError("Forbidden: bot can't initiate conversation with a user")
        bot.send_message = AsyncMock(side_effect=send)
        warnings, errors = [], []
        with patch.object(anime_news_bot.logger, 'warning',
                          side_effect=lambda m: warnings.append(m)), \
             patch.object(anime_news_bot.logger, 'error',
                          side_effect=lambda m: errors.append(m)):
            for _ in range(5):
                asyncio.run(anime_news_bot.notify_admin(bot, 'hi'))
        assert len([w for w in warnings if '8421105674' in w]) == 1
        assert '/start' in warnings[0]
        assert errors == []

    def test_recovers_after_start(self, env, monkeypatch):
        from telegram.error import TelegramError
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(extra_admins=[111]))
        anime_news_bot._unreachable_admins.clear()
        bot = MagicMock()
        state = {'blocked': True}
        async def send(chat_id=None, text=None, **kwargs):
            if chat_id == 111 and state['blocked']:
                raise TelegramError("Forbidden: bot can't initiate conversation with a user")
        bot.send_message = AsyncMock(side_effect=send)
        asyncio.run(anime_news_bot.notify_admin(bot, 'hi'))
        assert 111 in anime_news_bot._unreachable_admins
        state['blocked'] = False                       # админ нажал /start
        asyncio.run(anime_news_bot.notify_admin(bot, 'hi'))
        assert 111 not in anime_news_bot._unreachable_admins



class TestOpenModeration:
    """Кнопки под постами доступны всем участникам ветки (open_moderation)."""

    @pytest.fixture
    def open_env(self, env, monkeypatch):
        env.settings.open_moderation = True
        env.settings.extra_admins = []
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        return env

    def test_guest_publishes_and_admins_notified(self, open_env):
        key = open_env.pending_posts.add({'title': 'Новость', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')
        with patch.object(anime_news_bot, '_send_post',
                          new=AsyncMock(return_value=True)) as sp, \
             patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot.settings_callback(
                _guest_update(q), MagicMock(bot=_bot())))
        assert sp.await_count == 1
        note = na.await_args.args[1]
        assert 'Гость Вася' in note and 'опубликовал в канал' in note

    def test_guest_schedules_with_author(self, open_env):
        key = open_env.pending_posts.add({'title': 'Пост', 'link': 'x', 'lang': 'ru'})
        q = _query(f'sch:{key}')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(anime_news_bot.settings_callback(_guest_update(q), ctx))
        assert ctx.user_data['await_input']['mode'] == 'schedule'
        upd = _msg_update('+2ч')
        upd.effective_user = MagicMock(id=777001, full_name='Гость Вася', username='v')
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            with pytest.raises(ApplicationHandlerStop):
                asyncio.run(anime_news_bot.awaiting_input_handler(upd, ctx))
        skey = open_env.scheduled_posts.all()[0][0]
        assert open_env.scheduled_posts.meta(skey)['by']['name'] == 'Гость Вася'
        assert any('отложил пост' in c.args[1] for c in na.await_args_list)

    def test_guest_edit_notifies(self, open_env):
        key = open_env.pending_posts.add({'title': 'Оригинал', 'link': 'x', 'lang': 'ru'})
        ctx = MagicMock(bot=_bot(),
                        user_data={'await_input': _await('edit', key)})
        upd = _msg_update('Новый текст поста.')
        upd.effective_user = MagicMock(id=777001, full_name='Гость Вася', username='v')
        with patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            with pytest.raises(ApplicationHandlerStop):
                asyncio.run(anime_news_bot.awaiting_input_handler(upd, ctx))
        assert open_env.pending_posts.get(key)['_edited_text'] == 'Новый текст поста.'
        assert any('изменил текст' in c.args[1] for c in na.await_args_list)

    def test_closed_mode_denies_guest(self, env):
        env.settings.open_moderation = False
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')
        with patch.object(anime_news_bot, '_send_post', new=AsyncMock()) as sp:
            asyncio.run(anime_news_bot.settings_callback(
                _guest_update(q), MagicMock(bot=_bot())))
        assert sp.await_count == 0
        assert q.answer.await_args.kwargs.get('show_alert') is True

    def test_settings_menu_still_admin_only(self, open_env):
        q = _query('settings:toggle_quiet')
        asyncio.run(anime_news_bot.settings_callback(
            _guest_update(q), MagicMock(bot=_bot())))
        assert q.answer.await_args.kwargs.get('show_alert') is True

    def test_admin_actions_not_reported(self, open_env):
        key = open_env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')
        upd = MagicMock(callback_query=q)
        upd.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID, full_name='Dobe')
        with patch.object(anime_news_bot, '_send_post',
                          new=AsyncMock(return_value=True)), \
             patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()) as na:
            asyncio.run(anime_news_bot.settings_callback(upd, MagicMock(bot=_bot())))
        assert na.await_count == 0

    def test_open_moderation_persists(self, tmp_path):
        from anime_news_bot import BotSettings
        p = tmp_path / 's.json'
        s = BotSettings(p)
        assert s.open_moderation is False         # безопасно: только админы
        s.open_moderation = True
        assert BotSettings(p).open_moderation is True


class TestPlaceGate:
    """Кнопки модерации работают ТОЛЬКО в предназначенной ветке."""

    def test_button_in_foreign_chat_denied(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')
        q.message.chat_id = -777                     # чужой чат
        with patch.object(anime_news_bot, '_send_post', new=AsyncMock()) as sp:
            asyncio.run(anime_news_bot.settings_callback(
                _guest_update(q), MagicMock(bot=_bot())))
        assert sp.await_count == 0
        assert q.answer.await_args.kwargs.get('show_alert') is True
        assert 'ветке' in q.answer.await_args.args[0]

    def test_button_in_foreign_thread_denied(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'dis:{key}')
        q.message.message_thread_id = 999            # чужая ветка той же группы
        asyncio.run(anime_news_bot.settings_callback(
            _guest_update(q), MagicMock(bot=_bot())))
        assert env.pending_posts.get(key) is not None  # пост не тронут
        assert q.answer.await_args.kwargs.get('show_alert') is True

    def test_even_admin_denied_outside_thread(self, env, monkeypatch):
        # Защита места важнее прав: пересланные кнопки не работают нигде
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')
        q.message.chat_id = -777
        upd = MagicMock(callback_query=q)
        upd.effective_user = MagicMock(id=anime_news_bot.ADMIN_ID, full_name='Dobe')
        with patch.object(anime_news_bot, '_send_post', new=AsyncMock()) as sp:
            asyncio.run(anime_news_bot.settings_callback(upd, MagicMock(bot=_bot())))
        assert sp.await_count == 0

    def test_guest_in_right_thread_allowed(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        key = env.pending_posts.add({'title': 'N', 'link': 'x', 'lang': 'ru'})
        q = _query(f'pub:{key}')                     # правильные чат и ветка
        with patch.object(anime_news_bot, '_send_post',
                          new=AsyncMock(return_value=True)) as sp, \
             patch.object(anime_news_bot, 'notify_admin', new=AsyncMock()):
            asyncio.run(anime_news_bot.settings_callback(
                _guest_update(q), MagicMock(bot=_bot())))
        assert sp.await_count == 1


class TestPrivateGate:
    """Личка бота — только для админов."""

    def _pm_update(self, uid=777001, text='привет'):
        upd = MagicMock()
        upd.effective_chat = MagicMock(id=uid, type='private')
        upd.effective_user = MagicMock(id=uid, full_name='Гость')
        upd.message = MagicMock(text=text)
        upd.message.reply_text = AsyncMock()
        return upd

    def test_stranger_gets_one_hint_then_silence(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        anime_news_bot._private_denied.clear()
        upd = self._pm_update()
        for _ in range(3):
            with pytest.raises(ApplicationHandlerStop):
                asyncio.run(anime_news_bot.private_gate_handler(upd, MagicMock()))
        assert upd.message.reply_text.await_count == 1   # подсказка ровно одна
        assert 'только администраторам' in upd.message.reply_text.await_args.args[0]

    def test_admin_passes(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        upd = self._pm_update(uid=anime_news_bot.ADMIN_ID)
        asyncio.run(anime_news_bot.private_gate_handler(upd, MagicMock()))  # без Stop
        assert upd.message.reply_text.await_count == 0

    def test_group_messages_untouched(self, env, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'is_admin', _real_is_admin)
        upd = self._pm_update()
        upd.effective_chat = MagicMock(id=-100, type='supergroup')
        asyncio.run(anime_news_bot.private_gate_handler(upd, MagicMock()))  # без Stop
        assert upd.message.reply_text.await_count == 0


class TestOpenModerationToggle:
    def test_toggle_button_admin_only_zone(self, env):
        # env мокает is_admin=True — админ переключает
        env.settings.open_moderation = True
        q = _query('settings:toggle_open')
        asyncio.run(anime_news_bot.settings_callback(
            MagicMock(callback_query=q), MagicMock(bot=_bot())))
        assert env.settings.open_moderation is False
        q2 = _query('settings:toggle_open')
        asyncio.run(anime_news_bot.settings_callback(
            MagicMock(callback_query=q2), MagicMock(bot=_bot())))
        assert env.settings.open_moderation is True


class TestScheduledOverview:
    """Очередь одним сообщением: группировка по дням, номера, действия."""

    @pytest.fixture
    def queue(self, env):
        now = datetime.now(timezone.utc)
        keys = []
        for when, title, who in (
                (now - timedelta(minutes=8), 'Просроченный пост', 'Dobe'),
                (now + timedelta(hours=2), 'Сегодняшний пост', 'Dobe'),
                (now + timedelta(days=1), 'Завтрашний пост', 'Гость Вася'),
                (now + timedelta(days=5), 'Дальний пост', 'Петя')):
            keys.append(env.scheduled_posts.add(
                {'title': title, 'link': 'https://t.me/x/1', 'source': 'TG',
                 'images': ['a']}, when, by={'id': 1, 'name': who}))
        return env, keys

    def test_groups_by_day(self, queue):
        env, _ = queue
        text, _markup = env._scheduled_overview()
        assert 'Сегодня' in text and 'Завтра' in text
        assert 'Просрочено' in text or 'пора' in text

    def test_shows_author_and_time(self, queue):
        env, _ = queue
        text, _ = env._scheduled_overview()
        assert 'Гость Вася' in text
        assert 'через' in text

    def test_shows_ripe_count(self, queue):
        env, _ = queue
        text, _ = env._scheduled_overview()
        assert 'ждёт публикации' in text

    def test_number_buttons(self, queue):
        env, keys = queue
        _text, markup = env._scheduled_overview()
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert '1' in labels and '4' in labels
        assert any('Очистить' in l for l in labels)

    def test_empty_queue(self, env):
        text, markup = env._scheduled_overview()
        assert 'нет' in text and markup is None

    def test_fits_message_limit(self, env):
        now = datetime.now(timezone.utc)
        for i in range(60):
            env.scheduled_posts.add(
                {'title': 'Очень длинный заголовок поста номер %d' % i,
                 'link': 'https://t.me/x/1'},
                now + timedelta(hours=i), by={'id': 1, 'name': 'Dobe'})
        text, _ = env._scheduled_overview()
        assert len(text) <= anime_news_bot.TG_TEXT_LIMIT
        assert 'и ещё' in text

    def test_tries_shown(self, env):
        key = env.scheduled_posts.add({'title': 'T', 'link': 'x'},
                                      datetime.now(timezone.utc) + timedelta(hours=1))
        env.scheduled_posts.mark_try(key)
        env.scheduled_posts.mark_try(key)
        text, _ = env._scheduled_overview()
        assert 'попыток: 2' in text


class TestScheduledDetail:
    def test_shows_card_with_actions(self, env):
        key = env.scheduled_posts.add(
            {'title': 'Опенинг Bleach', 'link': 'https://t.me/x/1',
             'source': 'TG', 'images': ['a']},
            datetime.now(timezone.utc) + timedelta(hours=2),
            by={'id': 1, 'name': 'Dobe'})
        text, markup = env._scheduled_detail(key)
        assert 'Dobe' in text and 'Опенинг Bleach' in text
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert any('Перенести' in l for l in labels)
        assert any('К списку' in l for l in labels)

    def test_missing_post(self, env):
        text, markup = env._scheduled_detail('999')
        assert 'уже нет' in text
        assert markup is not None


class TestReschedule:
    def test_storage_reschedule(self, env):
        key = env.scheduled_posts.add({'title': 'T'},
                                      datetime.now(timezone.utc) + timedelta(hours=1))
        env.scheduled_posts.mark_try(key)
        new_time = datetime.now(timezone.utc) + timedelta(days=2)
        assert env.scheduled_posts.reschedule(key, new_time) is True
        assert env.scheduled_posts.meta(key)['tries'] == 0     # попытки сброшены
        assert abs((env.scheduled_posts.when(key) - new_time).total_seconds()) < 2

    def test_reschedule_missing(self, env):
        assert env.scheduled_posts.reschedule('999',
                                              datetime.now(timezone.utc)) is False

    def test_button_asks_time(self, env):
        key = env.scheduled_posts.add({'title': 'T'},
                                      datetime.now(timezone.utc) + timedelta(hours=1))
        q = _query(f'sedit:{key}')
        ctx = MagicMock(bot=_bot(), user_data={})
        asyncio.run(env.settings_callback(MagicMock(callback_query=q), ctx))
        assert ctx.user_data['await_input'] == _await('reschedule', key)
        assert 'перенести' in ctx.bot.send_message.await_args.kwargs['text'].lower()

    def test_new_time_applied(self, env):
        key = env.scheduled_posts.add({'title': 'T'},
                                      datetime.now(timezone.utc) + timedelta(hours=1))
        old = env.scheduled_posts.when(key)
        upd = _msg_update('завтра 09:00')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('reschedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert env.scheduled_posts.when(key) != old
        assert env.scheduled_posts.get(key) is not None      # пост на месте
        assert 'Перенёс' in upd.message.reply_text.await_args.args[0]

    def test_bad_time_keeps_waiting(self, env):
        key = env.scheduled_posts.add({'title': 'T'},
                                      datetime.now(timezone.utc) + timedelta(hours=1))
        upd = _msg_update('абырвалг')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('reschedule', key)})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert 'await_input' in ctx.user_data

    def test_vanished_post(self, env):
        upd = _msg_update('18:30')
        ctx = MagicMock(bot=_bot(), user_data={'await_input': _await('reschedule', '999')})
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(env.awaiting_input_handler(upd, ctx))
        assert 'уже нет' in upd.message.reply_text.await_args.args[0]


class TestClearQueue:
    def test_asks_confirmation(self, env):
        env.scheduled_posts.add({'title': 'T'},
                                datetime.now(timezone.utc) + timedelta(hours=1))
        q = _query('sclear')
        asyncio.run(env.settings_callback(MagicMock(callback_query=q),
                                          MagicMock(bot=_bot())))
        assert 'необратимо' in q.edit_message_text.await_args.args[0]
        assert len(env.scheduled_posts.all()) == 1          # ещё ничего не сняли

    def test_confirmed_clears(self, env):
        for i in range(3):
            env.scheduled_posts.add({'title': f'T{i}'},
                                    datetime.now(timezone.utc) + timedelta(hours=i + 1))
        q = _query('sclearyes')
        asyncio.run(env.settings_callback(MagicMock(callback_query=q),
                                          MagicMock(bot=_bot())))
        assert env.scheduled_posts.all() == []
        assert 'Снято постов: 3' in q.answer.await_args.args[0]

    def test_empty_queue_noop(self, env):
        q = _query('sclear')
        asyncio.run(env.settings_callback(MagicMock(callback_query=q),
                                          MagicMock(bot=_bot())))
        assert 'пуста' in q.answer.await_args.args[0]


class TestDayLabels:
    def test_today_tomorrow(self, env, monkeypatch):
        # Привязываемся к полудню: «+1 час» поздним вечером законно попадает
        # на завтра, и тест был бы хрупким
        noon = datetime(2026, 8, 1, 12, 0)
        monkeypatch.setattr(anime_news_bot, '_local_now', lambda: noon)
        monkeypatch.setattr(anime_news_bot, '_utc_to_local', lambda dt: dt)
        assert anime_news_bot._day_label(noon + timedelta(hours=1)) == 'Сегодня'
        assert anime_news_bot._day_label(noon + timedelta(days=1)) == 'Завтра'

    def test_far_date_named(self, env):
        label = anime_news_bot._day_label(datetime.now(timezone.utc) + timedelta(days=10))
        assert any(m in label for m in ('января', 'февраля', 'марта', 'апреля', 'мая',
                                        'июня', 'июля', 'августа', 'сентября',
                                        'октября', 'ноября', 'декабря'))

    def test_overdue(self, env):
        label = anime_news_bot._day_label(datetime.now(timezone.utc) - timedelta(days=3))
        assert 'росрочен' in label
