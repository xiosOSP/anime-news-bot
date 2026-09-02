#!/usr/bin/env python3
"""Пересобирает BUILD_MANIFEST.json по фактическому содержимому репозитория.

Манифест писался руками и разъехался с реальностью: в нём числились файлы,
которых в репозитории нет, и хеш давно изменившегося ``anime_news_bot.py``.
Такой манифест хуже отсутствующего — он выглядит как проверяемая опись, но
проверить по нему ничего нельзя. Поэтому опись теперь генерируется.

Список файлов берётся из ``git ls-files``: что отслеживается git, то и попадает
в манифест, без отдельного списка, который снова придётся синхронизировать.

    python tools/build_manifest.py            # перезаписать манифест
    python tools/build_manifest.py --check    # только сверить, ничего не писать
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'BUILD_MANIFEST.json'

# Сам манифест в опись не входит: его хеш зависел бы от собственного значения.
EXCLUDED = {'BUILD_MANIFEST.json'}

# Поля происхождения задаются человеком и переживают перегенерацию.
PROVENANCE_KEYS = ('schema', 'foundation', 'base_archive', 'base_archive_sha256')


def _git(*args: str) -> str:
    try:
        out = subprocess.run(('git', *args), cwd=ROOT, capture_output=True,
                             text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip()


def _tracked_files() -> list[str]:
    listing = _git('ls-files', '-z')
    if not listing:
        raise SystemExit('git ls-files ничего не вернул: запускайте внутри репозитория')
    names = [name for name in listing.split('\0') if name and name not in EXCLUDED]
    return sorted(names)


def _python_target() -> str:
    """Версия, на которой всё реально запускается, — из runtime.txt."""
    runtime = ROOT / 'runtime.txt'
    if runtime.exists():
        value = runtime.read_text(encoding='utf-8').strip()
        return value.removeprefix('python-') or value
    return f'{sys.version_info.major}.{sys.version_info.minor}'


def build() -> dict:
    previous = {}
    if MANIFEST.exists():
        try:
            previous = json.loads(MANIFEST.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            previous = {}

    files = {}
    for name in _tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue                     # удалённый, но ещё не закоммиченный файл
        data = path.read_bytes()
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}

    manifest = {key: previous.get(key) for key in PROVENANCE_KEYS if key in previous}
    manifest.setdefault('schema', 1)
    manifest.update({
        'python_target': _python_target(),
        'git_commit': _git('rev-parse', 'HEAD') or None,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'files': files,
    })
    return manifest


def main() -> int:
    check_only = '--check' in sys.argv[1:]
    manifest = build()

    if check_only:
        if not MANIFEST.exists():
            print('BUILD_MANIFEST.json отсутствует')
            return 1
        current = json.loads(MANIFEST.read_text(encoding='utf-8'))
        stale = sorted(set(current.get('files') or {}) - set(manifest['files']))
        missing = sorted(set(manifest['files']) - set(current.get('files') or {}))
        if stale:
            print(f'В манифесте числятся отсутствующие файлы: {stale}')
        if missing:
            print(f'В манифест не попали файлы репозитория: {missing}')
        return 1 if (stale or missing) else 0

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
    print(f'BUILD_MANIFEST.json пересобран: {len(manifest["files"])} файлов')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
