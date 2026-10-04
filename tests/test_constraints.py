"""Зафиксированные версии зависимостей: развёртывание ставит проверенный набор.

Без этого новое развёртывание брало «самые свежие на сегодня» версии
python-telegram-bot, Pillow, numpy — и бот мог сломаться без единой правки кода.
"""
import importlib.metadata as md
import re
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
FLOATING = {'yt-dlp'}       # YouTube ломает старые версии — нужна свежая


def norm(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement_lines():
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line and not line.startswith('-'):
            yield Requirement(line)


def pin_lines():
    """Строки constraints.txt: (имя, версия, условие или None)."""
    for line in (ROOT / 'constraints.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            req = Requirement(line)
            specs = list(req.specifier)
            assert len(specs) == 1 and specs[0].operator == '==', (
                f'в constraints.txt только точные версии: {line}')
            yield norm(req.name), specs[0].version, req.marker


def pins(python_version: str = None) -> dict:
    """Закреплённые версии для данного Python (по умолчанию — текущего).

    Строки с условием (numpy 2.4 только с 3.11 и т. п.) действуют лишь там,
    где условие выполняется: иначе проверка на 3.10 требовала бы версий,
    которые на 3.10 не ставятся.
    """
    env = {'python_version': python_version} if python_version else None
    out = {}
    for name, version, marker in pin_lines():
        if marker is None or marker.evaluate(env):
            # Две строки на один Python — pip не поставит ничего.
            assert name not in out, f'{name} закреплён дважды для Python {python_version}'
            out[name] = version
    return out


def closure() -> set:
    """Все пакеты, которые тянет requirements.txt (по установленным метаданным)."""
    seen, stack = set(), [(r, set(r.extras)) for r in requirement_lines()]
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


def test_requirements_use_the_constraints_file():
    lines = [l.strip() for l in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines()]
    assert '-c constraints.txt' in lines


def test_every_dependency_is_pinned_except_yt_dlp():
    pinned = pins()
    missing = sorted(closure() - set(pinned) - FLOATING)
    assert not missing, f'без точной версии: {missing}'
    assert not FLOATING & set(pinned), 'yt-dlp закреплять нельзя'
    stale = sorted(set(pinned) - closure())
    assert not stale, f'в constraints.txt лишние пакеты: {stale}'


def test_each_python_gets_exactly_one_version_of_numpy_and_onnxruntime():
    # Хостинг может запустить бота на 3.10 при runtime.txt=3.11: на каждой
    # поддерживаемой версии у пакета ровно одна строка — ни конфликта, ни дыры.
    for python in ('3.10', '3.11', '3.12', '3.13'):
        chosen = pins(python)
        assert {'numpy', 'onnxruntime'} <= set(chosen), python
    assert pins('3.10')['numpy'].startswith('2.2.')
    assert pins('3.11')['numpy'] != pins('3.10')['numpy']


def test_pins_fit_the_requirement_ranges():
    pinned = pins()
    for req in requirement_lines():
        version = pinned.get(norm(req.name))
        if version is not None:
            assert req.specifier.contains(version, prereleases=True), (req, version)


def test_installed_versions_are_the_pinned_ones():
    # В CI requirements.txt ставится вместе с constraints.txt — расхождение
    # значит, что ограничения не применились.
    wrong = {name: (version, md.version(name)) for name, version in pins().items()
             if md.version(name) != version}
    assert not wrong, f'установлено не то, что закреплено: {wrong}'
