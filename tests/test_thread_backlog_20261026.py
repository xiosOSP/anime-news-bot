"""Ветка модерации копила 150 постов (октябрь 2026).

Ветка получала примерно в 2,5 раза больше постов, чем канал выпускает, а
пост уходил из неё только через 7 дней. Автопостинг же берёт посты не старше
72 ч и всегда самый свежий — большая часть ветки была мёртвым грузом.
"""
import time
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot


def _store(tmp_path):
    return bot.PendingPosts(tmp_path / 'pending.json')


def _age(store, key, hours):
    store._items[key]['ts'] = time.time() - hours * 3600


# ---------- срок хранения и посты с неизвестным результатом ----------

def test_posts_leave_the_thread_after_the_ttl(tmp_path):
    store = _store(tmp_path)
    old = store.add({'title': 'Старый', 'link': 'https://e/1'})
    young = store.add({'title': 'Молодой', 'link': 'https://e/2'})
    _age(store, old, bot.PENDING_TTL_HOURS + 1)
    _age(store, young, bot.PENDING_TTL_HOURS - 1)
    store.add({'title': 'Новый', 'link': 'https://e/3'})      # чистка идёт при добавлении
    assert store.get(old) is None and store.get(young) is not None


def test_ttl_is_shorter_than_a_week_but_longer_than_freshness():
    assert bot.POST_MAX_AGE_HOURS < bot.PENDING_TTL_HOURS < 7 * 24


def test_uncertain_post_no_longer_lives_forever(tmp_path):
    store = _store(tmp_path)
    key = store.add({'title': 'Неизвестно', 'link': 'https://e/1'})
    store.mark_channel_uncertain(key)
    _age(store, key, bot.PENDING_TTL_HOURS + 1)
    store.add({'title': 'Новый', 'link': 'https://e/2'})
    assert store.get(key) is None


def test_young_uncertain_post_stays(tmp_path):
    store = _store(tmp_path)
    key = store.add({'title': 'Неизвестно', 'link': 'https://e/1'})
    store.mark_channel_uncertain(key)
    _age(store, key, 2)
    store.add({'title': 'Новый', 'link': 'https://e/2'})
    assert store.channel_state(key) == 'uncertain'


def test_post_being_published_is_never_dropped(tmp_path, monkeypatch):
    store = _store(tmp_path)
    key = store.add({'title': 'Публикуется', 'link': 'https://e/1'})
    store.mark_channel_sending(key)
    _age(store, key, bot.PENDING_TTL_HOURS + 1)
    store.add({'title': 'Новый', 'link': 'https://e/2'})
    assert store.get(key) is not None


# ---------- выбор в канал: ценность с поправкой на свежесть ----------

@pytest.fixture
def fresh_window(monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(post_max_age_hours=72))


def test_valuable_post_beats_a_slightly_fresher_minor_one(tmp_path, fresh_window):
    store = _store(tmp_path)
    big = store.add({'title': 'Анонс с трейлером', 'link': 'https://e/1', '_priority_score': 12.0})
    _age(store, big, 10)
    store.add({'title': 'Мелочь', 'link': 'https://e/2', '_priority_score': 2.0})
    assert store.next_for_autopost()[0] == big


def test_freshness_wins_eventually(tmp_path, fresh_window):
    store = _store(tmp_path)
    big = store.add({'title': 'Анонс', 'link': 'https://e/1', '_priority_score': 12.0})
    _age(store, big, 30)           # 12 − 15 < 2
    minor = store.add({'title': 'Мелочь', 'link': 'https://e/2', '_priority_score': 2.0})
    assert store.next_for_autopost()[0] == minor


def test_equal_value_still_takes_the_freshest(tmp_path, fresh_window):
    store = _store(tmp_path)
    store.add({'title': 'Первый', 'link': 'https://e/1', '_priority_score': 5.0})
    second = store.add({'title': 'Второй', 'link': 'https://e/2', '_priority_score': 5.0})
    assert store.next_for_autopost()[0] == second


def test_breaking_news_gets_a_head_start(tmp_path, fresh_window):
    store = _store(tmp_path)
    breaking = store.add({'title': 'Срочно', 'link': 'https://e/1', '_priority_score': 2.0,
                          '_breaking_news': True})
    _age(store, breaking, 6)
    store.add({'title': 'Обычная', 'link': 'https://e/2', '_priority_score': 4.0})
    assert store.next_for_autopost()[0] == breaking


def test_value_falls_with_age():
    now = time.time()
    news = {'_priority_score': 10.0}
    assert bot._autopost_value(news, now, now) == 10.0
    assert bot._autopost_value(news, now - 4 * 3600, now) == pytest.approx(
        10.0 - 4 * bot.AUTOPOST_AGE_PENALTY_PER_HOUR)


# ---------- «ждут решения»: сколько годится для канала ----------

def test_pending_count_shows_how_many_fit_the_channel(tmp_path, monkeypatch, fresh_window):
    store = _store(tmp_path)
    store.add({'title': 'Годный', 'link': 'https://e/1'})
    stale = store.add({'title': 'Старый', 'link': 'https://e/2'})
    _age(store, stale, 80)
    blocked = store.add({'title': 'Повтор тайтла', 'link': 'https://e/3'})
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, '_autopost_skip_reason',
                        lambda news: 'про тайтл уже был пост' if news['link'] == 'https://e/3' else '')
    assert blocked and bot._pending_count_text() == '3 (годных для канала: 1)'


def test_empty_thread_is_just_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'pending_posts', _store(tmp_path))
    assert bot._pending_count_text() == '0'


# ---------- впуск в «сытую» ветку ----------

@pytest.fixture
def full_thread(tmp_path, monkeypatch, fresh_window):
    store = _store(tmp_path)
    for i in range(3):
        store.add({'title': f'Запас {i}', 'link': f'https://e/{i}', '_priority_score': 5.0 + i})
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, '_autopost_skip_reason', lambda news: '')
    monkeypatch.setattr(bot, 'THREAD_FULL_CANDIDATES', 3)
    return store


def test_full_thread_admits_only_better_posts(full_thread):
    strong = {'title': 'Сильная', 'link': 'https://n/1', '_priority_score': 9.0}
    weak = {'title': 'Слабая', 'link': 'https://n/2', '_priority_score': 3.0}
    admitted, deferred = bot._thread_admission([strong, weak])
    assert admitted == [strong] and deferred == 1


def test_breaking_and_story_updates_always_pass(full_thread):
    breaking = {'title': 'Срочно', 'link': 'https://n/1', '_priority_score': -20.0,
                '_breaking_news': True}
    update = {'title': 'Обновление', 'link': 'https://n/2', '_priority_score': 0.0,
              '_story_update_of': 'abc'}
    assert bot._thread_admission([breaking, update]) == ([breaking, update], 0)


def test_thread_with_room_admits_everyone(full_thread, monkeypatch):
    monkeypatch.setattr(bot, 'THREAD_FULL_CANDIDATES', 4)
    weak = {'title': 'Слабая', 'link': 'https://n/2', '_priority_score': -5.0}
    assert bot._thread_admission([weak]) == ([weak], 0)


def test_stale_and_skipped_posts_are_not_stock(full_thread, monkeypatch):
    """Запас — только то, что канал может взять: старые и отсеянные не в счёт."""
    _age(full_thread, next(iter(full_thread._items)), 80)
    weak = {'title': 'Слабая', 'link': 'https://n/2', '_priority_score': -5.0}
    assert bot._thread_admission([weak]) == ([weak], 0)


def test_zero_turns_admission_off(full_thread, monkeypatch):
    monkeypatch.setattr(bot, 'THREAD_FULL_CANDIDATES', 0)
    weak = {'title': 'Слабая', 'link': 'https://n/2', '_priority_score': -5.0}
    assert bot._thread_admission([weak]) == ([weak], 0)


def test_old_source_date_lowers_the_new_post(full_thread):
    old = {'title': 'Старая в источнике', 'link': 'https://n/3', '_priority_score': 9.0,
           'published_parsed': time.gmtime(time.time() - 20 * 3600)}
    assert bot._thread_admission([old]) == ([], 1)
