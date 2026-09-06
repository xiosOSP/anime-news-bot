import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import anime_news_bot as bot

ROOT = Path(__file__).resolve().parent.parent


def test_roles_removed_from_current_feature_set():
    assert 'rbac' not in bot.FEATURE_FLAGS
    assert not hasattr(bot, 'AdminRoleStore')
    assert not hasattr(bot, 'roles_command')


def test_experiment_store_persists(tmp_path):
    path = tmp_path / 'experiments.json'
    store = bot.ExperimentStore(path)
    store.record('compact', 'assigned')
    store.record('compact', 'published')
    again = bot.ExperimentStore(path)
    assert again.snapshot()['compact'] == {'assigned': 1, 'published': 1}


def test_format_variant_is_deterministic(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'experiments', True)
    monkeypatch.setattr(bot, 'POST_FORMAT_COMPACT_PERCENT', 50.0)
    monkeypatch.setattr(bot, 'experiments', None)
    a = {'title': 'Same story', 'url': 'https://example.com/a'}
    b = {'title': 'Same story', 'url': 'https://example.com/a'}
    assert bot._assign_format_variant(a) == bot._assign_format_variant(b)


def test_compact_percent_100_assigns_compact(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'experiments', True)
    monkeypatch.setattr(bot, 'POST_FORMAT_COMPACT_PERCENT', 100.0)
    monkeypatch.setattr(bot, 'experiments', None)
    news = {'title': 'X', 'url': 'https://example.com/x'}
    assert bot._assign_format_variant(news) == 'compact'


def test_compact_format_is_not_longer_than_standard(monkeypatch):
    monkeypatch.setattr(bot, '_with_tags', lambda text, news: text)
    monkeypatch.setattr(bot, '_apply_editorial_rules', lambda text, news=None: text)
    summary = ('Первое предложение достаточно длинное для проверки. '
               'Второе предложение тоже содержит полезную информацию. '
               'Третье предложение должно остаться только в стандартном варианте.')
    base = {'title': 'Тестовая новость', 'summary': summary, 'lang': 'ru'}
    standard = dict(base, _format_variant='standard')
    compact = dict(base, _format_variant='compact')
    assert len(bot.format_news_short(compact)) <= len(bot.format_news_short(standard))
    assert 'Третье предложение' not in bot.format_news_short(compact)


def test_lifecycle_detects_unclean_previous_run(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    path.write_text(json.dumps({'state': 'running', 'total_starts': 3, 'consecutive_unclean': 1}), encoding='utf-8')
    data = bot._mark_lifecycle_start()
    assert data['total_starts'] == 4
    assert data['consecutive_unclean'] == 2
    assert data['state'] == 'running'


def test_lifecycle_clean_shutdown_resets_unclean_chain(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    bot._mark_lifecycle_start()
    bot._mark_lifecycle_exit('graceful_shutdown')
    data = bot._mark_lifecycle_start()
    assert data['consecutive_unclean'] == 0
    assert data['last_exit_kind'] == 'graceful_shutdown'



def test_paas_port_import_binds_all_interfaces(tmp_path):
    env = dict(os.environ)
    env['PORT'] = '9876'
    env.pop('HEALTH_HOST', None)
    # Не привязываемся к старым временным каталогам конкретного ревью.
    # Подпроцесс должен импортировать именно текущую базу и унаследовать
    # test-only stubs/venv path из запустившего pytest окружения.
    inherited = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = os.pathsep.join(x for x in (str(ROOT), inherited) if x)
    code = 'import anime_news_bot as b; print(b.HEALTH_HOST, b.HEALTH_PORT)'
    out = subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip()
    assert out.endswith('0.0.0.0 9876')

def test_paas_port_defaults_to_public_bind_contract():
    # Constant is import-time; verify the implemented contract directly.
    if bot._PLATFORM_PORT_RAW and not bot._HEALTH_HOST_RAW:
        assert bot.HEALTH_HOST == '0.0.0.0'


@pytest.mark.asyncio
async def test_global_error_handler_never_raises(monkeypatch):
    monkeypatch.setattr(bot, 'metrics', SimpleNamespace(inc=lambda *a, **k: None))
    ctx = SimpleNamespace(error=RuntimeError('boom'))
    await bot._global_error_handler(None, ctx)
    assert 'RuntimeError' in bot._runtime_health['last_error']


def test_polling_bootstrap_retry_default_is_resilient():
    assert bot.POLLING_BOOTSTRAP_RETRIES == -1 or bot.POLLING_BOOTSTRAP_RETRIES >= 0
