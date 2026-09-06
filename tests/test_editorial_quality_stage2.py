import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def _news(title, link='https://example.com/a', source='A', summary='Подробности события и официальный комментарий.'):
    return {'title': title, 'link': link, 'source': source, 'summary': summary, 'images': []}


def test_glossary_persists_case_only_rule(tmp_path):
    path = tmp_path / 'glossary.json'
    g = bot.EditorialGlossary(path)
    assert g.add('mappa', 'MAPPA')
    assert g.apply('mappa и Mappa работают над проектом') == 'MAPPA и MAPPA работают над проектом'
    assert bot.EditorialGlossary(path).apply('mappa') == 'MAPPA'


def test_glossary_prefers_longer_alias_first(tmp_path):
    g = bot.EditorialGlossary(tmp_path / 'g.json')
    g.add('Attack on Titan', 'Атака титанов')
    g.add('Titan', 'Титан')
    assert g.apply('Attack on Titan returns') == 'Атака титанов returns'


def test_entity_memory_manual_alias_is_reused(tmp_path):
    mem = bot.EntityMemory(tmp_path / 'entities.json')
    assert mem.remember('Shingeki no Kyojin', 'Атака титанов', source='admin')
    assert mem.observe('Shingeki no Kyojin') == 'Атака титанов'
    assert mem.apply('Shingeki no Kyojin получит новый постер') == 'Атака титанов получит новый постер'
    assert bot.EntityMemory(tmp_path / 'entities.json').observe('Shingeki no Kyojin') == 'Атака титанов'


def test_story_update_prefix_keeps_original_capitalization(monkeypatch):
    monkeypatch.setattr(bot, 'editorial_glossary', None)
    monkeypatch.setattr(bot, 'entity_memory', None)
    news = {'_story_update_of': 'abc'}
    assert bot._apply_editorial_rules('MAPPA показала трейлер.', news).startswith('Обновление: MAPPA')


def test_story_history_detects_meaningful_update(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    old = _news(
        'Witch Hat Atelier anime adaptation gets a trailer',
        'https://old.example/1',
        summary='The anime adaptation was officially announced by the production committee.',
    )
    old['_story_id'] = bot._story_id(old)
    store.record(old)
    new = _news(
        'Witch Hat Atelier anime adaptation gets a new trailer',
        'https://new.example/2',
        summary='The new trailer confirms the premiere on October 4 and names the streaming platform.',
    )
    new['_story_id'] = bot._story_id(new)
    match = store.classify_update(new)
    assert match is not None
    assert match['story_id'] == old['_story_id']
    assert match['_novelty'] > 0


def test_story_history_does_not_promote_plain_repeat(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    old = _news('Dandadan anime releases a new trailer', 'https://old.example/1', summary='A new trailer was released for the anime project.')
    old['_story_id'] = bot._story_id(old)
    store.record(old)
    repeat = _news('Dandadan anime releases a new trailer', 'https://new.example/2', summary='A new trailer was released for the anime project.')
    repeat['_story_id'] = bot._story_id(repeat)
    assert store.classify_update(repeat) is None


def test_replay_buffer_is_bounded_and_stable(tmp_path):
    buf = bot.ReplayBuffer(tmp_path / 'replay.json', max_items=2)
    first = _news('First story', 'https://e/1')
    rid1 = buf.capture(first)
    assert buf.capture(first) == rid1
    buf.capture(_news('Second story', 'https://e/2'))
    buf.capture(_news('Third story', 'https://e/3'))
    assert len(buf.latest(10)) == 2
    assert buf.get(rid1) is None


@pytest.mark.asyncio
async def test_replay_bypass_does_not_release_existing_ledger_claim(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'sent.json')
    news = _news('Concurrent replay story', 'https://e/replay')
    assert await store.claim(news['link'], news['title'])
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_post', AsyncMock(return_value=True))
    result = await bot.send_news(object(), news, chat_id=123, track_history=False,
                                 bypass_history_checks=True, apply_dedup=False,
                                 llm_side_effects=False)
    assert result == 'sent'
    assert news['link'] in store  # the original concurrent claim is untouched


def _llm_settings():
    return SimpleNamespace(
        llm_read_article=False, llm_filter=True, llm_skip_filler=True,
        llm_dedup_subject=False, llm_limit_repeats=False,
        llm_rewrite=True, llm_tags=True,
    )


@pytest.mark.asyncio
async def test_llm_enrich_records_prompt_version(monkeypatch):
    monkeypatch.setattr(bot, 'settings', _llm_settings())
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, 'entity_memory', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', False)
    # **kwargs: _llm_call получил task= для маршрутизации задач по провайдерам
    async def call(_messages, max_tokens=bot.LLM_MAX_TOKENS, **kwargs):
        return json.dumps({'topic': 'аниме', 'kind': 'новость', 'subject': 'Frieren',
                           'title': 'Frieren получила новый постер', 'summary': '', 'tags': ['аниме']})
    monkeypatch.setattr(bot, '_llm_call', call)
    news = _news('Frieren gets a new visual', summary='Official visual for the anime project was revealed by the production team.')
    assert await bot._llm_enrich(news) == 'ok'
    assert news['_prompt_version'] == bot.LLM_PROMPT_VERSION
    assert news['_llm_text']


@pytest.mark.asyncio
async def test_llm_judge_rejection_falls_back_without_skipping(monkeypatch):
    monkeypatch.setattr(bot, 'settings', _llm_settings())
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, 'entity_memory', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', True)
    calls = {'n': 0}
    # **kwargs: _llm_call получил task= для маршрутизации задач по провайдерам
    async def call(_messages, max_tokens=bot.LLM_MAX_TOKENS, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return json.dumps({'topic': 'аниме', 'kind': 'новость', 'subject': 'Frieren',
                               'title': 'Frieren получила новый постер', 'summary': '', 'tags': []})
        return json.dumps({'approved': False, 'reason': 'Добавлена неподтверждённая платформа'})
    monkeypatch.setattr(bot, '_llm_call', call)
    news = _news('Frieren gets a new visual', summary='Official visual for the anime project was revealed by the production team.')
    assert await bot._llm_enrich(news) == 'ok'
    assert news['_llm_judge_status'] == 'rejected'
    assert '_llm_text' not in news


def test_moderation_feedback_carries_quality_metadata(tmp_path):
    feedback = bot.ModerationFeedback(tmp_path / 'feedback.json')
    news = _news('Test')
    news.update({'_story_id': 'story123', '_prompt_version': 'pv7', '_confidence_score': 0.83})
    feedback.record('published', news)
    row = feedback._events[-1]
    assert row['story_id'] == 'story123'
    assert row['prompt_version'] == 'pv7'
    assert row['confidence'] == pytest.approx(0.83)


def test_stage2_feature_flags_exist():
    for name in ('editorial_glossary', 'entity_memory', 'llm_judge', 'story_updates', 'replay', 'golden_dataset'):
        assert name in bot.FEATURE_FLAGS


def test_golden_editorial_dataset(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    data = json.loads((Path(__file__).parents[1] / 'golden' / 'editorial_cases.json').read_text(encoding='utf-8'))
    assert data['schema_version'] == 1
    assert len(data['cases']) >= 6
    for case in data['cases']:
        if case['kind'] == 'cluster':
            a = _news(case['a'], 'https://a.example/' + case['id'])
            b = _news(case['b'], 'https://b.example/' + case['id'])
            same = bot._story_similarity(a, b) >= bot.STORY_CLUSTER_SIMILARITY
            assert same is case['expected_same_story'], case['id']
        elif case['kind'] == 'update':
            store = bot.PublishedStoryStore(tmp_path / (case['id'] + '.json'))
            old = _news(case['old_title'], 'https://old.example/' + case['id'], summary=case['old_summary'])
            old['_story_id'] = bot._story_id(old)
            store.record(old)
            new_item = _news(case['new_title'], 'https://new.example/' + case['id'], summary=case['new_summary'])
            new_item['_story_id'] = bot._story_id(new_item)
            assert (store.classify_update(new_item) is not None) is case['expected_update'], case['id']
        elif case['kind'] == 'glossary':
            g = bot.EditorialGlossary(tmp_path / ('golden-' + case['id'] + '.json'))
            g._aliases = {case['alias']: case['preferred']}
            assert g.apply(case['input']) == case['expected'], case['id']

@pytest.mark.asyncio
async def test_story_update_can_reuse_old_visible_title_with_new_url(tmp_path, monkeypatch):
    store = bot.SentLinksStore(tmp_path / 'sent-update.json')
    title = 'Witch Hat Atelier anime gets a new trailer'
    assert await store.claim('https://old.example/story', title)
    assert await store.mark_sending('https://old.example/story')
    assert await store.commit('https://old.example/story', title)
    monkeypatch.setattr(bot, 'sent_links', store)
    monkeypatch.setattr(bot, 'matches_keywords', lambda _n: True)
    monkeypatch.setattr(bot, '_prepare_news_for_send', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_channel_post', AsyncMock(return_value=True))
    monkeypatch.setattr(bot, 'story_history', None)
    monkeypatch.setattr(bot, 'stats', SimpleNamespace(
        record_skipped=AsyncMock(), record_failed_send=AsyncMock(), record_published=AsyncMock()))
    news = _news(title, 'https://new.example/story')
    news['_story_update_of'] = 'previous-story'
    assert await bot.send_news(object(), news) == 'sent'
    assert news['link'] in store

def test_runtime_golden_runner_passes_shipped_dataset(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    passed, total, failures = bot._run_golden_dataset()
    assert total >= 8
    assert passed == total
    assert failures == []
