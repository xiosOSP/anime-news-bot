"""requirements.txt: самодостаточный и с точными версиями.

Хостинг (Bothost) копирует в образ ОДИН requirements.txt и ставит его до
остального кода. Ссылка «-c constraints.txt» поэтому валила сборку целиком, и
ни одна новая версия бота не деплоилась. Точные версии нужны, чтобы новое
развёртывание не принесло «самые свежие на сегодня» python-telegram-bot,
Pillow, numpy и не сломало бота без единой правки кода.
"""
import importlib.metadata as md
import re
from pathlib import Path

from packaging.requirements import Requirement
import pytest

ROOT = Path(__file__).resolve().parent.parent
# yt-dlp нужен свежий: YouTube ломает старые версии за недели. Версии его
# собственных зависимостей (EJS, deno и т. п.) ведёт сам yt-dlp.
FLOATING_ROOT = 'yt-dlp'
SUPPORTED_PYTHONS = ('3.10', '3.11', '3.12', '3.13')


def norm(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def raw_lines():
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            yield line


def requirements(python_version: str = None):
    """Строки, действующие на данном Python (по умолчанию — на текущем).

    Версия — «3.11» или точная «3.11.4»: часть пакетов выбирается по точной
    (python_full_version), и без неё маркер взял бы версию того Python, на
    котором идут тесты.
    """
    env = None
    if python_version:
        full = python_version if python_version.count('.') >= 2 else python_version + '.99'
        env = {'python_version': '.'.join(full.split('.')[:2]), 'python_full_version': full}
    for line in raw_lines():
        req = Requirement(line)
        if req.marker is None or req.marker.evaluate(env):
            yield req


def pins(python_version: str = None) -> dict:
    """Точные версии для данного Python; плавающий только yt-dlp."""
    out = {}
    for req in requirements(python_version):
        name = norm(req.name)
        if name == FLOATING_ROOT:
            continue
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == '==', (
            f'в requirements.txt только точные версии: {req}')
        # Две строки на один Python — pip не поставит ничего.
        assert name not in out, f'{name} закреплён дважды для Python {python_version}'
        out[name] = specs[0].version
    return out


def closure(roots) -> set:
    """Все пакеты, которые тянут roots (по установленным метаданным)."""
    seen, stack = set(), [(r, set(r.extras)) for r in roots]
    while stack:
        req, extras = stack.pop()
        name = norm(req.name)
        if name in seen and not extras:
            continue
        seen.add(name)
        for raw in md.distribution(req.name).requires or []:
            sub = Requirement(raw)
            if sub.marker and not any(sub.marker.evaluate({'extra': e}) for e in extras | {''}):
                continue
            stack.append((sub, set(sub.extras)))
    return seen


def test_requirements_file_is_self_contained():
    # Никаких ссылок на соседние файлы и путей: при сборке их ещё нет в образе.
    for line in raw_lines():
        assert not line.startswith('-'), f'в requirements.txt нельзя «{line}»'
        assert '/' not in line.split(';', 1)[0], f'путь или URL в requirements.txt: {line}'


def test_yt_dlp_floats_with_youtube_extras():
    (req,) = [r for r in requirements() if norm(r.name) == FLOATING_ROOT]
    assert {'default', 'deno'} <= set(req.extras)
    assert all(spec.operator == '>=' for spec in req.specifier), req


def test_every_dependency_is_pinned_except_yt_dlp_own():
    reqs = list(requirements())
    ours = [r for r in reqs if norm(r.name) != FLOATING_ROOT]
    needed = closure(ours)
    pinned = pins()
    missing = sorted(needed - set(pinned))
    assert not missing, f'без точной версии: {missing}'
    stale = sorted(set(pinned) - needed)
    assert not stale, f'в requirements.txt лишние пакеты: {stale}'


def test_each_python_gets_exactly_one_version_of_numpy_and_onnxruntime():
    # Хостинг может запустить бота на 3.10 при runtime.txt=3.11: на каждой
    # поддерживаемой версии у пакета ровно одна строка — ни конфликта, ни дыры.
    for python in SUPPORTED_PYTHONS:
        chosen = pins(python)
        assert {'numpy', 'onnxruntime'} <= set(chosen), python
    assert pins('3.10')['numpy'].startswith('2.2.')
    assert pins('3.11')['numpy'] != pins('3.10')['numpy']


@pytest.mark.parametrize('python, expected', [
    ('3.10.14', '2.10.0'), ('3.11.0', '2.10.0'), ('3.11.4', '2.10.0'),
    ('3.11.5', '4.0'), ('3.11.14', '4.0'), ('3.12.7', '4.0'), ('3.13.1', '4.0'),
])
def test_soupsieve_follows_the_exact_python_version(python, expected):
    """4.0 закрывает CVE-2026-126190…126193, но ставится только на 3.11.5+.
    Условие по «3.11» дало бы 4.0 на 3.11.0–3.11.4, и сборка на хостинге упала бы."""
    assert pins(python)['soupsieve'] == expected


def test_installed_versions_are_the_pinned_ones():
    # В CI ставится ровно requirements.txt — расхождение значит, что версии
    # в файле не те, на которых прошли тесты.
    wrong = {name: (version, md.version(name)) for name, version in pins().items()
             if md.version(name) != version}
    assert not wrong, f'установлено не то, что закреплено: {wrong}'
