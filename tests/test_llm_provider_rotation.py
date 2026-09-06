"""Перебор провайдеров и подсказка имени модели.

Реальный случай: основной ответил 404 «нет свободной мощности», запасной —
429 «превышен темп». Оба отказа временные и не связаны друг с другом, но бот
остался вообще без модели. Тесты сторожат то, что из этого следует: круг
перебора должен доходить до последнего настроенного ключа, а подсказку
провайдера про имя модели надо показывать, а не терять.
"""
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def three(monkeypatch):
    for name, value in (
        ('LLM_BASE_URL', 'https://one.test/v1'), ('LLM_API_KEY', 'key-one-aaaa'),
        ('LLM_MODEL', 'deepseek/deepseek-v4-pro-free'),
        ('LLM_FALLBACK_BASE_URL', 'https://two.test/v1'),
        ('LLM_FALLBACK_API_KEY', 'key-two-bbbb'),
        ('LLM_FALLBACK_MODEL', 'mistral-small-latest'),
        ('LLM_FAST_BASE_URL', 'https://three.test/v1'),
        ('LLM_FAST_API_KEY', 'key-three-cccc'), ('LLM_FAST_MODEL', 'llama-3.3-70b'),
        ('_llm_using_fallback', False), ('_llm_candidate', ()),
        ('_llm_tried_candidates', set()), ('_llm_primary_retry_at', 0.0),
        ('_llm_disabled_runtime', False), ('_llm_disabled_reason', ''),
        ('_llm_fail_streak', 0), ('_llm_circuit_until', 0.0),
        ('_llm_failover_level', 0), ('_llm_failover_alert_key', ''),
        ('_queue_admin_alert', lambda _m: None),
    ):
        monkeypatch.setattr(bot, name, value)
    return monkeypatch


def _reply(status, text='{}'):
    r = MagicMock(status_code=status, text=text, headers={})
    r.json.return_value = {'choices': [{'message': {'content': 'ok'}}]}
    return r


def test_rotation_reaches_the_third_provider(three):
    """Третий ключ раньше не использовался никогда.

    Переключение было одноразовым: сходили на запасного — и всё. Когда два
    первых легли по разным временным причинам, бот молчал при живом третьем
    ключе в окружении.
    """
    assert bot._llm_try_failover('capacity', '', 60.0) is True
    assert bot._llm_current()[2] == 'mistral-small-latest'
    assert bot._llm_try_failover('rate_limit', '', 60.0) is True
    assert bot._llm_current()[2] == 'llama-3.3-70b'


def test_rotation_stops_when_everyone_is_tried(three):
    """Круг конечен: по кругу до бесконечности не ходим."""
    assert bot._llm_try_failover('capacity', '', 60.0) is True
    assert bot._llm_try_failover('rate_limit', '', 60.0) is True
    assert bot._llm_try_failover('capacity', '', 60.0) is False


def test_answer_closes_the_round(three):
    """Ответивший провайдер закрывает круг: следующий отказ начинает новый."""
    bot._llm_try_failover('capacity', '', 60.0)
    bot._llm_try_failover('rate_limit', '', 60.0)
    bot._llm_note_primary_recovered()
    assert bot._llm_tried_candidates == set()


def test_manual_switch_clears_the_round(three):
    """Ручное переключение — новая конфигурация, старые попытки не в счёт."""
    bot._llm_try_failover('capacity', '', 60.0)
    bot._llm_reset_provider_state('тест')
    assert bot._llm_tried_candidates == set()
    assert bot._llm_current()[2] == 'deepseek/deepseek-v4-pro-free'


def test_rate_limit_moves_on_instead_of_waiting_alone(three):
    """429 у одного провайдера ничего не говорит об остальных."""
    three.setattr(bot.requests, 'post', lambda *a, **k: _reply(429, 'Rate limit exceeded'))
    bot._llm_request([{'role': 'user', 'content': 'hi'}], 100)
    assert bot._llm_using_fallback is True


# ---------- подсказка имени модели ----------

REAL_404 = ('{"error":{"code":"model_not_found","message":"No available capacity for '
            'model deepseek/deepseek-v4-pro-free right now. Please try again later. '
            'Did you mean deepseek/deepseek-v4-pro-0813?"}}')


def test_suggestion_is_extracted_from_the_answer():
    """Провайдер сам пишет рабочее имя — искать его в документации незачем."""
    assert bot._llm_suggested_model(REAL_404) == 'deepseek/deepseek-v4-pro-0813'


def test_no_suggestion_when_provider_offers_none():
    assert bot._llm_suggested_model('{"error":"rate limited"}') == ''
    assert bot._llm_suggested_model('') == ''


def test_capacity_refusal_stays_temporary_and_carries_the_hint(three):
    """Нет мощности — это «зайди позже», а не «поправь переменные».

    Различать важно: у временного отказа ожидание помогает, у ошибки в имени
    модели — не помогает никогда.
    """
    reason = bot._llm_fatal_reason(404, REAL_404)
    assert reason['reason'] == 'capacity'
    assert reason['suggested_model'] == 'deepseek/deepseek-v4-pro-0813'
    assert '/llmmodel deepseek/deepseek-v4-pro-0813' in reason['admin']


def test_probe_marks_temporary_refusals(three):
    """Проба отличает «подождать» от «чинить настройки»."""
    three.setattr(bot.requests, 'post', lambda *a, **k: _reply(404, REAL_404))
    row = bot._llm_probe_slot('primary')
    assert row['temporary'] is True
    assert row['suggested'] == 'deepseek/deepseek-v4-pro-0813'

    three.setattr(bot.requests, 'post', lambda *a, **k: _reply(429, 'Rate limit exceeded'))
    assert bot._llm_probe_slot('fallback')['temporary'] is True

    three.setattr(bot.requests, 'post', lambda *a, **k: _reply(401, 'bad key'))
    assert bot._llm_probe_slot('fallback')['temporary'] is False


def test_returning_to_primary_starts_a_new_round(three):
    """Круг перебора начинается заново, как только мы снова на основном.

    Вернуть нас туда может и кулдаун, и ручное переключение. Если считать
    только по набору опробованных, после такого возврата перебор застрял бы
    с прошлым набором и не дошёл бы ни до кого.
    """
    assert bot._llm_try_failover('capacity', '', 60.0) is True
    assert bot._llm_try_failover('rate_limit', '', 60.0) is True
    assert bot._llm_try_failover('capacity', '', 60.0) is False   # круг исчерпан

    bot._llm_using_fallback = False        # кулдаун вернул нас к основному
    assert bot._llm_try_failover('capacity', '', 60.0) is True
    assert bot._llm_current()[2] == 'mistral-small-latest'


def test_suggested_name_means_waiting_will_not_help(three):
    """«Did you mean X?» — это «такой модели у меня нет», чем бы ни объяснялось.

    Реальный случай: роутер писал «No available capacity ... try again later»
    про модель, которой в его каталоге уже не было вовсе. Совет «подождать»
    отправлял ждать того, что не наступит.
    """
    reason = bot._llm_fatal_reason(404, REAL_404)
    assert 'ожидание не поможет' in reason['admin']
    assert 'Переменные менять не нужно' not in reason['admin']


def test_plain_capacity_refusal_still_says_to_wait(three):
    """Без подсказки имени отказ по мощности остаётся тем, чем был."""
    body = ('{"error":{"code":"model_not_found","message":"No available capacity '
            'for model x right now. Please try again later."}}')
    reason = bot._llm_fatal_reason(404, body)
    assert reason['reason'] == 'capacity'
    assert 'Переменные менять не нужно' in reason['admin']


def test_openrouter_preset_points_at_a_live_model():
    """Пресет вёл на снятую модель, то есть был готовой поломкой из коробки.

    Имена бесплатных моделей у роутеров живут недолго. Проверить существование
    по сети здесь нельзя, поэтому сторожим хотя бы то, что пресет не остался на
    той, про которую точно известно, что её сняли.
    """
    _, model = bot.LLM_PRESETS['openrouter']
    assert model != 'google/gemma-3-27b-it:free', 'снятая модель вернулась в пресет'
    assert model


def test_presets_do_not_mix_up_catalogs():
    """У каждого роутера свой каталог: имя от соседа даёт 400 invalid_model.

    Ошибка из практики: модель, взятая из каталога openrouter.ai, была
    подставлена провайдеру orcarouter.ai — внешне похожие сервисы, но списки
    моделей у них разные и не пересекаются по именам.
    """
    base_orca, model_orca = bot.LLM_PRESETS['orcarouter']
    base_or, model_or = bot.LLM_PRESETS['openrouter']
    assert 'orcarouter' in base_orca and 'openrouter' in base_or
    assert model_orca != model_or, 'один и тот же id для разных каталогов'


def test_every_preset_has_a_base_url_and_a_model():
    """Пресет без модели — это отказ провайдера при первом же запросе."""
    for name, (base_url, model) in bot.LLM_PRESETS.items():
        assert base_url.startswith('https://'), name
        assert model, f'{name}: пресет без модели'


# ---------- запасные модели на том же ключе ----------

@pytest.fixture
def alternates(three, monkeypatch):
    """Три бесплатные модели одного провайдера — типичный расклад free-тарифа."""
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES',
                        ('qwen/qwen3.8-27b-free', 'tencent/hy3-free'))
    return monkeypatch


def test_own_models_are_tried_before_other_providers(alternates):
    """Сосед по каталогу — самая дешёвая замена: тот же ключ, свои лимиты.

    Идти к другому провайдеру, когда у своего есть живая бесплатная модель,
    значит тратить чужую квоту на пустом месте.
    """
    order = bot._llm_candidates()
    assert order[0] == ('primary', 'deepseek/deepseek-v4-pro-free')
    assert order[1] == ('primary', 'qwen/qwen3.8-27b-free')
    assert order[2] == ('primary', 'tencent/hy3-free')
    assert order[3][0] == 'fallback'


def test_rotation_walks_models_then_providers(alternates):
    """Перебор доходит до конца списка, а не до конца слотов."""
    seen = []
    while bot._llm_try_failover('capacity', '', 60.0):
        seen.append(bot._llm_current()[2])
    assert seen[:2] == ['qwen/qwen3.8-27b-free', 'tencent/hy3-free']
    assert 'mistral-small-latest' in seen
    assert 'llama-3.3-70b' in seen


def test_alternate_keeps_the_key_of_its_own_provider(alternates):
    """Модель соседа по каталогу берётся тем же ключом, а не чужим."""
    bot._llm_try_failover('capacity', '', 60.0)
    base_url, api_key, model = bot._llm_current()
    assert model == 'qwen/qwen3.8-27b-free'
    assert (base_url, api_key) == ('https://one.test/v1', 'key-one-aaaa')


def test_no_alternates_configured_behaves_as_before(three):
    """Без списка запасных ничего не меняется: сразу к другому провайдеру."""
    order = bot._llm_candidates()
    assert order[0][0] == 'primary'
    assert order[1][0] == 'fallback'


def test_duplicate_alternate_is_not_tried_twice(three, monkeypatch):
    """Модель, уже стоящая основной, в списке запасных ничего не добавляет."""
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES',
                        ('deepseek/deepseek-v4-pro-free', 'qwen/qwen3.8-27b-free'))
    order = bot._llm_candidates()
    assert order.count(('primary', 'deepseek/deepseek-v4-pro-free')) == 1
