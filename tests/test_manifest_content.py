import hashlib
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def manifest_module():
    path = Path(__file__).resolve().parents[1] / 'tools/build_manifest.py'
    spec = importlib.util.spec_from_file_location('manifest_content_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_crlf_checkout_has_same_content_hash(manifest_module, tmp_path, monkeypatch):
    path = tmp_path / 'app.py'
    monkeypatch.setattr(manifest_module, 'ROOT', tmp_path)
    monkeypatch.setattr(manifest_module, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(manifest_module, '_tracked_files', lambda: ['app.py'])
    path.write_bytes(b'print("hello")\n')
    expected = manifest_module.build()['files']['app.py']
    path.write_bytes(b'print("hello")\r\n')
    assert manifest_module.build()['files']['app.py'] == expected
    path.write_bytes(b'print("other")\r\n')
    assert manifest_module.build()['files']['app.py']['sha256'] != expected['sha256']


@pytest.mark.parametrize('data', [b'\0\r\n\xff', b'\xff\r\n'])
def test_binary_content_remains_exact(manifest_module, tmp_path, data):
    path = tmp_path / 'binary'
    path.write_bytes(data)
    assert hashlib.sha256(manifest_module._content_bytes(path)).digest() == hashlib.sha256(data).digest()


def test_rebuild_refuses_while_files_are_untracked(manifest_module, tmp_path, monkeypatch, capsys):
    """Опись, собранная до git add, неполна — и выяснится это только на CI.

    Она строится по ``git ls-files``, поэтому файл, попавший в коммит уже
    после пересборки, в неё не входит. Локально всё сходится: файла нет ни в
    описи, ни в git. На CI он отслеживается — и сборка падает. Так и вышло:
    три файла остались неописанными, main покраснел.
    """
    monkeypatch.setattr(manifest_module, 'ROOT', tmp_path)
    monkeypatch.setattr(manifest_module, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(manifest_module, '_tracked_files', lambda: [])
    monkeypatch.setattr(manifest_module, '_git', lambda *args: 'tools/new_tool.py\0')
    monkeypatch.setattr(manifest_module.sys, 'argv', ['build_manifest.py'])

    assert manifest_module.main() == 1
    assert not (tmp_path / 'manifest.json').exists(), 'неполная опись не должна записываться'
    assert 'git add' in capsys.readouterr().out


def test_rebuild_writes_when_everything_is_tracked(manifest_module, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_module, 'ROOT', tmp_path)
    monkeypatch.setattr(manifest_module, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(manifest_module, '_tracked_files', lambda: [])
    monkeypatch.setattr(manifest_module, '_git', lambda *args: '')
    monkeypatch.setattr(manifest_module.sys, 'argv', ['build_manifest.py'])

    assert manifest_module.main() == 0
    assert (tmp_path / 'manifest.json').exists()
