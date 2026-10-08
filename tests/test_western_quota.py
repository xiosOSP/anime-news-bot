"""Суточный лимит западного кино и корпоративная хроника студий.

7–8 октября 2026 из 17 постов бота в канале 14 были про Голливуд, Marvel и
Skydance (кто возглавил маркетинг, сколько миллиардов долга, ведро для
попкорна) — ни одной реакции. Три поста про аниме за те же дни собрали по
три. Вес темы такие посты уже понижал, но когда аниме в очереди не было,
кино всё равно шло подряд. Примеры ниже — пересказ, не цитаты.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


def _film(link='https://t.me/geek/1', **extra):
    news = {'title': 'Режиссёр вернётся к новой части «Роботов-гигантов»',
            'summary': 'Студия подтвердила возвращение постановщика.',
            'source': 'TG: QewbsNews', 'link': link, '_llm_topic': 'кино'}
    news.update(extra)
    return news


def _anime(link='https://t.me/anime/1'):
    return {'title': 'Аниме по манге о замке демонов выйдет в январе',
            'summary': 'Анимацию делает студия Project No.9.',
            'source': 'TG: Advance', 'link': link, '_llm_topic': 'аниме'}


@pytest.fixture
def history(monkeypatch, tmp_path):
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    monkeypatch.setattr(bot, 'story_history', store)
    monkeypatch.setattr(bot, 'WESTERN_DAILY_MAX', 3)
    return store


def _publish(store, news, hours_ago=0.0):
    store.record(news)
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    store._items[-1]['at'] = at.isoformat()


# ------------------------------------------------ что считается западным кино

@pytest.mark.parametrize('news, western', [
    (_film(), True),
    (_film(source='TG: Nexvlsz', _llm_topic='комиксы',
           title='Продан редкий выпуск комикса о паучке'), True),
    (_film(source='TG: Nexvlsz', _llm_topic=''), False),
    # Кино-новость про аниме-франшизу — профильная, лимит её не касается.
    (_film(title='Полнометражный «Наруто» покажут на фестивале'), False),
    (_film(_llm_topic='игры', title='В шутер добавили пять скинов'), False),
    # Лента общей тематики без вердикта модели: тему не знаем, не гадаем.
    (_film(_llm_topic=''), False),
    (_film(source='Variety', _llm_topic='прочее'), True),
    (_anime(), False),
])
def test_what_counts_as_western(news, western):
    assert bot._is_western_screen(news) is western


# ------------------------------------------------ подсчёт за сутки

def test_history_marks_western_posts(history):
    _publish(history, _film())
    _publish(history, _anime())
    assert [row['western'] for row in history._items] == [True, False]


def test_only_the_last_day_counts(history):
    _publish(history, _film('https://x/1'), hours_ago=30)
    _publish(history, _film('https://x/2'), hours_ago=25)
    _publish(history, _film('https://x/3'), hours_ago=5)
    _publish(history, _anime('https://x/4'), hours_ago=1)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert history.count_since('western', since) == 1


def test_naive_timestamp_is_read_as_utc(history):
    _publish(history, _film())
    history._items[-1]['at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(
        tzinfo=None).isoformat()
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    assert history.count_since('western', since) == 1


def test_quota_fills_at_the_limit(history):
    for i in range(2):
        _publish(history, _film(f'https://x/{i}'), hours_ago=i + 1)
    assert bot._western_quota_blocks(_film('https://x/new')) is False
    _publish(history, _film('https://x/2'), hours_ago=3)
    assert bot._western_quota_blocks(_film('https://x/new')) is True
    # Аниме лимит не трогает никогда.
    assert bot._western_quota_blocks(_anime()) is False


def test_yesterdays_films_free_the_limit(history):
    for i in range(3):
        _publish(history, _film(f'https://x/{i}'), hours_ago=25 + i)
    assert bot._western_quota_blocks(_film('https://x/new')) is False


def test_without_history_there_is_no_limit(monkeypatch):
    monkeypatch.setattr(bot, 'story_history', None)
    assert bot._western_quota_blocks(_film()) is False
    assert bot._western_status_line() == ''


def test_zero_means_no_limit(history, monkeypatch):
    for i in range(5):
        _publish(history, _film(f'https://x/{i}'))
    monkeypatch.setattr(bot, 'WESTERN_DAILY_MAX', 0)
    assert bot._western_quota_blocks(_film('https://x/new')) is False


# ------------------------------------------------ путь публикации

@pytest.fixture
def prepare_env(monkeypatch, history):
    monkeypatch.setattr(bot, 'settings', MagicMock(local_topic_filter=False, llm_enabled=True,
                                                   llm_rewrite=True, dedup_final_text=False))
    for name in ('_improve_thumb', '_discover_article_video', '_ensure_youtube_cover',
                 '_optimize_news_media', '_image_duplicate', '_video_duplicate'):
        monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    monkeypatch.setattr(bot, '_left_untranslated', lambda news: False)
    monkeypatch.setattr(bot, '_tg_copy_without_rewrite', lambda news: False)
    monkeypatch.setattr(bot, 'published_texts', None)
    monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='ok'))
    for i in range(3):
        _publish(history, _film(f'https://x/{i}'), hours_ago=i + 1)
    return bot


def test_autopublish_skips_film_over_the_limit(prepare_env):
    news = _film('https://x/new')
    assert asyncio.run(bot._prepare_news_for_send(
        news, news['source'], channel_quota=True)) == 'skipped_filter'
    bot.stats.record_skipped.assert_awaited_once_with('filtered', news['source'])


def test_anime_still_goes_out_when_film_limit_is_spent(prepare_env):
    news = _anime('https://x/new')
    assert asyncio.run(bot._prepare_news_for_send(
        news, news['source'], channel_quota=True)) is None


def test_admin_choice_ignores_the_limit(prepare_env):
    """Кнопка админа и ветка обсуждения — не автопубликация в канал."""
    news = _film('https://x/new')
    assert asyncio.run(bot._prepare_news_for_send(news, news['source'])) is None


def test_send_news_applies_limit_only_for_the_channel(monkeypatch):
    seen = []

    async def prepare(news, source, count_stats=True, **kwargs):
        seen.append(kwargs['channel_quota'])
        return 'skipped_filter'
    monkeypatch.setattr(bot, '_prepare_news_for_send', prepare)
    monkeypatch.setattr(bot, 'matches_keywords', lambda news: True)
    ledger = MagicMock(has_similar_title=lambda title: False, claim=AsyncMock(return_value=True),
                       reject=AsyncMock(), release=AsyncMock())
    monkeypatch.setattr(bot, 'sent_links', ledger)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    news = _film('https://x/new')
    asyncio.run(bot.send_news(MagicMock(), dict(news), channel_quota=True))
    asyncio.run(bot.send_news(MagicMock(), dict(news), chat_id=1, channel_quota=True))
    asyncio.run(bot.send_news(MagicMock(), dict(news)))
    assert seen == [True, False, False]


def test_thread_autopost_passes_over_spent_film(history, tmp_path):
    for i in range(3):
        _publish(history, _film(f'https://x/{i}'), hours_ago=i + 1)
    pending = bot.PendingPosts(tmp_path / 'pending.json')
    anime_key = pending.add(_anime('https://x/a'))
    pending.add(_film('https://x/f'))               # свежее, но лимит выбран
    key, _news = pending.next_for_autopost(skip=bot._western_quota_blocks)
    assert key == anime_key


def test_thread_autopost_uses_the_limit(monkeypatch):
    pending = MagicMock()
    pending.next_for_autopost.return_value = None
    monkeypatch.setattr(bot, 'pending_posts', pending)
    monkeypatch.setattr(bot, '_history_paused', lambda: False)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda now_local=None: False)

    async def due(now=None):
        return True
    monkeypatch.setattr(bot, '_channel_autopost_due', due)
    asyncio.run(bot._autopost_one_from_thread(MagicMock()))
    pending.next_for_autopost.assert_called_once_with(skip=bot._western_quota_blocks)


def test_status_shows_the_limit(history, monkeypatch):
    _publish(history, _film())
    assert bot._western_status_line() == '🎬 Западное кино за сутки: 1 из 3\n'
    monkeypatch.setattr(bot, 'WESTERN_DAILY_MAX', 0)
    assert bot._western_status_line() == ''


# ------------------------------------------------ корпоративная хроника

@pytest.mark.parametrize('title', [
    'Бывший сотрудник студии возглавит её маркетинговый отдел',
    'Студия потратит 40 млрд долларов на контент',
    'Компания начинает работу с огромным долгом',
    'Studio chief steps down after a bad year',
    'Новым CEO студии стал бывший продюсер',
])
def test_studio_business_news_ranks_below_plain_film_news(title):
    plain = _film(title='Вышел новый трейлер фантастического боевика', summary='')
    business = _film(title=title, summary='')
    assert bot._topic_affinity(business) == bot._topic_affinity(plain) - 3.0


def test_anime_business_news_is_not_penalised():
    """Кадры в аниме-студии — профильная новость, её вес не трогаем."""
    plain = _anime()
    business = dict(_anime(), title='Новый продюсер возглавит аниме-студию MAPPA')
    assert bot._topic_affinity(business) == bot._topic_affinity(plain)
