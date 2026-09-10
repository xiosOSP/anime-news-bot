#!/usr/bin/env python3
"""Справочник переменных окружения, собранный из самого кода.

Список настроек рос быстрее, чем ``.env.example``: к этому моменту код читал
260 переменных, а описаны были 69. Понять по такому файлу, чего не хватает на
хостинге, нельзя — а именно за этим в него и заглядывают.

Собрать заново::

    python tools/env_reference.py            # переписать docs/env-reference.md
    python tools/env_reference.py --check    # только сверить, ничего не писать

Значения по умолчанию берутся из вызовов ``_env*`` разбором синтаксиса, а не
запуском бота: справочник должен собираться и там, где нет ни токена, ни сети.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / 'docs' / 'env-reference.md'
READERS = {'_env', '_env_int', '_env_bool', '_env_float'}

# Что бот не сможет сделать без переменной. Остальные — точная настройка со
# значением по умолчанию: их задают, когда дефолт не подошёл.
REQUIRED = {
    'BOT_TOKEN': 'без него бот не запустится',
    'ADMIN_ID': 'кому принадлежат кнопки и отчёты',
    'CHANNEL_ID': 'куда публиковать',
    'DATA_DIR': 'где хранить состояние; без постоянного тома оно теряется '
                'при каждом перезапуске',
}

GROUP_TITLES = {
    'LLM': 'Языковая модель новостей',
    'MODERATION': 'Модерация чата',
    'FEATURE': 'Переключатели возможностей',
    'SOURCE': 'Источники новостей',
    'MEDIA': 'Картинки и видео в постах',
    'VIDEO': 'Видео: нормализация и превью',
    'STORY': 'Сюжеты и обновления',
    'ADAPTIVE': 'Адаптивное расписание',
    'ANALYTICS': 'Аналитика',
    'HEALTH': 'Health-порт и метрики',
    'DASHBOARD': 'Админ-дашборд',
    'BACKPRESSURE': 'Защита от перегрузки',
    'VERIFICATION': 'Проверка фактов',
    'POLLING': 'Опрос Telegram',
    'HTTP': 'Сетевые повторы',
    'LOG': 'Логи',
    'EVENT': 'Журнал событий',
}


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def collect(root: Path = ROOT) -> dict[str, dict]:
    """Все переменные окружения, которые читает код, с их значениями."""
    found: dict[str, dict] = {}

    def remember(name, default, source):
        row = found.setdefault(name, {'default': default, 'sources': set()})
        row['sources'].add(source)
        if row['default'] in (None, '') and default not in (None, ''):
            row['default'] = default

    for path in sorted(root.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            if name in READERS:
                key = _literal(node.args[0])
                default = _literal(node.args[1]) if len(node.args) > 1 else None
            elif name in ('getenv', 'get') or isinstance(func, ast.Attribute) and func.attr == 'environ':
                key = _literal(node.args[0])
                default = _literal(node.args[1]) if len(node.args) > 1 else None
                if not isinstance(key, str) or not key.isupper():
                    continue
            else:
                continue
            if isinstance(key, str) and key.isupper() and len(key) > 2:
                remember(key, default, path.name)
    return found


def _group(name: str) -> str:
    head = name.split('_')[0]
    return head if head in GROUP_TITLES else 'ПРОЧЕЕ'


def render(found: dict[str, dict]) -> str:
    lines = [
        '# Переменные окружения',
        '',
        '<!-- Файл собран из кода: python tools/env_reference.py. Руками не править. -->',
        '',
        f'Бот читает {len(found)} переменных. Почти все — точная настройка со значением',
        'по умолчанию: задавать их нужно, только если дефолт не подошёл.',
        '',
        '## Без чего бот не работает как задумано',
        '',
    ]
    for name, why in REQUIRED.items():
        mark = '' if name in found else ' (в коде не найдена — проверьте справочник)'
        lines.append(f'- `{name}` — {why}{mark}')
    lines += ['', '## Все переменные', '']

    groups: dict[str, list[str]] = {}
    for name in sorted(found):
        groups.setdefault(_group(name), []).append(name)
    for group in sorted(groups, key=lambda g: (g == 'ПРОЧЕЕ', g)):
        lines.append(f'### {GROUP_TITLES.get(group, "Прочее")}')
        lines.append('')
        lines.append('| Переменная | По умолчанию | Где читается |')
        lines.append('| --- | --- | --- |')
        for name in groups[group]:
            row = found[name]
            default = row['default']
            shown = '—' if default in (None, '') else f'`{default}`'
            where = ', '.join(sorted(row['sources']))
            lines.append(f'| `{name}` | {shown} | {where} |')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def main() -> int:
    found = collect()
    text = render(found)
    if '--check' in sys.argv:
        current = REFERENCE.read_text(encoding='utf-8') if REFERENCE.exists() else ''
        if current != text:
            print('docs/env-reference.md отстал от кода: '
                  'python tools/env_reference.py')
            return 1
        print(f'справочник совпадает с кодом ({len(found)} переменных)')
        return 0
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(text, encoding='utf-8')
    print(f'docs/env-reference.md пересобран: {len(found)} переменных')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
