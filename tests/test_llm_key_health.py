"""Память об отклонённых ключах и уборка настроек модели.

Живая ситуация: в переменных шесть слотов, ни один не отвечает — 401 у одних,
429 у других. Бот при этом каждый цикл честно обходил все шесть, тратил на
каждый таймаут и слал админу отчёт; после перезапуска (а платформа
перезапускает процесс каждые ~18 минут) всё начиналось сначала.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot as bot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def health(tmp_path, monkeypatch):
    store = bot.LLMKeyHealth(tmp_path / 'health.json')
    monkeypatch.setattr(bot, 'llm_key_health', store)
    return store


class TestKeyMemory:
    def test_rejection_is_remembered_across_restarts(self, health, tmp_path):
        health.remember_rejected('primary', 'sk-dead', 401, 'some-model')
        # Перезапуск: память процесса чистая, файл на месте.
        restored = bot.LLMKeyHealth(tmp_path / 'health.json')
        assert restored.rejected('primary', 'sk-dead', ttl=3600)

    def test_a_new_key_gets_a_new_chance(self, health):
        """Отказ не должен пережить замену ключа.

        Иначе владелец меняет ключ, а бот продолжает считать слот мёртвым —
        и выглядит сломанным ровно после того, как его починили.
        """
        health.remember_rejected('primary', 'sk-dead', 401)
        assert health.rejected('primary', 'sk-dead', ttl=3600)
        assert health.rejected('primary', 'sk-fresh', ttl=3600) is None

    def test_memory_expires(self, health):
        """401 приходит и при исчерпанной квоте — она восстанавливается."""
        health.remember_rejected('primary', 'sk-dead', 401)
        assert health.rejected('primary', 'sk-dead', ttl=0.0) is None

    def test_success_clears_the_mark(self, health):
        health.remember_rejected('primary', 'sk-key', 401)
        health.forget('primary')
        assert health.rejected('primary', 'sk-key', ttl=3600) is None

    def test_the_key_itself_is_never_written_to_disk(self, health, tmp_path):
        """На диске только отпечаток: файл лежит рядом с данными, ключ — секрет."""
        health.remember_rejected('primary', 'sk-super-secret-value', 401)
        raw = (tmp_path / 'health.json').read_text(encoding='utf-8')
        assert 'sk-super-secret-value' not in raw
        assert json.loads(raw)['slots']['primary']['fingerprint']

    def test_broken_file_does_not_block_the_model(self, tmp_path):
        """Битая память — повод пробовать ключи заново, а не молчать."""
        path = tmp_path / 'health.json'
        path.write_text('{ это не json', encoding='utf-8')
        assert bot.LLMKeyHealth(path).rejected('primary', 'sk', ttl=3600) is None

    def test_store_is_capped(self, health):
        for i in range(bot.LLMKeyHealth.MAX_SLOTS + 10):
            health.remember_rejected(f'slot{i}', 'sk', 401)
        assert len(health.snapshot()) <= bot.LLMKeyHealth.MAX_SLOTS


class TestCandidates:
    @pytest.fixture
    def configured(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings', MagicMock(llm_primary_slot='', llm_model_override=''))
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://primary.test/v1')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-primary')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'model-primary')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://backup.test/v1')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'sk-backup')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'model-backup')
        monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ('alt-1',))
        return bot

    def test_rejected_slot_leaves_the_working_queue(self, configured, health):
        assert ('primary', 'model-primary') in configured._llm_candidates()
        health.remember_rejected('primary', 'sk-primary', 401)
        slots = {slot for slot, _ in configured._llm_candidates()}
        assert slots == {'fallback'}, 'мёртвый ключ отнимает попытку у живого слота'

    def test_alternates_of_a_rejected_key_go_too(self, configured, health):
        """Соседняя модель на том же ключе получит тот же 401."""
        health.remember_rejected('primary', 'sk-primary', 401)
        assert all(model != 'alt-1' for _, model in configured._llm_candidates())

    def test_diagnostics_still_sees_the_rejected_slot(self, configured, health):
        """Иначе слот исчезает из бота навсегда: вернуть его нечем."""
        health.remember_rejected('primary', 'sk-primary', 401)
        assert ('primary', 'model-primary') in configured._llm_all_candidates()


class TestCleanupPlan:
    def test_live_slot_is_kept(self):
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'm', 'ok': True, 'status': 200, 'took': 1.2}])
        assert any('LLM_API_KEY' in item for item in plan['keep'])
        assert not plan['replace']

    def test_rejected_key_is_named_with_its_variable(self):
        plan = bot._llm_cleanup_plan([
            {'slot': 'fallback', 'model': 'm', 'ok': False, 'status': 401, 'detail': 'no'}])
        assert any('LLM_FALLBACK_API_KEY' in item for item in plan['replace'])

    def test_quota_is_not_something_to_fix(self):
        """429 — подождать. Совет «поменяй ключ» тут только навредит."""
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'm', 'ok': False, 'status': 429,
             'temporary': True, 'detail': 'rate limited'}])
        assert plan['wait'] and not plan['replace']

    def test_unknown_model_suggests_the_working_one(self):
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'gone-model', 'ok': False, 'status': 404,
             'suggested': 'live-model', 'detail': 'model not found'}])
        assert any('live-model' in item for item in plan['replace'])

    def test_a_working_slot_outweighs_its_failed_alternates(self):
        """Запасная модель того же слота упала — сам слот при этом живой."""
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'main', 'ok': True, 'status': 200, 'took': .5},
            {'slot': 'primary', 'model': 'alt', 'ok': False, 'status': 404, 'detail': 'no'}])
        assert plan['keep'] and not plan['replace']


@pytest.mark.asyncio
async def test_llmclean_tells_what_to_do_when_everything_is_dead(monkeypatch, health):
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    monkeypatch.setattr(bot, '_llm_all_candidates', lambda: [('primary', 'm'), ('fallback', 'm2')])
    # Уборка обязана спрашивать ВСЕХ, включая отвергнутых: рабочий список
    # их уже не содержит, и по нему команда сказала бы «настраивать нечего».
    monkeypatch.setattr(bot, '_llm_candidates', lambda: [])
    monkeypatch.setattr(bot, '_doctor_env_conflicts', lambda: [])
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0.0)
    monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', False)

    def probe(slot, timeout=12.0, model=''):
        return {'slot': slot, 'model': model, 'ok': False, 'status': 401,
                'took': .1, 'detail': 'invalid api key'}

    monkeypatch.setattr(bot, '_llm_probe_slot', probe)
    edit = MagicMock()

    async def edit_text(text, **kwargs):
        edit(text)

    msg = MagicMock()

    async def reply_text(_text):
        return MagicMock(edit_text=edit_text)

    msg.reply_text = reply_text
    await bot.llmclean_command(MagicMock(message=msg), MagicMock(args=[]))
    report = edit.call_args.args[0]
    assert 'LLM_API_KEY' in report and 'LLM_FALLBACK_API_KEY' in report
    # Главное для владельца: бот не встал.
    assert 'без модели' in report and 'не останавливаются' in report


class TestWiring:
    """Память бесполезна, если её не заполняют и не чистят в рабочем пути."""

    @pytest.fixture
    def llm(self, monkeypatch, health):
        monkeypatch.setattr(bot, 'settings', MagicMock(llm_primary_slot='', llm_model_override='',
                                                       llm_enabled=True))
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://primary.test/v1')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-primary')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'model-primary')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', '')
        monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', '')
        monkeypatch.setattr(bot, '_llm_candidate', None)
        monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(bot, '_llm_fail_streak', 0)
        monkeypatch.setattr(bot, 'llm_budget', None)
        return bot

    @staticmethod
    def _reply(status, payload=None, text=''):
        return MagicMock(status_code=status, text=text, headers={},
                         json=lambda: payload or {}, close=lambda: None)

    def test_rejected_key_is_recorded_by_the_real_call(self, llm, health):
        reply = self._reply(401, text='invalid api key')
        with patch.object(llm.requests, 'post', return_value=reply):
            assert llm._llm_request([{'role': 'user', 'content': 'x'}]) is None
        assert health.rejected('primary', 'sk-primary', ttl=3600), \
            'без записи бот пойдёт к тому же ключу после перезапуска'

    def test_a_working_answer_clears_the_mark(self, llm, health):
        health.remember_rejected('primary', 'sk-primary', 401)
        reply = self._reply(200, {'choices': [{'message': {'content': 'ok'}}]})
        with patch.object(llm.requests, 'post', return_value=reply):
            assert llm._llm_request([{'role': 'user', 'content': 'x'}]) == 'ok'
        assert health.rejected('primary', 'sk-primary', ttl=3600) is None

    def test_probe_revives_a_slot_after_the_key_is_fixed(self, llm, health):
        """Единственный путь назад: рабочий путь к отвергнутому слоту не ходит."""
        health.remember_rejected('primary', 'sk-primary', 401)
        reply = self._reply(200, {'choices': [{'message': {'content': 'pong'}}]})
        with patch.object(llm.requests, 'post', return_value=reply):
            assert llm._llm_probe_slot('primary')['ok'] is True
        assert health.rejected('primary', 'sk-primary', ttl=3600) is None

    def test_probe_records_a_rejection_too(self, llm, health):
        reply = self._reply(401, text='invalid api key')
        with patch.object(llm.requests, 'post', return_value=reply):
            assert llm._llm_probe_slot('primary')['ok'] is False
        assert health.rejected('primary', 'sk-primary', ttl=3600)


class TestPresets:
    """Пресет — это то, куда пойдёт ключ, если руками ничего не задавать."""

    # Модели, до которых бесплатный ключ не дотянется. Проверено 11.09.2026 по
    # документации провайдеров; причина у каждой своя, и «снята» — не всегда
    # верное слово.
    UNREACHABLE_ON_FREE = {
        'llama-3.3-70b-versatile': 'у Groq работает, но только на корпоративном '
                                   'тарифе: в таблице бесплатного плана её нет',
        'gemini-2.0-flash': 'по сторонним сводкам потеряла бесплатный доступ '
                            'летом 2026; таблицу Google публикует только в AI Studio',
    }

    def test_presets_do_not_lead_to_models_a_free_key_cannot_reach(self):
        """Пресет в недоступную модель — это «ключ вставил, а не работает».

        Отличить такую поломку от негодного ключа по сообщению провайдера
        почти нельзя: и то и другое приходит как отказ на первом же запросе.
        """
        for provider, (_url, model) in bot.LLM_PRESETS.items():
            assert model not in self.UNREACHABLE_ON_FREE, \
                f'{provider}: {self.UNREACHABLE_ON_FREE.get(model)}'

    def test_every_preset_is_complete(self):
        """Половина пресета хуже его отсутствия: запрос уйдёт в никуда."""
        for provider, pair in bot.LLM_PRESETS.items():
            url, model = pair
            assert url.startswith('https://'), provider
            assert model, provider

    def test_documented_providers_exist_in_presets(self):
        """Справка советует провайдеров именами переменных — они должны быть."""
        doc = (ROOT / 'docs' / 'llm-providers.md').read_text(encoding='utf-8')
        for provider in ('groq', 'mistral', 'gemini', 'openrouter', 'cerebras'):
            assert f'`{provider}`' in doc, f'{provider} не описан в справке'
            assert provider in bot.LLM_PRESETS, f'{provider} советуют, но пресета нет'


class TestEnvIsReadOnlyAtStartup:
    """Правка переменных без перезапуска выглядит как «бот игнорирует».

    Живой случай: провайдера в панели сменили, а бот продолжал показывать
    модель прежнего пресета. Окружение работающего процесса снаружи не
    меняется — панель задаёт переменные следующему процессу, не этому.
    Единственное, чем тут можно помочь, — сказать об этом на том же экране.
    """

    def test_model_screen_names_the_restart(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings', MagicMock(llm_primary_slot='', llm_model_override=''))
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://primary.test/v1')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-primary')
        monkeypatch.setattr(bot, 'LLM_MODEL', 'model-primary')
        view = bot._llm_model_view()
        assert 'перезапуска' in view
        assert 'Переменные прочитаны при запуске' in view

    def test_unconfigured_screen_does_not_promise_a_reload(self, monkeypatch):
        """Когда провайдеров нет, экран другой — и обещать там нечего."""
        monkeypatch.setattr(bot, 'LLM_API_KEY', '')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', '')
        monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', '')
        monkeypatch.setattr(bot, 'LLM_FAST_API_KEY', '')
        assert 'LLM_PROVIDER' in bot._llm_model_view()

    @pytest.mark.parametrize(('spent', 'expected'), [
        (30, 'только что'),
        (5 * 60, '5 мин назад'),
        (3 * 3600 + 12 * 60, '3 ч 12 мин назад'),
    ])
    def test_age_is_human(self, monkeypatch, spent, expected):
        """Возраст важнее точного времени: по нему видно, до правки или после."""
        monkeypatch.setattr(bot, '_process_started_at', bot.time.time() - spent)
        assert expected in bot._env_read_ago()


class TestCleanupPlanSaysEachThingOnce:
    """Две строки об одной переменной читаются как два разных дела."""

    def test_base_url_is_not_listed_twice(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_PROVIDER', 'groq')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.orcarouter.ai/v1')
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', True)
        monkeypatch.setattr(bot, 'LLM_MODEL', bot.LLM_PRESETS['groq'][1])
        monkeypatch.setattr(bot, 'LLM_MODEL_FROM_ENV', False)
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'm', 'ok': False, 'status': 401, 'detail': 'no'}])
        mentions = [item for item in plan['remove'] if 'LLM_BASE_URL' in item]
        assert len(mentions) == 1, plan['remove']
        # Строка обязана остаться конкретной: с адресом, а не общим правилом.
        assert 'orcarouter' in mentions[0]

    def test_general_advice_appears_when_there_is_no_specific_one(self, monkeypatch):
        """Адрес совпадает с пресетом — конфликта нет, но переменная лишняя."""
        monkeypatch.setattr(bot, 'LLM_PROVIDER', 'groq')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', bot.LLM_PRESETS['groq'][0])
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', True)
        monkeypatch.setattr(bot, 'LLM_MODEL', bot.LLM_PRESETS['groq'][1])
        monkeypatch.setattr(bot, 'LLM_MODEL_FROM_ENV', False)
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'm', 'ok': True, 'status': 200, 'took': .4}])
        assert any('LLM_BASE_URL' in item for item in plan['remove'])


class TestWhereTheValueCameFrom:
    """Файл .env заполняет только то, чего в панели нет.

    Отсюда самый обидный случай: переменную из панели удалили, а бот
    продолжает её видеть — значение молча пришло из файла. В окружении
    процесса источник уже не различить, поэтому его надо запомнить при чтении.
    """

    def test_file_fills_only_what_the_panel_left_empty(self, tmp_path, monkeypatch):
        env_file = tmp_path / '.env'
        env_file.write_text('LLM_BASE_URL=https://from-file.test/v1\n'
                            'LLM_PROVIDER=from-file\n', encoding='utf-8')
        monkeypatch.setenv('LLM_PROVIDER', 'from-panel')
        monkeypatch.delenv('LLM_BASE_URL', raising=False)

        loaded = bot._load_dotenv(env_file)

        # Панель сильнее файла — это правильно и так было всегда.
        assert bot.os.environ['LLM_PROVIDER'] == 'from-panel'
        assert 'LLM_PROVIDER' not in loaded
        # А вот отсутствующую в панели переменную файл подставил молча.
        assert bot.os.environ['LLM_BASE_URL'] == 'https://from-file.test/v1'
        assert loaded == {'LLM_BASE_URL'}

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert bot._load_dotenv(tmp_path / 'нет-такого.env') == set()

    def test_doctor_names_the_file_as_the_source(self, monkeypatch):
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL', 'LLM_MODEL', 'BOT_TOKEN'})
        rows = {name: detail for name, ok, detail in bot._doctor_env_conflicts() if not ok}
        detail = rows.get('LLM: переменные из файла .env', '')
        assert 'LLM_BASE_URL' in detail and 'LLM_MODEL' in detail
        # Токен к делу не относится: речь про настройки модели.
        assert 'BOT_TOKEN' not in detail
        assert 'панели' in detail and 'файле' in detail

    def test_cleanup_plan_carries_it_to_the_user(self, monkeypatch):
        """Иначе объяснение осталось бы в /doctor, куда за этим не ходят."""
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL'})
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', False)
        plan = bot._llm_cleanup_plan([
            {'slot': 'primary', 'model': 'm', 'ok': False, 'status': 401, 'detail': 'no'}])
        assert any('.env' in item for item in plan['remove']), plan['remove']

    def test_silence_when_there_is_no_file(self, monkeypatch):
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', set())
        names = [name for name, ok, _ in bot._doctor_env_conflicts() if not ok]
        assert 'LLM: переменные из файла .env' not in names
