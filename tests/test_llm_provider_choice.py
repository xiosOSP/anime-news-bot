"""Выбор основного провайдера в настройках.

Появилось после суток, которые бот провёл на отказавшем провайдере: запасной
работал, но поменять их местами можно было только правкой переменных
окружения и перезапуском. Тесты сторожат то, что делает переключение
осмысленным, — что оно реально меняет адресата запроса и что после него бот
не тащит за собой решения, принятые о прежнем провайдере.
"""
import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True, scope='module')
def _globals():
    bot._init_globals()
    return True


@pytest.fixture(autouse=True)
def _slots(monkeypatch, tmp_path):
    """Три настроенных провайдера и своё хранилище настроек на каждый тест."""
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://one.example/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'key-one-aaaaaaaa')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-one')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://two.example/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'key-two-bbbbbbbb')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'model-two')
    monkeypatch.setattr(bot, 'LLM_FAST_BASE_URL', 'https://three.example/v1')
    monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', 'key-three-cccccccc')
    monkeypatch.setattr(bot, 'LLM_FAST_MODEL', 'model-three')
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, '_llm_using_fallback', False)
    monkeypatch.setattr(bot, '_llm_primary_retry_at', 0.0)
    return True


def test_default_keeps_environment_roles():
    """Без выбора в настройках всё как было: основной — LLM_PROVIDER."""
    assert bot._llm_primary_slot() == 'primary'
    assert bot._llm_current()[2] == 'model-one'
    assert bot._llm_backup_model() == 'model-two'


def test_switch_changes_who_is_asked():
    """Переключение меняет адресата запроса, а не только надпись."""
    bot.settings.llm_primary_slot = 'fallback'
    base_url, api_key, model = bot._llm_current()
    assert (base_url, api_key, model) == ('https://two.example/v1', 'key-two-bbbbbbbb', 'model-two')


def test_switch_makes_the_old_primary_the_backup():
    """Страховка не теряется: бывший основной становится запасным.

    Иначе переключение на запасного оставляло бы бота вообще без подстраховки,
    хотя рабочий ключ прежнего основного никуда не делся.
    """
    bot.settings.llm_primary_slot = 'fallback'
    assert bot._llm_backup_slot() == 'primary'
    assert bot._llm_backup_model() == 'model-one'


def test_fast_slot_can_stand_in_when_no_fallback(monkeypatch):
    """Быстрый слот тоже страхует.

    Раньше при незаданном LLM_FALLBACK отказ основного выключал модель до
    перезапуска — при том что рабочий ключ в окружении был.
    """
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
    assert bot._llm_backup_slot() == 'fast'
    assert bot._llm_fallback_configured() is True


def test_stale_choice_never_disables_the_model(monkeypatch):
    """Выбор слота, чей ключ убрали, не должен оставить бота без модели."""
    bot.settings.llm_primary_slot = 'fast'
    monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', '')
    assert bot._llm_primary_slot() == 'primary'
    assert bot._llm_current()[2] == 'model-one'


def test_model_override_keeps_provider_credentials():
    """Переопределяется только имя модели: чужой ключ к чужому адресу не клеим."""
    bot.settings.llm_model_override = 'model-one-large'
    base_url, api_key, model = bot._llm_current()
    assert model == 'model-one-large'
    assert (base_url, api_key) == ('https://one.example/v1', 'key-one-aaaaaaaa')


def test_model_override_does_not_leak_into_backup():
    """Имя, заданное для основного, не подставляется запасному."""
    bot.settings.llm_model_override = 'model-one-large'
    assert bot._llm_backup_model() == 'model-two'


def test_switch_revives_a_model_disabled_until_restart(monkeypatch):
    """Главное, ради чего всё затевалось.

    Отказ по ключу выключал модель до перезапуска. Если после этого сменить
    провайдера, а состояние не сбросить, бот продолжит молчать — и админ
    решит, что переключение не работает.
    """
    monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
    monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
    monkeypatch.setattr(bot, '_llm_fail_streak', 9)
    monkeypatch.setattr(bot, '_llm_using_fallback', True)
    assert bot._llm_active() is False
    bot._llm_reset_provider_state('тест')
    assert bot._llm_active() is True
    assert bot._llm_using_fallback is False


def test_unavailable_reason_names_the_cause(monkeypatch):
    """«Недоступна» без причины — тупик: чинить по такой строке нечего."""
    bot.settings.llm_enabled = False
    assert 'выключена' in (bot._llm_unavailable_reason() or '')
    bot.settings.llm_enabled = True
    assert bot._llm_unavailable_reason() is None
    monkeypatch.setattr(bot, '_llm_disabled_runtime', True)
    monkeypatch.setattr(bot, '_llm_disabled_reason', 'auth')
    assert 'ключ' in (bot._llm_unavailable_reason() or '')


def test_every_provider_key_is_redacted():
    """Ответ провайдера уходит админу в личку — ключи из него вырезаем.

    Маскировался только LLM_API_KEY: ключи запасного и быстрого провайдеров
    прошли бы в сообщение целиком.
    """
    leaked = 'rejected: key-one-aaaaaaaa key-two-bbbbbbbb key-three-cccccccc'
    out = bot._redact_secrets(leaked)
    for secret in ('key-one-aaaaaaaa', 'key-two-bbbbbbbb', 'key-three-cccccccc'):
        assert secret not in out
