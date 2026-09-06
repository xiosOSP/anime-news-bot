import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


def test_stage6_feature_flags_exist():
    for name in ('admin_audit', 'config_reload', 'backup_verify', 'canary_publish'):
        assert name in bot.FEATURE_FLAGS


def test_admin_audit_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'admin_audit', True)
    audit = bot.AdminAuditLog(tmp_path / 'audit.jsonl', max_bytes=100000)
    actor = SimpleNamespace(id=123, username='operator', full_name='Operator')
    audit.record('command:test', actor, role='admin', secret='x' * 1000)
    rows = audit.tail(10)
    assert len(rows) == 1
    assert rows[0]['actor_id'] == 123
    assert rows[0]['action'] == 'command:test'
    assert len(rows[0]['details']['secret']) <= 300


def test_admin_audit_rotates(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'admin_audit', True)
    path = tmp_path / 'audit.jsonl'
    audit = bot.AdminAuditLog(path, max_bytes=1024, backups=2)
    actor = SimpleNamespace(id=1, username='op', full_name='op')
    for i in range(80):
        audit.record('callback', actor, payload='x' * 200, n=i)
    assert path.exists()
    assert Path(str(path) + '.1').exists()


def _zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_backup_verify_accepts_valid_json_archive(monkeypatch):
    monkeypatch.setattr(bot, 'BACKUP_VERIFY_MAX_BYTES', 1024 * 1024)
    data = _zip_bytes({'bot_settings.json': json.dumps({'x': 1}), 'sent_links.json': '[]'})
    result = bot._verify_backup_archive(data)
    assert result['ok'] is True
    assert result['files'] == 2
    assert result['json_files'] == 2


def test_backup_verify_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(bot, 'BACKUP_VERIFY_MAX_BYTES', 1024 * 1024)
    data = _zip_bytes({'bot_settings.json': '{bad json'})
    result = bot._verify_backup_archive(data)
    assert result['ok'] is False
    assert any('invalid json' in err for err in result['errors'])


def test_backup_verify_rejects_path_traversal(monkeypatch):
    monkeypatch.setattr(bot, 'BACKUP_VERIFY_MAX_BYTES', 1024 * 1024)
    data = _zip_bytes({'../escape.json': '{}'})
    result = bot._verify_backup_archive(data)
    assert result['ok'] is False
    assert any('unsafe path' in err for err in result['errors'])


def test_safe_reload_is_atomic_on_broken_json(tmp_path, monkeypatch):
    settings_path = tmp_path / 'bot_settings.json'
    settings_path.write_text(json.dumps({'check_interval_min': 55}), encoding='utf-8')
    rules_path = tmp_path / 'editorial_rules.json'
    rules_path.write_text('{broken', encoding='utf-8')
    glossary_path = tmp_path / 'editorial_glossary.json'
    entity_path = tmp_path / 'entity_memory.json'
    glossary_path.write_text('{}', encoding='utf-8')
    entity_path.write_text('{}', encoding='utf-8')
    old_settings = SimpleNamespace(marker='old')
    monkeypatch.setattr(bot, 'settings', old_settings)
    monkeypatch.setattr(bot, 'SETTINGS_FILE', settings_path)
    monkeypatch.setattr(bot, 'EDITORIAL_RULES_FILE', rules_path)
    monkeypatch.setattr(bot, 'EDITORIAL_GLOSSARY_FILE', glossary_path)
    monkeypatch.setattr(bot, 'ENTITY_MEMORY_FILE', entity_path)
    with pytest.raises(json.JSONDecodeError):
        bot._reload_safe_runtime_config()
    assert bot.settings is old_settings


def test_safe_reload_reads_valid_runtime_files(tmp_path, monkeypatch):
    paths = {
        'SETTINGS_FILE': tmp_path / 'bot_settings.json',
        'EDITORIAL_RULES_FILE': tmp_path / 'editorial_rules.json',
        'EDITORIAL_GLOSSARY_FILE': tmp_path / 'editorial_glossary.json',
        'ENTITY_MEMORY_FILE': tmp_path / 'entity_memory.json',
    }
    paths['SETTINGS_FILE'].write_text(json.dumps({'check_interval_min': 45}), encoding='utf-8')
    paths['EDITORIAL_RULES_FILE'].write_text(json.dumps({'rules': {'boost': ['official'], 'block': [], 'downrank': [], 'breaking': []}}), encoding='utf-8')
    paths['EDITORIAL_GLOSSARY_FILE'].write_text(json.dumps({'aliases': {'mappa': 'MAPPA'}}), encoding='utf-8')
    paths['ENTITY_MEMORY_FILE'].write_text('{}', encoding='utf-8')
    for name, path in paths.items():
        monkeypatch.setattr(bot, name, path)
    changed = bot._reload_safe_runtime_config()
    assert set(changed) == {'bot_settings', 'editorial_rules', 'editorial_glossary', 'entity_memory'}
    assert bot.settings.check_interval_min == 45
    assert bot.editorial_rules.evaluate({'title': 'Official reveal', 'summary': ''})['adjustment'] > 0


def test_canary_config_requires_distinct_valid_target(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'canary_publish', True)
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001)
    monkeypatch.setattr(bot, 'CANARY_CHANNEL_ID', -1002)
    assert bot._canary_configured() is True
    monkeypatch.setattr(bot, 'CANARY_CHANNEL_ID', -1001)
    assert bot._canary_configured() is False
    monkeypatch.setattr(bot, 'CANARY_CHANNEL_ID', 'bad name')
    assert bot._canary_configured() is False


@pytest.mark.asyncio
async def test_canary_mirror_zero_percent_does_nothing(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'canary_publish', True)
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001)
    monkeypatch.setattr(bot, 'CANARY_CHANNEL_ID', -1002)
    monkeypatch.setattr(bot, 'CANARY_MIRROR_PERCENT', 0.0)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_send_post', send)
    assert await bot._maybe_mirror_canary(object(), {'title': 'X'}) is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_canary_mirror_failure_never_raises(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'canary_publish', True)
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001)
    monkeypatch.setattr(bot, 'CANARY_CHANNEL_ID', -1002)
    monkeypatch.setattr(bot, 'CANARY_MIRROR_PERCENT', 100.0)
    monkeypatch.setattr(bot, '_prepare_video_file', AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_send_post', AsyncMock(side_effect=RuntimeError('canary down')))
    assert await bot._maybe_mirror_canary(object(), {'title': 'X'}) is False

