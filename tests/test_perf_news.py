"""Ускорение кластеризации и поиска обновлений — без изменения решений.

Предфильтры отсекают только строки, которые в полном сравнении всё равно
дали бы 0. Тесты гоняют один и тот же поток новостей с фильтром и без него.
"""
import random

import pytest

import anime_news_bot as bot

FRANCHISES = ['Frieren', 'One Piece', 'Chainsaw Man', 'Dandadan', 'Kaiju No. 8', 'Фрирен', 'Ван Пис']
EVENTS = ['Season {n} Announced', 'Reveals Trailer for Season {n}', 'Delayed to {y}',
          'Film Premieres in {y}', 'Adds Cast Member', 'Key Visual Revealed', 'трейлер {n} сезона',
          'Season {n} Premieres October {d}, {y}', 'Manga Ends']


def stream(seed, count):
    rng = random.Random(seed)
    for i in range(count):
        franchise = rng.choice(FRANCHISES)
        event = rng.choice(EVENTS).format(n=rng.randint(2, 4), y=rng.randint(2026, 2027),
                                          d=rng.randint(1, 28))
        news = {'title': f'{franchise} {event}', 'link': f'https://s{i % 5}.test/{seed}/{i}',
                'summary': f'{franchise} {event} details', 'source': f'S{i % 5}'}
        if rng.random() < .25:
            news['_llm_subject'] = rng.choice(['frieren', 'one piece', 'фрирен'])
        if rng.random() < .25:
            news['_work_key'] = franchise.lower()
        if rng.random() < .2:
            news['_story_id'] = f'sid{rng.randint(0, 20)}'
        yield news


def observe_decisions(tmp_path, monkeypatch, seed, prefilter):
    if not prefilter:
        monkeypatch.setattr(bot.StoryRegistry, '_cannot_match', staticmethod(lambda *a: False))
    registry = bot.StoryRegistry(tmp_path / f'reg-{prefilter}.json')
    ids, out = {}, []
    for news in stream(seed, 400):
        memory = registry.observe(news, [news['source']], [news['link']])
        out.append((ids.setdefault(memory['registry_id'], len(ids)),
                    memory.get('delivery_duplicate'), memory['source_count']))
    return out, len(ids)


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_registry_prefilter_changes_no_decision(tmp_path, monkeypatch, seed):
    fast, stories = observe_decisions(tmp_path, monkeypatch, seed, True)
    slow, _ = observe_decisions(tmp_path, monkeypatch, seed, False)
    assert fast == slow
    assert 5 < stories < 300                 # склейки в потоке действительно есть


def test_prefilter_skips_only_hopeless_rows():
    row = {'title': 'Frieren Season 2 Announced', 'anchors': ['frieren'], 'work_key': 'frieren'}
    tokens = bot._story_tokens_cached('Dandadan Movie Revealed')
    assert bot.StoryRegistry._cannot_match(tokens, set(), '', row)
    assert not bot.StoryRegistry._cannot_match(tokens, set(), 'frieren', row)        # тот же тайтл
    assert not bot.StoryRegistry._cannot_match(tokens, {'frieren'}, '', row)         # общий якорь
    two = bot._story_tokens_cached('Frieren Season 3 Announced')
    assert not bot.StoryRegistry._cannot_match(two, set(), '', row)                  # два общих слова


def update_decisions(tmp_path, monkeypatch, seed, prefilter):
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
    history = bot.PublishedStoryStore(tmp_path / f'hist-{seed}-{prefilter}.json')
    news_stream = list(stream(seed, 500))
    for news in news_stream[:300]:
        history.record(news, 'text')
        history._items[-1]['story_id'] = news.get('_story_id') or ''
    if not prefilter:
        # Без отсева: отсев видит у любых двух заголовков два общих слова, и
        # каждая строка идёт в полное сравнение (оно считает слова само).
        monkeypatch.setattr(bot, '_story_tokens_cached', lambda title: frozenset({'x', 'y'}))
    out = []
    for news in news_stream[300:]:
        found = history.classify_update(news)
        out.append(None if found is None else (found['title'], found['_similarity'], found['_novelty']))
    return out


@pytest.mark.parametrize('seed', [4, 5])
def test_update_prefilter_changes_no_decision(tmp_path, monkeypatch, seed):
    fast = update_decisions(tmp_path, monkeypatch, seed, True)
    slow = update_decisions(tmp_path, monkeypatch, seed, False)
    assert fast == slow
    assert any(fast)
