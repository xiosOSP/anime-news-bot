"""Баги публикации из внутреннего аудита (октябрь 2026)."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def autopost(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'pending.json')
    key = store.add({'title': 'Новость', 'link': 'https://e.com/1'})
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', NS(last_channel_post_at='', channel_interval_sec=3600,
                                            last_publish_at='', post_max_age_hours=72,
                                            save=lambda: None))
    for name in ('stats', 'source_yield', 'story_registry', 'story_history'):
        monkeypatch.setattr(bot, name, None)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: False)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    marks = []
    monkeypatch.setattr(bot, '_mark_published_now', lambda: marks.append('channel'))
    return NS(store=store, key=key, marks=marks, monkeypatch=monkeypatch)


def _send_raising(exc, *, started):
    async def send(delivery, news):
        if started:
            delivery.started = True
        raise exc
    return send


def test_uncertain_autopost_moves_the_channel_clock(autopost):
    """Таймаут на загрузке ролика: пост, скорее всего, вышел. Следующий —
    не через минуту, а через интервал."""
    autopost.monkeypatch.setattr(bot, '_prepare_and_send_channel_post',
                                 _send_raising(bot.DeliveryUncertain('timed out'), started=True))
    assert asyncio.run(bot._autopost_one_from_thread(NS())) == 'uncertain'
    assert autopost.marks == ['channel']
    assert autopost.store.channel_state(autopost.key) == 'uncertain'


def test_crash_after_sending_started_is_uncertain_not_retry(autopost):
    autopost.monkeypatch.setattr(bot, '_prepare_and_send_channel_post',
                                 _send_raising(RuntimeError('photo follow-up'), started=True))
    assert asyncio.run(bot._autopost_one_from_thread(NS())) == 'uncertain'
    assert autopost.store.channel_state(autopost.key) == 'uncertain'
    assert autopost.marks == ['channel']


def test_crash_before_sending_is_a_retry(autopost):
    autopost.monkeypatch.setattr(bot, '_prepare_and_send_channel_post',
                                 _send_raising(RuntimeError('prep'), started=False))
    assert asyncio.run(bot._autopost_one_from_thread(NS())) == 'failed'
    assert autopost.store.channel_state(autopost.key) == 'pending'
    assert autopost.marks == []


def test_queue_uncertain_also_moves_the_channel_clock(monkeypatch):
    news = {'title': 'Т', 'link': 'https://e.com/q'}
    queue = MagicMock()
    queue.pop_next = AsyncMock(side_effect=[news, None])
    queue.ack_done = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    monkeypatch.setattr(bot, '_llm_prefetch_queue_head', AsyncMock())
    monkeypatch.setattr(bot, 'send_news', AsyncMock(return_value='uncertain'))
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    marks = []
    monkeypatch.setattr(bot, '_mark_published_now', lambda: marks.append(1))
    asyncio.run(bot._publish_one_from_queue_locked(MagicMock()))
    assert marks == [1]


# ---------- пост в ветке не кандидат канала, пока не доставлен ----------

def test_staging_post_is_not_offered_to_the_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))
    store = bot.PendingPosts(tmp_path / 'p.json')
    key = store.add({'title': 'Отправляется', 'link': 'https://e.com/s'}, staging=True)
    reasons = {}
    assert store.next_for_autopost(reasons=reasons) is None
    assert reasons == {'ещё отправляется в ветку': 1}
    assert store.mark_channel_sending(key) is False          # и кнопкой тоже нельзя
    store.mark_channel_pending(key)
    assert store.next_for_autopost()[0] == key


def test_staging_becomes_pending_after_restart(tmp_path):
    store = bot.PendingPosts(tmp_path / 'p.json')
    key = store.add({'title': 'Отправлялся', 'link': 'https://e.com/s'}, staging=True)
    assert bot.PendingPosts(tmp_path / 'p.json').channel_state(key) == 'pending'


# ---------- правка текста: откат при сбое диска ----------

def test_failed_edit_keeps_the_old_text(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'p.json')
    key = store.add({'title': 'Т', 'link': 'https://e.com/e', '_edited_text': 'Старый'})
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=True))
    monkeypatch.setattr(bot, 'is_admin', lambda _u: True)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(store, '_save', lambda: False)
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.message = MagicMock(message_thread_id=10138, text='Новый')
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {'await_input': {'mode': 'edit', 'key': key, 'chat_id': -100, 'thread_id': 10138}}
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    try:
        asyncio.run(bot.awaiting_input_handler(upd, ctx))
    except bot.ApplicationHandlerStop:
        pass
    assert store.get(key)['_edited_text'] == 'Старый'


# ---------- отложка держит место «публикуется» ----------

def test_schedule_holds_the_publish_guard(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'p.json')
    key = store.add({'title': 'Т', 'link': 'https://e.com/sch'})
    seen = []

    class Sched:
        def add(self, news, when, by=None):
            seen.append(bot._is_publishing(f'pending:{key}'))
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'scheduled_posts', Sched())
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=True))
    monkeypatch.setattr(bot, 'is_admin', lambda _u: True)
    monkeypatch.setattr(bot, 'moderation_feedback', None)
    monkeypatch.setattr(bot, '_update_moderation_done', AsyncMock())
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, '_parse_schedule_time',
                        lambda _t: bot.datetime.now(bot.timezone.utc) + bot.timedelta(hours=2))
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.message = MagicMock(message_thread_id=10138, text='18:30')
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {'await_input': {'mode': 'schedule', 'key': key, 'chat_id': -100, 'thread_id': 10138}}
    try:
        asyncio.run(bot.awaiting_input_handler(upd, ctx))
    except bot.ApplicationHandlerStop:
        pass
    assert seen == [True]
    assert not bot._is_publishing(f'pending:{key}')          # место освобождено
    assert store.get(key) is None


# ---------- отправка в ветку: staging и медиа после загрузки ----------

@pytest.fixture
def thread_send(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'p.json')
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', MagicMock(video_enabled=False, require_image=True))
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    monkeypatch.setattr(bot, 'format_news_text_long', lambda news: news['title'])
    monkeypatch.setattr(bot, '_video_thumbnail_kwargs_async', AsyncMock(return_value={}))
    states = []

    async def send_photo(**kw):
        states.extend(item['channel_state'] for item in store._items.values())
        return MagicMock(message_id=5)
    api = MagicMock(send_photo=send_photo, send_message=AsyncMock(return_value=MagicMock(message_id=6)),
                    send_media_group=AsyncMock(return_value=[MagicMock(message_id=7)]))
    return NS(store=store, api=api, states=states, monkeypatch=monkeypatch)


def test_thread_post_is_staging_while_telegram_answers(thread_send):
    thread_send.monkeypatch.setattr(bot, '_resolve_photos_for_album',
                                    AsyncMock(return_value=[b'jpeg-bytes']))
    news = {'title': 'Постер', 'link': 'https://e.com/t', 'source': 'S', 'images': ['https://img/x.jpg']}
    asyncio.run(bot._send_post_thread_split(thread_send.api, news, None))
    assert thread_send.states == ['staging']


def test_photos_lost_in_download_are_recounted(thread_send):
    """Все фото отпали при загрузке, видео нет: при require_image — пропуск, а не пустой альбом."""
    thread_send.monkeypatch.setattr(bot, '_resolve_photos_for_album', AsyncMock(return_value=[]))
    news = {'title': 'Постер', 'link': 'https://e.com/t2', 'images': ['https://img/x.jpg']}
    assert asyncio.run(bot._send_post_thread_split(thread_send.api, news, None)) is False
    thread_send.api.send_media_group.assert_not_called()
    assert thread_send.store._items == {}
