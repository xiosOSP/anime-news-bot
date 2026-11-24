"""Как админы правят посты бота — и недельная сводка о качестве.

Самый точный сигнал о качестве поста — то, что админ в нём поправил руками.
Именно так, вручную листая канал, нашлись значок 🔄, который админы стирали,
названия на ромадзи и лишние строки (ноябрь 2026). Бот этого не видел:
отправив пост, он забывал, что именно отправил. Теперь он помнит текст своих
постов в канале, сравнивает с правкой и раз в неделю пишет владельцу, что
правят чаще всего. Из этих данных потом вырастут правила: повторяющаяся
правка становится предложением, которое админ принимает одной кнопкой.

Модуль без зависимостей от бота: вход — тексты, выход — словари и строки.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import difflib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

POSTS_MAX = 400
EDITS_MAX = 400
# Значок перед заголовком: эмодзи и символы-картинки в начале строки.
_LEADING_EMOJI_RE = re.compile(r'^[\U0001F000-\U0001FAFF☀-➿⬀-⯿]️?\s*')
_HASHTAG_LINE_RE = re.compile(r'^\s*#[\w-]+(?:\s+#[\w-]+)*\s*$')
_WORD_RE = re.compile(r'[0-9A-Za-zА-Яа-яЁё぀-ヿ一-鿿]+(?:[-‐‑][0-9A-Za-zА-Яа-яЁё]+)*')
# Название тайтла в кавычках, где одна латиница: «Kekkaishi no Ichirinka».
_LATIN_QUOTED_RE = re.compile(r'«([^«»]*[A-Za-z][^«»]*)»')

CHANGE_LABELS = {
    'emoji_removed': 'убран значок перед заголовком',
    'emoji_changed': 'заменён значок перед заголовком',
    'headline': 'изменён заголовок',
    'about_removed': 'убрана строка «о чём»',
    'hashtags_removed': 'убраны хэштеги',
    'date_removed': 'убрана строка с датой',
    'line_removed': 'убрана строка текста',
    'line_added': 'дописана строка',
    'body': 'изменён текст',
    'rewritten': 'пост переписан целиком',
}


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or '').splitlines() if line.strip()]


def _split_emoji(head: str) -> tuple[str, str]:
    match = _LEADING_EMOJI_RE.match(head)
    if not match:
        return '', head
    return match.group(0).strip(), head[match.end():].strip()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(str(text or ''))


def _replacements(before: str, after: str, limit: int = 3) -> list[list[str]]:
    """Короткие замены слов: [было, стало] — до трёх слов с каждой стороны."""
    a, b = _words(before), _words(after)
    out = []
    matcher = difflib.SequenceMatcher(None, [w.casefold() for w in a], [w.casefold() for w in b],
                                      autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'replace' and 0 < i2 - i1 <= limit and 0 < j2 - j1 <= limit:
            out.append([' '.join(a[i1:i2]), ' '.join(b[j1:j2])])
    return out[:8]


def classify_edit(before: str, after: str, *, about: str = '') -> dict:
    """Что админ поменял в посте бота. Пустой список changes — ничего по сути.

    about — строка «о чём», которую бот поставил цитатой: её удаление — свой
    вид правки, по нему видно, что цитата там была лишней.
    """
    old, new = _lines(before), _lines(after)
    if old == new:
        return {'changes': [], 'pairs': [], 'ratio': 1.0}
    changes: list[str] = []
    old_head = old[0] if old else ''
    new_head = new[0] if new else ''
    old_emoji, old_title = _split_emoji(old_head)
    new_emoji, new_title = _split_emoji(new_head)
    if old_emoji and not new_emoji:
        changes.append('emoji_removed')
    elif old_emoji and new_emoji and old_emoji != new_emoji:
        changes.append('emoji_changed')
    if old_title != new_title:
        changes.append('headline')
    old_body, new_body = old[1:], new[1:]
    removed = [line for line in old_body if line not in new_body]
    added = [line for line in new_body if line not in old_body]
    about = str(about or '').strip()
    for line in removed:
        if about and line == about:
            kind = 'about_removed'
        elif _HASHTAG_LINE_RE.match(line):
            kind = 'hashtags_removed'
        elif line.startswith('📅'):
            kind = 'date_removed'
        else:
            kind = None
        if kind and kind not in changes:
            changes.append(kind)
    plain_removed = [line for line in removed if not (
        (about and line == about) or _HASHTAG_LINE_RE.match(line) or line.startswith('📅'))]
    # Строка заменена другой — это правка текста, а не удаление и вставка.
    if plain_removed and added:
        changes.append('body')
    elif plain_removed:
        changes.append('line_removed')
    elif added:
        changes.append('line_added')
    ratio = difflib.SequenceMatcher(None, '\n'.join(old), '\n'.join(new), autojunk=False).ratio()
    if ratio < 0.4:
        changes = ['rewritten'] + [c for c in changes if c.startswith('emoji')]
    pairs = _replacements(old_title, new_title) + _replacements(
        '\n'.join(old_body), '\n'.join(new_body))
    return {'changes': changes, 'pairs': pairs[:8], 'ratio': round(ratio, 3),
            'head_before': old_head[:200], 'head_after': new_head[:200]}


def latin_title_in_headline(head: str) -> bool:
    """В заголовке название латиницей без русского: «Kekkaishi no Ichirinka».

    «Рубеж Шангри-Ла» (Shangri-La Frontier) — так и задумано: русское название,
    оригинал в скобках. Считаем только кавычки, где нет ни одной кириллической
    буквы и есть хотя бы два латинских слова: «MAPPA» или «Netflix» — не тайтл.
    """
    for match in _LATIN_QUOTED_RE.finditer(str(head or '')):
        inner = match.group(1)
        if not re.search(r'[А-Яа-яЁё]', inner) and len(re.findall(r'[A-Za-z]{2,}', inner)) >= 2:
            return True
    return False


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ChannelPostLog:
    """Посты бота в канале (текст, как его вернул Telegram) и правки админов.

    Хранит последние POSTS_MAX постов и EDITS_MAX правок; на одну запись —
    одна правка: повторная правка того же поста заменяет прежнюю, а сравнение
    всегда идёт с тем, что отправил бот.
    """

    def __init__(self, path, *, writer=None):
        self.path = Path(path)
        self._write = writer or _atomic_write
        self._lock = threading.Lock()
        self._data = {'schema_version': 1, 'posts': {}, 'edits': {}, 'digest_week': ''}
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                for key in ('posts', 'edits'):
                    if isinstance(raw.get(key), dict):
                        self._data[key] = {str(k): v for k, v in raw[key].items() if isinstance(v, dict)}
                self._data['digest_week'] = str(raw.get('digest_week') or '')
        except (OSError, ValueError):
            pass            # нет файла или он битый — начинаем с чистого листа: это справка, не история

    def _save_locked(self) -> bool:
        for key, limit in (('posts', POSTS_MAX), ('edits', EDITS_MAX)):
            rows = self._data[key]
            if len(rows) > limit:
                keep = sorted(rows.items(), key=lambda kv: float(kv[1].get('at') or 0))[-limit:]
                self._data[key] = dict(keep)
        try:
            self._write(self.path, self._data)
            return True
        except OSError:
            return False

    def record_post(self, message_id: int, text: str, meta: dict = None) -> bool:
        if not isinstance(message_id, int) or isinstance(message_id, bool) or not str(text or '').strip():
            return False
        row = {'at': time.time(), 'text': str(text)[:4096]}
        for key, value in (meta or {}).items():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                row[str(key)] = value if not isinstance(value, str) else value[:300]
        with self._lock:
            self._data['posts'][str(message_id)] = row
            return self._save_locked()

    def record_edit(self, message_id: int, text: str):
        """Правка поста бота: событие с разбором или None (не наш пост / без перемен)."""
        with self._lock:
            post = self._data['posts'].get(str(message_id))
            if not post:
                return None
            diff = classify_edit(post.get('text', ''), text, about=str(post.get('about') or ''))
            if not diff['changes']:
                self._data['edits'].pop(str(message_id), None)
                self._save_locked()
                return None
            event = dict(diff, at=time.time(), posted_at=post.get('at'),
                         topic=post.get('topic', ''), source=post.get('source', ''))
            self._data['edits'][str(message_id)] = event
            self._save_locked()
            return dict(event)

    def window(self, days: float = 7, now: float = None) -> tuple[list[dict], list[dict]]:
        edge = (now or time.time()) - days * 86400
        with self._lock:
            posts = [dict(v) for v in self._data['posts'].values() if float(v.get('at') or 0) >= edge]
            edits = [dict(v) for v in self._data['edits'].values()
                     if float(v.get('posted_at') or v.get('at') or 0) >= edge]
        return posts, edits

    def digest_week(self) -> str:
        with self._lock:
            return self._data['digest_week']

    def mark_digest_week(self, week: str) -> bool:
        with self._lock:
            self._data['digest_week'] = str(week)
            return self._save_locked()


def week_key(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f'{year}-W{week:02d}'


def weekly_digest(posts: list[dict], edits: list[dict], tech_lines=(), *, now: datetime = None,
                  days: int = 7) -> str:
    """Текст недельной сводки для владельца. Простой текст, без разметки."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    lines = [f'📈 Неделя бота: {start:%d.%m} — {now:%d.%m}', '']
    total = len(posts)
    lines.append(f'Постов бота в канале: {total}')
    if total:
        share = round(100 * len(edits) / total)
        lines.append(f'Правили админы: {len(edits)} ({share}%)')
    kinds = Counter(change for edit in edits for change in edit.get('changes') or ())
    for kind, count in kinds.most_common(6):
        lines.append(f'  • {CHANGE_LABELS.get(kind, kind)} — {count}')
    pairs = Counter((a.casefold(), b.casefold()) for edit in edits for a, b in edit.get('pairs') or ())
    frequent = [(pair, n) for pair, n in pairs.most_common(5) if n >= 2]
    if frequent:
        lines.append('Частые замены: ' + '; '.join(f'«{a}» → «{b}» ({n})' for (a, b), n in frequent))
    latin = [post for post in posts if post.get('topic') in ('аниме', 'манга')
             and latin_title_in_headline((_lines(post.get('text', '')) or [''])[0])]
    if latin:
        examples = '; '.join(_split_emoji(_lines(p.get('text', ''))[0])[1][:60] for p in latin[:3])
        lines.append(f'Название латиницей в заголовке: {len(latin)} — {examples}')
    tech = [str(line).strip() for line in tech_lines if str(line).strip()]
    if tech:
        lines += ['', 'Техника:'] + [f'  {line}' for line in tech]
    if not total:
        lines += ['', 'Данных пока нет: бот запоминает свои посты с этой версии.']
    return '\n'.join(lines)
