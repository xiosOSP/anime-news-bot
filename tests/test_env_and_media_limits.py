"""Настройки окружения и потолок памяти проверки медиа.

Обе темы с одного разбора: на хостинге 2 ГБ памяти, а воркер просил лимит в
3 ГБ — больше, чем есть у машины. Такой лимит не срабатывает никогда, и вместо
аккуратного отказа одной проверки жертву выбирает ядро. Рядом — набор
переменных: ошибка в нём не выглядит ошибкой, бот работает, просто не так, как
думает владелец.
"""
import importlib.util
import json
from pathlib import Path

import pytest

import anime_news_bot as bot
import moderation_media as media

ROOT = Path(__file__).resolve().parents[1]


class TestWorkerMemoryCeiling:
    def test_default_fits_a_small_host(self):
        """Лимит больше памяти машины — это отсутствие лимита."""
        assert media.WORKER_MEMORY_MB_DEFAULT < 2048

    def test_too_small_a_limit_is_raised_to_what_works(self):
        """Замерено: на 1024 МБ детектор не поднимается, на 1536 — да.

        Настройка ниже этого порога молча превращала бы каждую проверку в
        отказ, то есть выключала бы защиту, выглядя как её настройка.
        """
        assert media.clamp_worker_memory(64) == media.WORKER_MEMORY_MB_MIN
        assert media.clamp_worker_memory('мусор') == media.WORKER_MEMORY_MB_DEFAULT
        assert media.clamp_worker_memory(4096) == 4096

    def test_worker_is_told_the_limit(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs.get('env') or {})
            return type('R', (), {'returncode': 0, 'stdout': '{"status": "checked"}', 'stderr': ''})()

        monkeypatch.setattr(media.subprocess, 'run', fake_run)
        media.run_worker('file', 'image', 5, .8, .85, 2048)
        assert seen[media.WORKER_MEMORY_ENV] == '2048'

    def test_scanner_passes_its_limit_down(self):
        assert media.MediaScanner(memory_mb=64).memory_mb == media.WORKER_MEMORY_MB_MIN

    @pytest.mark.parametrize('text', ['std::bad_alloc', 'Cannot allocate memory'])
    def test_memory_failure_is_named_not_hidden(self, text, tmp_path, monkeypatch):
        """onnxruntime говорит про память std::bad_alloc, а не MemoryError.

        Без разбора текста нехватка памяти выглядела бы как «ошибка
        декодирования», и владелец чинил бы формат файла вместо хостинга.
        """
        script = ROOT / 'moderation_media.py'
        source = script.read_text(encoding='utf-8')
        # Запускаем настоящий __main__ воркера, подсунув падающий scan_file.
        probe = tmp_path / 'worker.py'
        probe.write_text(source.replace(
            'scan = scan_file(sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))',
            f'raise RuntimeError({text!r})'), encoding='utf-8')
        result = media.subprocess.run(
            [media.sys.executable, str(probe), 'file', 'image', '0.8', '0.85'],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0
        # Ответ воркера экранирован в ASCII — сверяем разобранный, а не текст.
        assert 'памяти' in json.loads(result.stdout)['reason'], result.stdout
        # Трассировка обязана дойти до stderr: /mediaping показывает именно её.
        assert 'RuntimeError' in result.stderr


class TestEnvConflicts:
    """/doctor обязан ловить набор переменных, который отменяет сам себя."""

    def test_manual_url_overriding_a_preset_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_PROVIDER', 'tokenator')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://api.orcarouter.ai/v1')
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', True)
        monkeypatch.setattr(bot, 'LLM_MODEL', bot.LLM_PRESETS['tokenator'][1])
        monkeypatch.setattr(bot, 'LLM_MODEL_FROM_ENV', False)
        rows = {name: (ok, detail) for name, ok, detail in bot._doctor_env_conflicts()}
        ok, detail = rows['LLM: ручные адрес и модель']
        assert ok is False
        assert 'orcarouter' in detail and 'tokenator' in detail

    def test_preset_alone_is_not_a_complaint(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_PROVIDER', 'tokenator')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', bot.LLM_PRESETS['tokenator'][0])
        monkeypatch.setattr(bot, 'LLM_MODEL', bot.LLM_PRESETS['tokenator'][1])
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', False)
        monkeypatch.setattr(bot, 'LLM_MODEL_FROM_ENV', False)
        names = [name for name, ok, _ in bot._doctor_env_conflicts() if not ok]
        assert 'LLM: ручные адрес и модель' not in names

    def test_same_value_as_the_preset_is_not_a_conflict(self, monkeypatch):
        """Переменная, повторяющая пресет, ничего не ломает — молчим о ней."""
        monkeypatch.setattr(bot, 'LLM_PROVIDER', 'tokenator')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', bot.LLM_PRESETS['tokenator'][0])
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', True)
        monkeypatch.setattr(bot, 'LLM_MODEL', bot.LLM_PRESETS['tokenator'][1])
        monkeypatch.setattr(bot, 'LLM_MODEL_FROM_ENV', True)
        names = [name for name, ok, _ in bot._doctor_env_conflicts() if not ok]
        assert 'LLM: ручные адрес и модель' not in names

    def test_key_without_provider_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_PROVIDER', '')
        monkeypatch.setattr(bot, 'LLM_API_KEY', 'sk-test')
        monkeypatch.setattr(bot, 'LLM_BASE_URL', '')
        monkeypatch.setattr(bot, 'LLM_BASE_URL_FROM_ENV', False)
        assert any(name == 'LLM_PROVIDER' and not ok
                   for name, ok, _ in bot._doctor_env_conflicts())

    def test_moderation_key_without_model_is_reported(self, monkeypatch):
        monkeypatch.setattr(bot, 'MODERATION_LLM_API_KEY', 'sk-test')
        monkeypatch.setattr(bot, 'MODERATION_LLM_BASE_URL', 'https://api.tokenator.top/v1')
        monkeypatch.setattr(bot, 'MODERATION_LLM_MODEL', '')
        assert any(name == 'Модель модерации' and not ok
                   for name, ok, _ in bot._doctor_env_conflicts())

    def test_disabled_media_check_is_said_out_loud(self, monkeypatch):
        monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', False)
        rows = {name: detail for name, ok, detail in bot._doctor_env_conflicts() if not ok}
        assert '18+' in rows.get('Проверка медиа', '')

    def test_doctor_shows_these_rows(self, monkeypatch):
        monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', False)
        names = [check['name'] for check in bot._doctor_local_checks()]
        assert 'Проверка медиа' in names


class TestEnvReference:
    """Справочник переменных не имеет права отставать от кода."""

    @staticmethod
    def _module():
        spec = importlib.util.spec_from_file_location(
            'env_reference_test', ROOT / 'tools' / 'env_reference.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_reader_finds_variables_of_every_shape(self, tmp_path):
        module = self._module()
        (tmp_path / 'sample.py').write_text(
            "x = _env('PLAIN_NAME', 'default')\n"
            "y = _env_int('NUMERIC_NAME', 42)\n"
            "z = os.getenv('BARE_NAME')\n"
            "ignored = _env(name, 'default')\n", encoding='utf-8')
        found = module.collect(tmp_path)
        assert set(found) == {'PLAIN_NAME', 'NUMERIC_NAME', 'BARE_NAME'}
        assert found['NUMERIC_NAME']['default'] == 42

    def test_committed_reference_matches_the_code(self):
        module = self._module()
        found = module.collect()
        text = (ROOT / 'docs' / 'env-reference.md').read_text(encoding='utf-8')
        missing = [name for name in found if f'`{name}`' not in text]
        assert not missing, f'не описаны: {missing[:5]}'

    def test_new_media_switches_are_documented(self):
        """Переменные, добавленные этой правкой, должны быть в примере."""
        example = (ROOT / '.env.example').read_text(encoding='utf-8')
        assert 'MODERATION_MEDIA_QUEUE' in example
        assert 'MODERATION_MEDIA_MEMORY_MB' in example
