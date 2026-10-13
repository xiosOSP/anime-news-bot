"""Один тайтл — один пост: дубли в канале, на которые жаловались читатели.

Пересказ случая (октябрь 2026): владелец написал, что аниме «Экзорцист Нюэ»
(Nue's Exorcist) анонсировано; бот в тот же день выложил постер и тизер
«Оммёдзи Нуэ», а назавтра — ещё и анонс с датой. Названия разные, тайтл один.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot

NUE = {'found': True, 'romaji': 'Nue no Onmyouji', 'english': "Nue's Exorcist",
       'native': '鵺の陰陽師', 'synonyms': []}
OWNER = "Аниме «Экзорцист Нюэ» (Nue's Exorcist) официально анонсировано!"
BOT_FIRST = {'title': 'Постер и тизер аниме «Оммёдзи Нуэ» от студии Hibari',
             '_llm_subject': 'Nue no Onmyouji'}
BOT_SECOND = {'title': 'Анонсировано аниме по манге «Оммёдзи Нуэ», премьера летом 2027',
              '_llm_subject': 'Nue no Onmyouji'}


@pytest.fixture
def anilist(monkeypatch, tmp_path):
    client = bot.AniListClient(tmp_path / 'anilist.json')
    for name in ("Nue's Exorcist", 'Nue no Onmyouji'):
        client._cache[bot.AniListClient._norm_key(name)] = dict(
            NUE, checked_at=bot.datetime.now().isoformat())
    monkeypatch.setattr(bot, 'anilist', client)
    monkeypatch.setattr(client, '_query_api', lambda *a, **k: pytest.fail('сеть не нужна'))
    return client


@pytest.fixture
def memory(monkeypatch, tmp_path):
    store = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', store)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    return store


def test_english_and_romaji_names_meet_through_anilist(anilist):
    owner = bot._manual_post_news(OWNER, 'https://t.me/chan/1')
    assert set(bot._work_identity_keys(owner)) & set(bot._work_identity_keys(BOT_FIRST))


def test_bot_repeats_meet_without_network(monkeypatch):
    monkeypatch.setattr(bot, 'anilist', None)
    assert set(bot._work_identity_keys(BOT_FIRST)) & set(bot._work_identity_keys(BOT_SECOND))


def test_russian_name_alone_links_the_bots_own_posts(monkeypatch):
    monkeypatch.setattr(bot, 'anilist', None)
    first = {'title': 'Вышли кадры аниме «Оммёдзи Нуэ»'}
    second = {'title': 'Аниме «Оммёдзи Нуэ» выйдет летом'}
    assert set(bot._work_identity_keys(first)) & set(bot._work_identity_keys(second))


def test_short_quoted_word_is_not_a_title(monkeypatch):
    monkeypatch.setattr(bot, 'anilist', None)
    assert bot._work_identity_keys({'title': 'Студия «Ufo» показала кадры'}) == []
    assert bot._work_identity_keys({'title': 'Новое «Аниме» студии'}) == []


def test_latin_subject_links_different_russian_names(monkeypatch):
    """Каналы переводят название по-своему; subject модели — латиница."""
    monkeypatch.setattr(bot, 'anilist', None)
    first = {'title': 'Кадры аниме «Оммёдзи Нуэ»', '_llm_subject': 'Nue no Onmyouji'}
    second = {'title': 'Тизер аниме «Экзорцист Нюэ»', '_llm_subject': 'Nue no Onmyouji'}
    assert set(bot._work_identity_keys(first)) & set(bot._work_identity_keys(second))


def test_unrelated_titles_do_not_meet(monkeypatch):
    monkeypatch.setattr(bot, 'anilist', None)
    a = bot._work_identity_keys({'title': 'Кадры аниме «Магическая битва»', '_llm_subject': 'Jujutsu Kaisen'})
    b = bot._work_identity_keys({'title': 'Кадры аниме «Ледяная стена»', '_llm_subject': 'Koori no Jouheki'})
    assert not set(a) & set(b)


def test_anilist_guess_that_does_not_match_the_name_is_ignored(monkeypatch, tmp_path):
    client = bot.AniListClient(tmp_path / 'anilist.json')
    client._cache[bot.AniListClient._norm_key('Phantom Busters')] = dict(
        found=True, romaji='Saint October', english='Saint October', native='', synonyms=[],
        checked_at=bot.datetime.now().isoformat())
    monkeypatch.setattr(bot, 'anilist', client)
    keys = bot._work_identity_keys({'title': 'x', '_llm_subject': 'Phantom Busters'})
    assert not any(k.startswith('ani:') for k in keys)


def test_network_lookup_only_within_budget(monkeypatch, tmp_path):
    client = bot.AniListClient(tmp_path / 'anilist.json')
    calls = []
    monkeypatch.setattr(client, '_query_api', lambda q, manga=False: calls.append(q) or dict(NUE))
    monkeypatch.setattr(bot, 'anilist', client)
    bot._work_identity_keys({'title': 'x', '_llm_subject': "Nue's Exorcist"}, lookups=0)
    assert calls == []
    keys = bot._work_identity_keys({'title': 'x', '_llm_subject': "Nue's Exorcist"}, lookups=1)
    assert calls and 'ani:nuenoonmyouji' in keys


def test_memory_window_and_restart(memory, tmp_path):
    memory.remember(['name:nuenoonmyouji'], OWNER, 'admin')
    assert memory.seen(['name:nuenoonmyouji'], 36)['by'] == 'admin'
    memory._items[-1]['at'] = time.time() - 40 * 3600
    assert memory.seen(['name:nuenoonmyouji'], 36) is None
    memory.remember(['ru:x1234'], 'другое', 'bot')
    assert bot.WorkMemory(tmp_path / 'works.json').seen(['ru:x1234'], 36)


def test_zero_hours_turns_the_guard_off(memory, monkeypatch):
    memory.remember(['name:nuenoonmyouji'], OWNER, 'admin')
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 0)
    assert not bot._work_repeat_blocks({'_work_keys': ['name:nuenoonmyouji']})


def test_admin_post_is_remembered(anilist, memory, monkeypatch):
    for name in ('story_registry', 'published_texts', 'image_hashes'):
        monkeypatch.setattr(bot, name, None)
    bot._remember_manual_channel_post(bot._manual_post_news(OWNER, 'https://t.me/chan/1'))
    assert bot._work_repeat_blocks(dict(BOT_FIRST))


def test_bot_channel_post_is_remembered(memory, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'anilist', None)
    monkeypatch.setattr(bot, 'story_history', bot.PublishedStoryStore(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'settings', None)
    bot._record_story_history(dict(BOT_FIRST, link='https://x/1'))
    assert bot._work_repeat_blocks(dict(BOT_SECOND))


@pytest.fixture
def prepare_env(monkeypatch, memory):
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
    monkeypatch.setattr(bot, 'anilist', None)
    monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='ok'))
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    memory.remember(['name:nuenoonmyouji'], OWNER, 'admin')
    return bot


def _news():
    return dict(BOT_SECOND, source='TG: A', link='https://x/nue', summary='')


def test_autopublish_skips_a_title_the_channel_just_covered(prepare_env):
    news = _news()
    assert asyncio.run(bot._prepare_news_for_send(news, news['source'], channel_quota=True)) == 'skipped_dup'
    bot.stats.record_skipped.assert_awaited_once_with('duplicate', news['source'])


def test_admin_button_is_not_blocked(prepare_env):
    news = _news()
    assert asyncio.run(bot._prepare_news_for_send(news, news['source'])) is None
    assert news['_work_keys'], 'ключи тайтла не сохранены в новости'


def test_thread_autopost_passes_over_a_covered_title(memory, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    memory.remember(['name:nuenoonmyouji'], OWNER, 'admin')
    pending = bot.PendingPosts(tmp_path / 'pending.json')
    other = pending.add({'title': 'Кадры «Ледяной стены»', 'link': 'https://x/a',
                         '_work_keys': ['name:kooriinojouheki']})
    pending.add(dict(_news(), _work_keys=['name:nuenoonmyouji']))
    key, _ = pending.next_for_autopost(skip=bot._autopost_skip)
    assert key == other
