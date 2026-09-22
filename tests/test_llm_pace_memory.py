"""Выученный темп провайдера переживает перезапуск.

Раньше темп жил только в памяти. После каждого перезапуска бот заново
нащупывал предел запасного провайдера: около шести отказов 429 подряд, и
каждый списывался с дневного лимита — провайдер запрос посчитал.
"""
import json
import time

import pytest

import anime_news_bot as bot


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'LLM_PACE_FILE', tmp_path / 'llm_pace.json')
    monkeypatch.setattr(bot, '_llm_pace', {})
    monkeypatch.setattr(bot, '_llm_pace_restored', False)
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 1.2)
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MIN_INTERVAL', 0.0)
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://api.mistral.ai/v1')
    return tmp_path / 'llm_pace.json'


def _restart(monkeypatch):
    """Новый процесс: память пуста, файл на месте."""
    monkeypatch.setattr(bot, '_llm_pace', {})
    monkeypatch.setattr(bot, '_llm_pace_restored', False)


class TestPaceSurvivesRestart:
    def test_learned_pace_comes_back(self, fresh, monkeypatch):
        for _ in range(5):
            bot._llm_pace_slower('fallback')
        learned = bot._llm_pace_for('fallback')
        assert learned > 30
        _restart(monkeypatch)
        assert bot._llm_pace_for('fallback') == pytest.approx(learned, rel=0.01)

    def test_another_provider_in_the_slot_starts_fresh(self, fresh, monkeypatch):
        """Слоту сменили провайдера — чужой темп к новому не относится."""
        for _ in range(5):
            bot._llm_pace_slower('fallback')
        _restart(monkeypatch)
        monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://api.groq.com/openai/v1')
        assert bot._llm_pace_for('fallback') == pytest.approx(1.2)

    def test_a_day_old_pace_is_forgotten(self, fresh, monkeypatch):
        bot._llm_pace_slower('fallback')
        data = json.loads(fresh.read_text(encoding='utf-8'))
        data['fallback']['at'] = time.time() - bot.LLM_PACE_REMEMBER_SEC - 60
        fresh.write_text(json.dumps(data), encoding='utf-8')
        _restart(monkeypatch)
        assert bot._llm_pace_for('fallback') == pytest.approx(1.2)

    def test_recovery_is_remembered_too(self, fresh, monkeypatch):
        """Провайдер снова отвечает — ускорение тоже переживает перезапуск."""
        for _ in range(3):
            bot._llm_pace_slower('fallback')
        slow = bot._llm_pace_for('fallback')
        bot._llm_pace_faster('fallback')
        faster = bot._llm_pace_for('fallback')
        assert faster < slow
        _restart(monkeypatch)
        assert bot._llm_pace_for('fallback') == pytest.approx(faster, rel=0.01)

    @pytest.mark.parametrize('content', ['не json', '[]', '{"fallback": {"pace": "x"}}',
                                         '{"fallback": {"pace": 9999, "at": 0, "url": ""}}'])
    def test_a_broken_file_does_not_break_the_bot(self, fresh, monkeypatch, content):
        fresh.write_text(content, encoding='utf-8')
        _restart(monkeypatch)
        assert bot._llm_pace_for('fallback') == pytest.approx(1.2)


    @pytest.mark.parametrize('pace', [9999, -5, 0, float('nan')])
    def test_an_absurd_pace_is_rejected(self, fresh, monkeypatch, pace):
        """Свежая запись для того же адреса, но нелепый темп: час между
        запросами выключил бы провайдера, отрицательный — снял бы паузу."""
        fresh.write_text(json.dumps({'fallback': {
            'pace': pace, 'at': time.time(), 'url': 'https://api.mistral.ai/v1'}}), encoding='utf-8')
        _restart(monkeypatch)
        assert bot._llm_pace_for('fallback') == pytest.approx(1.2)


class TestSlotFloor:
    def test_fallback_floor_slows_only_the_fallback(self, fresh, monkeypatch):
        """Общий LLM_MIN_INTERVAL тормозил бы и основной Groq."""
        monkeypatch.setattr(bot, 'LLM_FALLBACK_MIN_INTERVAL', 30.0)
        assert bot._llm_pace_for('fallback') == pytest.approx(30.0)
        assert bot._llm_pace_for('primary') == pytest.approx(1.2)

    def test_the_floor_holds_even_after_recovery(self, fresh, monkeypatch):
        monkeypatch.setattr(bot, 'LLM_FALLBACK_MIN_INTERVAL', 30.0)
        bot._llm_pace_slower('fallback')
        for _ in range(50):
            bot._llm_pace_faster('fallback')
        assert bot._llm_pace_for('fallback') >= 30.0
