from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


def test_source_yield_counts_unique_story_credit_per_source(tmp_path):
    store = bot.SourceYieldStore(tmp_path / 'yield.json')
    store.record_fetch('A', raw=10, fresh=5, duplicates=4, no_image=1, duration_sec=0.25)
    store.record_story('story-1', ['A'])
    store.record_story('story-1', ['A'])
    store.record_story('story-1', ['A', 'B'])
    rows = {r['source']: r for r in store.snapshot()}
    assert rows['A']['unique_stories'] == 1
    assert rows['B']['unique_stories'] == 1
    assert rows['A']['raw'] == 10
    assert rows['A']['avg_fetch_ms'] == 250.0


def test_story_registry_carries_independent_sources_across_cycles(tmp_path, monkeypatch):
    store = bot.StoryRegistry(tmp_path / 'stories.json')
    monkeypatch.setattr(bot, 'STORY_CLUSTER_SIMILARITY', 0.88)
    first = {'title': 'Chainsaw Man movie gets October premiere', 'summary': ''}
    second = {'title': 'Chainsaw Man movie gets October premiere', 'summary': ''}
    r1 = store.observe(first, ['Source A'], ['https://a.test/1'])
    r2 = store.observe(second, ['Source B'], ['https://b.test/1'])
    assert r1['source_count'] == 1
    assert r2['source_count'] == 2
    assert set(r2['sources']) == {'Source A', 'Source B'}
    assert r2['registry_id'] == r1['registry_id']


def test_pending_posts_value_overflow_keeps_stronger_candidate(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'value_moderation_queue', True)
    monkeypatch.setattr(bot.PendingPosts, 'MAX_ITEMS', 2)
    monkeypatch.setattr(bot, '_news_priority_score', lambda n: float(n.get('_test_score', 0)))
    monkeypatch.setattr(bot, '_is_official_news', lambda n: False)
    monkeypatch.setattr(bot, 'source_yield', None)
    store = bot.PendingPosts(tmp_path / 'pending.json')
    store.add({'title': 'weak', '_test_score': 1})
    store.add({'title': 'strong', '_test_score': 10})
    store.add({'title': 'stronger', '_test_score': 20})
    titles = {v['news']['title'] for v in store._items.values()}
    assert titles == {'strong', 'stronger'}


def test_pending_posts_rejects_new_candidate_if_it_is_weakest(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'value_moderation_queue', True)
    monkeypatch.setattr(bot.PendingPosts, 'MAX_ITEMS', 2)
    monkeypatch.setattr(bot, '_news_priority_score', lambda n: float(n.get('_test_score', 0)))
    monkeypatch.setattr(bot, '_is_official_news', lambda n: False)
    monkeypatch.setattr(bot, 'source_yield', None)
    store = bot.PendingPosts(tmp_path / 'pending.json')
    store.add({'title': 'a', '_test_score': 10})
    store.add({'title': 'b', '_test_score': 20})
    with pytest.raises(OverflowError):
        store.add({'title': 'weak-new', '_test_score': -5})
    titles = {v['news']['title'] for v in store._items.values()}
    assert titles == {'a', 'b'}


def test_fast_route_selection_is_task_specific(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_quality_routing', True)
    monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', 'x')
    monkeypatch.setattr(bot, 'LLM_FAST_BASE_URL', 'https://fast.test')
    monkeypatch.setattr(bot, 'LLM_FAST_MODEL', 'fast-model')
    monkeypatch.setattr(bot, 'LLM_FAST_TASKS', {'judge'})
    assert bot._llm_route_for('judge') == ('https://fast.test', 'x', 'fast-model')
    assert bot._llm_route_for('editorial') is None


class TestFastRouteActuallyRoutes:
    """Быстрый маршрут должен уходить к другому провайдеру, а не к основному.

    В первой версии Stage 17 `_llm_request` вычислял `route_config`, но затем
    брал конфигурацию через `_llm_current()` — то есть запрос всё равно уходил
    к основной модели, и дешёвый маршрут не использовался вовсе.
    """

    @pytest.fixture(autouse=True)
    def _routes(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'quality-key')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://quality.test/v1')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'quality-model')
        monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', 'fast-key')
        monkeypatch.setattr(bot, 'LLM_FAST_BASE_URL', 'https://fast.test/v1')
        monkeypatch.setattr(bot, 'LLM_FAST_MODEL', 'fast-model')
        monkeypatch.setattr(bot, 'LLM_FAST_TASKS', {'judge'})
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        yield

    def _spy(self, monkeypatch):
        seen = []

        def post(url, **kwargs):
            seen.append(url)
            response = MagicMock()
            response.status_code = 200
            response.headers = {}
            response.text = 'ok'
            response.json.return_value = {
                'choices': [{'message': {'content': 'ответ'}}]}
            return response

        monkeypatch.setattr(bot.requests, 'post', post)
        return seen

    def test_routed_task_hits_the_fast_provider(self, monkeypatch):
        seen = self._spy(monkeypatch)
        bot._llm_request([{'role': 'user', 'content': 'x'}], 100,
                         route_config=bot._llm_route_for('judge'))
        assert 'fast.test' in seen[0], 'быстрый маршрут ушёл не туда'

    def test_main_task_stays_on_quality_provider(self, monkeypatch):
        seen = self._spy(monkeypatch)
        bot._llm_request([{'role': 'user', 'content': 'x'}], 100,
                         route_config=bot._llm_route_for('editorial'))
        assert 'quality.test' in seen[0], 'основная задача ушла на дешёвую модель'

    def test_fast_model_name_is_sent(self, monkeypatch):
        payloads = []

        def post(url, **kwargs):
            payloads.append(kwargs.get('json', {}))
            response = MagicMock()
            response.status_code = 200
            response.headers = {}
            response.text = 'ok'
            response.json.return_value = {
                'choices': [{'message': {'content': 'ответ'}}]}
            return response

        monkeypatch.setattr(bot.requests, 'post', post)
        bot._llm_request([{'role': 'user', 'content': 'x'}], 100,
                         route_config=bot._llm_route_for('judge'))
        assert payloads[0].get('model') == 'fast-model'


class TestNoUndefinedNamesAfterStage17:
    """Вызов несуществующего имени уже ронял авторассылку целиком.

    В первой версии Stage 17 строка с `route_config` попала в
    `_llm_configured()`, где такого параметра нет. Функция вызывается из
    `_llm_active()`, то есть обогащение падало на каждом запросе, а с ним
    117 тестов.
    """

    # Проверка ruff F821 намеренно не дублируется: она уже есть в
    # tests/test_autosend_incident.py. Два параллельных запуска ruff делят один
    # кэш и дают плавающие падения. Здесь проверяем поведение, а не линтер.

    def test_llm_configured_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'key')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://provider.test/v1')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'model')
        assert bot._llm_configured() is True
        assert isinstance(bot._llm_active(), bool)
