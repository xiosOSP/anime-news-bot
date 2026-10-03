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


def pins() -> dict:
    out = {}
    for line in (ROOT / 'constraints.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            name, _, version = line.partition('==')
            assert version, f'в constraints.txt только точные версии: {line}'
            out[norm(name)] = version.strip()
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
