"""Глобальное обновление, ноябрь 2026: лимит модели, отложка, ветка."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


# ---------- дедуп картинки и ролика — до модели ----------

@pytest.fixture
def prepare(monkeypatch):
    for name in ('_improve_thumb', '_discover_article_video', '_ensure_youtube_cover',
                 '_optimize_news_media'):
        monkeypatch.setattr(bot, name, AsyncMock())
    monkeypatch.setattr(bot, '_tg_account_media_waits', lambda _n: False)
    monkeypatch.setattr(bot, '_assign_format_variant', lambda _n: None)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    llm = AsyncMock(return_value='skip')
    monkeypatch.setattr(bot, '_llm_enrich', llm)
    return llm


def test_image_duplicate_is_caught_before_the_model(prepare, monkeypatch):
    monkeypatch.setattr(bot, '_image_duplicate', AsyncMock(return_value='Старый пост'))
    monkeypatch.setattr(bot, '_video_duplicate', AsyncMock(return_value=None))
    assert asyncio.run(bot._prepare_news_for_send({'title': 'Т'}, 'S')) == 'skipped_dup'
    prepare.assert_not_called()


def test_video_duplicate_is_caught_before_the_model(prepare, monkeypatch):
    monkeypatch.setattr(bot, '_image_duplicate', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_video_duplicate', AsyncMock(return_value='Тот же трейлер'))
    assert asyncio.run(bot._prepare_news_for_send({'title': 'Т'}, 'S')) == 'skipped_dup'
    prepare.assert_not_called()


def test_manual_publish_skips_media_dedup(prepare, monkeypatch):
    image = AsyncMock(return_value='Старый пост')
    monkeypatch.setattr(bot, '_image_duplicate', image)
    asyncio.run(bot._prepare_news_for_send({'title': 'Т'}, 'S', apply_dedup=False))
    image.assert_not_called()
    prepare.assert_awaited()


def test_batch_skips_published_rejected_and_known_videos(monkeypatch, tmp_path):
    ledger = MagicMock(has_similar_title=lambda _t: False,
                       __contains__=lambda _self, link: link == 'https://e/sent',
                       is_rejected=lambda link: link == 'https://e/rejected')
    monkeypatch.setattr(bot, 'sent_links', ledger)
    hashes = bot.ImageHashes(tmp_path / 'h.json')
    hashes.add('v:dQw4w9WgXcQ', 'Трейлер')
    monkeypatch.setattr(bot, 'image_hashes', hashes)
    monkeypatch.setattr(bot, 'settings', NS(image_dedup=True))
    batch = AsyncMock(return_value=0)
    monkeypatch.setattr(bot, '_llm_enrich_batch', batch)
    news = [{'title': 'Вышла', 'link': 'https://e/sent'},
            {'title': 'Отсеяна', 'link': 'https://e/rejected'},
            {'title': 'Трейлер', 'link': 'https://e/v', 'video': 'https://youtu.be/dQw4w9WgXcQ'},
            {'title': 'Новая', 'link': 'https://e/new'}]
    asyncio.run(bot._llm_prefetch_for_cycle(news))
    assert [n['title'] for n in batch.await_args.args[0]] == ['Новая']


# ---------- кеш разборов модели на диске ----------

@pytest.fixture
def disk_cache(monkeypatch, tmp_path):
    path = tmp_path / 'llm_cache.json'
    monkeypatch.setattr(bot, 'LLM_EDITORIAL_CACHE_FILE', path)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, '_llm_editorial_cache_state',
                        {'loaded': False, 'dirty': False, 'saved_at': 0.0})
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-a')
    monkeypatch.setattr(bot, '_llm_editorial_data', lambda data: data)
    return path


def _restart(monkeypatch):
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, '_llm_editorial_cache_state',
                        {'loaded': False, 'dirty': False, 'saved_at': 0.0})


NEWS = {'title': 'Анонс второго сезона', 'summary': 'Текст'}


def test_cached_answer_survives_a_restart(disk_cache, monkeypatch):
    bot._llm_editorial_remember(NEWS, {'title': 'Готово'})
    assert disk_cache.exists()
    _restart(monkeypatch)
    assert bot._llm_editorial_cached(NEWS) == {'title': 'Готово'}


def test_other_model_does_not_reuse_disk_answers(disk_cache, monkeypatch):
    bot._llm_editorial_remember(NEWS, {'title': 'Готово'})
    _restart(monkeypatch)
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-b')
    assert bot._llm_editorial_cached(NEWS) is None


def test_stale_disk_answers_are_dropped(disk_cache, monkeypatch):
    bot._llm_editorial_remember(NEWS, {'title': 'Готово'})
    raw = json.loads(disk_cache.read_text(encoding='utf-8'))
    for entry in raw['items'].values():
        entry['at'] -= bot.LLM_EDITORIAL_CACHE_TTL_SEC + 1
    disk_cache.write_text(json.dumps(raw), encoding='utf-8')
    _restart(monkeypatch)
    assert bot._llm_editorial_cached(NEWS) is None


def test_disk_write_is_throttled(disk_cache, monkeypatch):
    writes = []
    real = bot._atomic_write_json
    monkeypatch.setattr(bot, '_atomic_write_json', lambda p, d, **k: writes.append(1) or real(p, d, **k))
    bot._llm_editorial_remember(NEWS, {'title': 'Раз'})
    bot._llm_editorial_remember(dict(NEWS, title='Другой'), {'title': 'Два'})
    assert len(writes) == 1                      # второй ждёт
    bot._llm_editorial_cache_flush(force=True)   # остановка пишет всё
    assert len(writes) == 2


# ---------- оценка токенов ----------

def test_russian_text_costs_more_tokens_than_latin():
    ru = bot._estimate_llm_tokens([{'content': 'а' * 330}], 0)
    en = bot._estimate_llm_tokens([{'content': 'a' * 330}], 0)
    assert ru - 32 == 100 and en - 32 == 83


# ---------- статьи пачки — параллельно ----------

def test_batch_fetches_articles_two_at_a_time(monkeypatch):
    active = []
    peak = []

    async def source_text(news):
        active.append(1)
        peak.append(len(active))
        await asyncio.sleep(0.01)
        active.pop()
        return news['title']
    monkeypatch.setattr(bot, '_llm_source_text', source_text)
    monkeypatch.setattr(bot, '_attach_work_about', AsyncMock())
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=None))
    asyncio.run(bot._llm_enrich_chunk([{'title': str(i)} for i in range(4)]))
    assert max(peak) == 2


# ---------- автопостинг помечает пост в ветке ----------

def test_autopost_marks_the_thread_post(tmp_path, monkeypatch):
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
    monkeypatch.setattr(bot, '_prepare_and_send_channel_post', AsyncMock(return_value=True))
    marked = []

    async def done(_bot, k, suffix):
        marked.append((k, store.get(k) is not None, suffix))
    monkeypatch.setattr(bot, '_update_moderation_done', done)
    assert asyncio.run(bot._autopost_one_from_thread(NS())) == 'sent'
    assert marked == [(key, True, '\n\n✅ Опубликовано в канал (автопостинг)')]


# ---------- перенос отложки ----------

def _reschedule(monkeypatch, scheduled):
    monkeypatch.setattr(bot, 'scheduled_posts', scheduled)
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=False))
    monkeypatch.setattr(bot, 'is_admin', lambda _u: True)
    monkeypatch.setattr(bot, '_parse_schedule_time',
                        lambda _t: datetime.now(timezone.utc) + timedelta(hours=2))
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.message = MagicMock(message_thread_id=10138, text='18:30')
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {'await_input': {'mode': 'reschedule', 'key': 'k', 'chat_id': -100, 'thread_id': 10138}}
    try:
        asyncio.run(bot.awaiting_input_handler(upd, ctx))
    except bot.ApplicationHandlerStop:
        pass
    return upd.message.reply_text.await_args.args[0]


def test_reschedule_reports_a_failed_write(monkeypatch):
    sched = MagicMock()
    sched.meta.return_value = {'state': 'pending'}
    sched.reschedule.return_value = False
    assert 'Не удалось записать' in _reschedule(monkeypatch, sched)


def test_uncertain_post_is_not_rescheduled(monkeypatch):
    sched = MagicMock()
    sched.meta.return_value = {'state': 'uncertain'}
    reply = _reschedule(monkeypatch, sched)
    sched.reschedule.assert_not_called()
    assert 'не подтвердил' in reply


# ---------- отложка после простоя ----------

@pytest.fixture
def overdue(tmp_path, monkeypatch):
    sched = bot.ScheduledPosts(tmp_path / 'sched.json')
    long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(3):
        sched.add({'title': f'Пост {i}', 'link': f'https://e/{i}'}, long_ago + timedelta(minutes=i))
    monkeypatch.setattr(bot, 'scheduled_posts', sched)
    for name in ('stats', 'source_yield', 'story_registry', 'story_history'):
        monkeypatch.setattr(bot, name, None)
    monkeypatch.setattr(bot, 'feature_enabled', lambda _n: False)
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    monkeypatch.setattr(bot, 'PAUSE_BETWEEN_SENDS', 0)
    monkeypatch.setattr(bot, '_sched_late_sent_at', 0.0)
    marks = []
    monkeypatch.setattr(bot, '_mark_published_now', lambda: marks.append(1))
    monkeypatch.setattr(bot, '_mark_published', lambda: None)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_prepare_and_send_channel_post', sent)
    return NS(sched=sched, sent=sent, marks=marks, monkeypatch=monkeypatch)


def test_overdue_posts_go_out_one_at_a_time(overdue):
    overdue.monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    asyncio.run(bot.publish_scheduled(MagicMock()))
    assert overdue.sent.await_count == 1 and len(overdue.sched.all()) == 2
    assert overdue.marks == [1]                  # интервал автопостинга сдвинут
    asyncio.run(bot.publish_scheduled(MagicMock()))
    assert overdue.sent.await_count == 1         # следующий — через SCHEDULED_LATE_GAP_SEC


def test_overdue_posts_wait_for_the_morning(overdue):
    overdue.monkeypatch.setattr(bot, '_channel_quiet_now', lambda: True)
    asyncio.run(bot.publish_scheduled(MagicMock()))
    overdue.sent.assert_not_called()


def test_on_time_post_ignores_quiet_hours(tmp_path, overdue):
    """Админ сам поставил ночное время — пост выходит ночью."""
    sched = bot.ScheduledPosts(tmp_path / 'sched2.json')
    sched.add({'title': 'Ночной', 'link': 'https://e/n'}, datetime.now(timezone.utc) - timedelta(minutes=1))
    overdue.monkeypatch.setattr(bot, 'scheduled_posts', sched)
    overdue.monkeypatch.setattr(bot, '_channel_quiet_now', lambda: True)
    asyncio.run(bot.publish_scheduled(MagicMock()))
    assert overdue.sent.await_count == 1


# ---------- ветка: кнопки на тексте после медиа ----------

def test_media_then_text_remembers_the_preview(monkeypatch):
    remembered = []
    monkeypatch.setattr(bot, '_remember_preview', lambda key, msg: remembered.append((key, msg)))
    monkeypatch.setattr(bot, 'settings', MagicMock(require_image=False))
    reply = MagicMock(message_id=9)
    api = MagicMock(send_message=AsyncMock(return_value=reply))
    ok = asyncio.run(bot._send_thread_media_then_text(
        api, {'title': 'Т', 'source': 'S', '_pending_key': '7'}, [], False, None, None,
        'Т', None, {}, -100))
    assert ok and remembered == [('7', reply)]
