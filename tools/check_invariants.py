#!/usr/bin/env python3
"""Инварианты, найденные ревью: каждая строка — отдельная исправленная проблема.

Запуск глазами::

    python tools/check_invariants.py

Тот же список подключён к pytest (``test_invariants.py``), чтобы пропажа
механизма ломала сборку сама, а не всплывала на проде через месяц.

Каждая проверка возвращает ``(имя, ok, почему)``. ``ok=False`` означает, что
регрессия вернулась: комментарий рядом объясняет, чем именно она вредна.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ---------- помощники для AST-проверок ----------

def _func(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls(node: ast.AST, dotted: str) -> bool:
    """Есть ли внутри узла вызов вида ``a.b(...)`` / ``name(...)``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            try:
                if ast.unparse(sub.func) == dotted:
                    return True
            except Exception:
                continue
    return False


# ---------- сами инварианты ----------

def _check_truncation_fits_limit(bot) -> tuple[bool, str]:
    """Многоточие — тоже символ.

    ``text[:limit] + '…'`` даёт limit + 1, и подпись, посчитанная впритык под
    лимит Telegram, отвергается Bot API уже после всей дорогой подготовки поста.
    """
    for limit in range(2, 60):
        for text in ('a' * 200, 'слово ' * 40, 'one two three four five six'):
            out = bot.smart_truncate(text, limit)
            if len(out) > limit:
                return False, f'smart_truncate({limit}) вернул {len(out)} символов'
            out = bot.fit_to_limit(text, limit)
            if len(out) > limit:
                return False, f'fit_to_limit({limit}) вернул {len(out)} символов'
    return True, 'обрезка укладывается в лимит вместе с многоточием'


def _check_health_metric_cardinality(bot, tree) -> tuple[bool, str]:
    """Health-порт может смотреть наружу, а путь запроса выбирает клиент.

    Метка Prometheus из сырого пути означала бы вечный счётчик на каждый URL,
    который придумает сканер: registry живёт всю жизнь процесса и не вытесняет.
    """
    hostile = ['/' + 'x' * 40, '/wp-admin', '/../../etc/passwd', '/%00', '/a?b=c']
    labels = {bot._health_probe_label(p) for p in hostile}
    if labels != {'other'}:
        return False, f'незнакомые пути дали метки {sorted(labels)}'
    if bot._health_probe_label('/healthz') != '/healthz':
        return False, 'известный путь потерял собственную метку'
    if bot._health_probe_label('/HEALTHZ/') != '/healthz':
        return False, 'регистр/слеш не нормализуются — метка раздваивается'
    probe = _func(tree, '_note_health_probe')
    if probe is None:
        return False, '_note_health_probe пропал'
    if not _calls(probe, '_health_probe_label'):
        return False, 'сырой путь снова попадает в метку метрики напрямую'
    return True, 'метка пути ограничена известным списком'


def _check_worker_skip_not_charged_to_source(bot, tree) -> tuple[bool, str]:
    """Зависший поток прошлого цикла — наша вина, а не источника.

    Раньше пропуск возбуждал ``TimeoutError``, тот попадал в общий ``except``
    и начислял источнику hard-ошибку: circuit breaker открывался, счётчик
    тишины рос, и совершенно здоровый сайт в итоге выключался насовсем.
    """
    if not issubclass(bot.SourceWorkerBusy, bot.SourceWorkerUnavailable):
        return False, 'SourceWorkerBusy больше не подкласс SourceWorkerUnavailable'
    collect = _func(tree, 'collect_all_news')
    if collect is None:
        return False, 'collect_all_news не найден'
    for handler in (n for n in ast.walk(collect) if isinstance(n, ast.ExceptHandler)):
        if handler.type is not None and ast.unparse(handler.type) == 'SourceWorkerUnavailable':
            if _calls(handler, '_note_source_failure'):
                return False, 'пропуск воркера снова начисляет источнику ошибку'
            return True, 'пропуск по вине пула не штрафует источник'
    return False, 'collect_all_news не обрабатывает SourceWorkerUnavailable отдельно'


def _check_empty_body_is_not_a_failure(bot) -> tuple[bool, str]:
    """Пустое тело ответа — это ``b''``, а не сбой чтения.

    Обращение к ``response.content`` после ``iter_content`` бросает RuntimeError:
    корректный пустой ответ превращался в None, то есть в «не смог прочитать».
    """
    class _Empty:
        headers: dict = {}

        def iter_content(self, chunk_size=1):
            return iter(())

        @property
        def content(self):
            raise RuntimeError('The content for this response was already consumed')

    got = bot._read_limited_response(_Empty(), 1024)
    if got != b'':
        return False, f'пустое тело прочиталось как {got!r}'

    class _Big(_Empty):
        def iter_content(self, chunk_size=1):
            return iter([b'x' * 100, b'y' * 100])

    if bot._read_limited_response(_Big(), 50) is not None:
        return False, 'превышение лимита тела не отклонено'
    return True, 'пустое тело и превышение лимита различаются'


def _check_season_numbers_never_merge(bot) -> tuple[bool, str]:
    """Season 2 и Season 3 — разные новости при почти одинаковом шаблоне."""
    a = {'title': 'One Piece Season 2 announced by Toei'}
    b = {'title': 'One Piece Season 3 announced by Toei'}
    if bot._story_similarity(a, b) != 0.0:
        return False, 'разные номера сезонов склеиваются в одну story'
    same = {'title': 'One Piece Season 2 announced by Toei Animation'}
    if bot._story_similarity(a, same) < bot.STORY_CLUSTER_SIMILARITY:
        return False, 'одинаковый сезон перестал распознаваться как та же story'
    return True, 'номера сезонов различают истории'


def _check_similarity_is_symmetric(bot) -> tuple[bool, str]:
    """Близость не должна зависеть от порядка аргументов.

    Кеши и ранний выход из SequenceMatcher — оптимизация, а не смена ответа.
    """
    titles = [
        'Bleach Thousand-Year Blood War Part 4 opening revealed',
        'Bleach TYBW Part 4 opening theme revealed by jo0ji',
        'Jujutsu Kaisen movie gets new visual',
        'Второй сезон аниме получил трейлер',
    ]
    for i, first in enumerate(titles):
        for second in titles[i:]:
            a, b = {'title': first}, {'title': second}
            if abs(bot._story_similarity(a, b) - bot._story_similarity(b, a)) > 1e-12:
                return False, f'несимметрично на {first!r} / {second!r}'
    return True, 'близость симметрична'


def _check_ssrf_guard(bot) -> tuple[bool, str]:
    """Пользовательский RSS не должен ходить внутрь сети хостинга."""
    blocked = [
        'http://localhost/x', 'http://127.0.0.1/x', 'http://169.254.169.254/latest/meta-data',
        'http://10.0.0.1/', 'http://[::1]/', 'file:///etc/passwd', 'http://user:pw@example.com/',
    ]
    for url in blocked:
        if bot._is_public_http_url(url):
            return False, f'{url} признан безопасным'
    if not bot._is_public_http_url('https://example.com/feed'):
        return False, 'обычный публичный URL заблокирован'
    return True, 'приватные адреса и metadata-endpoint закрыты'


def _check_atomic_json_write(tree) -> tuple[bool, str]:
    """Kill посреди ``write`` не должен оставлять обрезанный runtime-JSON."""
    node = _func(tree, '_atomic_write_json')
    if node is None:
        return False, '_atomic_write_json пропал'
    if not _calls(node, 'os.replace'):
        return False, 'запись больше не атомарна (нет os.replace)'
    if not _calls(node, 'os.fsync'):
        return False, 'нет fsync — данные могут не дойти до диска'
    return True, 'JSON пишется через временный файл + os.replace'


def _check_no_mutable_defaults(tree) -> tuple[bool, str]:
    """Список/словарь в значении по умолчанию живёт между вызовами."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
        for default in defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                bad.append(f'{node.name}:{node.lineno}')
    return (not bad), (f'мутабельные значения по умолчанию: {bad[:5]}' if bad
                       else 'мутабельных значений по умолчанию нет')


def _check_no_bare_except(tree) -> tuple[bool, str]:
    """``except:`` глотает KeyboardInterrupt и SystemExit вместе с ошибками."""
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.ExceptHandler) and n.type is None]
    return (not bad), (f'голый except в строках {bad[:5]}' if bad else 'голого except нет')


def _check_every_request_has_timeout(tree) -> tuple[bool, str]:
    """HTTP без таймаута вешает цикл сборки навсегда."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        try:
            target = ast.unparse(node.func)
        except Exception:
            continue
        if target in ('requests.get', 'requests.post', 'requests.head', 'requests.request'):
            if not any(kw.arg == 'timeout' for kw in node.keywords):
                bad.append(node.lineno)
    return (not bad), (f'requests без timeout в строках {bad[:5]}' if bad
                       else 'у всех requests-вызовов есть timeout')


def _check_text_files_have_encoding(tree) -> tuple[bool, str]:
    """``open()`` без encoding читает по локали хоста и ломает кириллицу."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        try:
            target = ast.unparse(node.func)
        except Exception:
            continue
        receiver = target[:-len('.open')] if target.endswith('.open') else ''
        # Image.open / zipfile.ZipFile.open принимают не путь, а поток или имя
        # внутри архива: параметра encoding у них нет и быть не должно.
        if receiver.split('.')[-1] in ('Image', 'zipfile', 'ZipFile', 'tarfile', 'gzip', 'io'):
            continue
        if not (target == 'open' or target.endswith('.open')):
            continue
        mode = ''
        if target == 'open' and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        if target.endswith('.open') and node.args and isinstance(node.args[0], ast.Constant):
            mode = str(node.args[0].value)
        for kw in node.keywords:
            if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if 'b' in mode:
            continue
        if not any(kw.arg == 'encoding' for kw in node.keywords):
            bad.append(node.lineno)
    return (not bad), (f'open без encoding в строках {bad[:5]}' if bad
                       else 'текстовые файлы открываются с явной кодировкой')


def _check_bounded_cache_evicts_oldest(bot) -> tuple[bool, str]:
    """Полный ``clear()`` при переполнении устраивал cache stampede.

    201-я статья мгновенно стирала все 200 и заставляла бот перекачивать их.
    """
    cache: dict = {}
    for i in range(10):
        bot._bounded_cache_put(cache, i, i, 4)
    if len(cache) != 4:
        return False, f'кеш вырос до {len(cache)} при потолке 4'
    if sorted(cache) != [6, 7, 8, 9]:
        return False, f'вытеснены не самые старые ключи: {sorted(cache)}'
    return True, 'кеш вытесняет по одному, а не очищается целиком'


def _check_normalize_url_never_invents_host(bot) -> tuple[bool, str]:
    """Относительная ссылка не должна превращаться в ``https:///path``.

    Испорченный ключ дедупа — это либо потерянная новость, либо дубль в канале.
    """
    for raw in ('/relative/path', 'mailto:a@b.c', 'tg://resolve?domain=x', '', '   '):
        out = bot.normalize_url(raw)
        # Схему https здесь приписывать не из чего: хоста в этих строках нет.
        if out.startswith('https:'):
            return False, f'{raw!r} превратился в {out!r}'
    if bot.normalize_url('https://WWW.Example.com/a/?utm_source=x#f') != 'https://example.com/a':
        return False, 'канонизация обычного URL изменилась'
    if bot.normalize_url('http://example.com/a') != bot.normalize_url('https://example.com/a'):
        return False, 'http и https перестали считаться одной статьёй'
    return True, 'нормализация URL не выдумывает хост'


def _check_retry_delay_is_bounded(bot) -> tuple[bool, str]:
    """Один ответ сервера не должен замораживать worker дольше потолка."""
    for attempt in range(6):
        for retry_after in (None, 0, 1, 10 ** 9, float('inf')):
            delay = bot._adaptive_retry_delay(attempt, retry_after)
            if not (0.0 <= delay <= bot.HTTP_RETRY_MAX_DELAY):
                return False, f'attempt={attempt} retry_after={retry_after} → {delay}'
    if bot._parse_retry_after('999999') > bot.HTTP_RETRY_MAX_DELAY:
        return False, 'Retry-After не ограничен потолком'
    return True, f'задержка всегда в [0, {bot.HTTP_RETRY_MAX_DELAY}]'


def _check_metric_labels_are_capped(bot) -> tuple[bool, str]:
    """Значение метки не должно тащить в registry килобайт текста."""
    registry = bot.MetricsRegistry()
    registry.inc('probe_total', 1, {'reason': 'x' * 5000})
    rendered = registry.render()
    if len(rendered) > 400:
        return False, f'метка не обрезана, render дал {len(rendered)} символов'
    newline = bot.MetricsRegistry()
    newline.inc('probe_total', 1, {'reason': 'a\nb'})
    body = newline.render()
    if body.count('\n') != 1 or '\\n' not in body:
        return False, 'перевод строки в метке ломает формат Prometheus'
    return True, 'значения меток обрезаны и экранированы'


def _check_dashboard_closed_without_token(bot) -> tuple[bool, str]:
    """Дашборд не открывается, пока для него не задан отдельный токен."""
    if bot._dashboard_authorized({'Authorization': 'Basic YWRtaW46YWRtaW4='}):
        return False, 'дашборд пустил запрос без настроенного токена'
    if bot._dashboard_authorized({}):
        return False, 'дашборд пустил запрос без заголовка'
    return True, 'без DASHBOARD_TOKEN доступ закрыт'


def _check_image_cache_is_capped_in_bytes(bot) -> tuple[bool, str]:
    """Кеш картинок должен ограничиваться объёмом, а не числом записей.

    Сорок записей при потолке загрузки в 9 МБ — это до 360 МБ в памяти.
    Именно так процесс на скромном контейнере получает SIGTERM от платформы
    на ровном месте, хотя «утечки» в обычном смысле нет.
    """
    cache: dict = {}
    budget = bot.IMAGE_BYTES_CACHE_MAX_BYTES
    for i in range(bot.IMAGE_BYTES_CACHE_MAX * 5):
        bot._bounded_bytes_cache_put(cache, f'https://example.com/{i}.jpg',
                                     b'x' * bot.HTTP_IMAGE_MAX_BYTES,
                                     bot.IMAGE_BYTES_CACHE_MAX, budget)
    used = bot._cache_bytes_used(cache)
    if used > budget:
        return False, f'кеш занял {used} байт при бюджете {budget}'
    if len(cache) > bot.IMAGE_BYTES_CACHE_MAX:
        return False, f'записей {len(cache)} при потолке {bot.IMAGE_BYTES_CACHE_MAX}'
    worst = bot.IMAGE_BYTES_CACHE_MAX * bot.HTTP_IMAGE_MAX_BYTES
    if budget >= worst:
        return False, 'бюджет не ограничивает худший случай — потолок бесполезен'
    # Неудачная загрузка должна запоминаться, иначе битую картинку качаем снова.
    fresh: dict = {}
    bot._bounded_bytes_cache_put(fresh, 'fail', None, bot.IMAGE_BYTES_CACHE_MAX, budget)
    if 'fail' not in fresh:
        return False, 'отрицательный результат не кешируется'
    return True, f'кеш ограничен {budget // (1024 * 1024)} МБ вместо {worst // (1024 * 1024)} МБ'


def _check_manifest_describes_reality() -> tuple[bool, str]:
    """Опись не должна числить файлы, которых в репозитории нет.

    Манифест, перечисляющий несуществующее, хуже отсутствующего: он выглядит
    как проверяемая опись, но проверить по нему нельзя ничего. Пересобрать —
    ``python tools/build_manifest.py``.
    """
    manifest_path = ROOT / 'BUILD_MANIFEST.json'
    if not manifest_path.exists():
        return True, 'манифеста нет — вводить в заблуждение нечему'
    try:
        listed = json.loads(manifest_path.read_text(encoding='utf-8')).get('files') or {}
    except (json.JSONDecodeError, OSError) as exc:
        return False, f'манифест не читается: {exc}'
    phantom = sorted(name for name in listed if not (ROOT / name).is_file())
    if phantom:
        return False, f'в манифесте числятся отсутствующие файлы: {phantom[:5]}'
    return True, f'все {len(listed)} записей манифеста существуют'


def _check_ledger_claim_is_exclusive(bot) -> tuple[bool, str]:
    """Двойной claim одного URL — это дубль в канале."""
    async def run() -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            store = bot.SentLinksStore(Path(tmp) / 'sent.json')
            link = 'https://example.com/news/1'
            if not await store.claim(link, 'Some anime news title'):
                return False, 'первый claim не прошёл'
            if await store.claim(link, 'Some anime news title'):
                return False, 'повторный claim того же URL разрешён'
            await store.release(link, 'Some anime news title')
            if not await store.claim(link, 'Some anime news title'):
                return False, 'после release URL остался заблокирован'
            return True, 'claim эксклюзивен, release возвращает URL'

    return asyncio.run(run())


def checks(bot, tree) -> list[tuple[str, bool, str]]:
    """Полный список инвариантов. Порядок стабилен: на него смотрит pytest."""
    rows: list[tuple[str, bool, str]] = []

    def add(name, result):
        ok, why = result
        rows.append((name, bool(ok), str(why)))

    add('обрезка текста укладывается в лимит', _check_truncation_fits_limit(bot))
    add('метки health-метрики ограничены', _check_health_metric_cardinality(bot, tree))
    add('пропуск воркера не штрафует источник',
        _check_worker_skip_not_charged_to_source(bot, tree))
    add('пустое тело ответа — не сбой', _check_empty_body_is_not_a_failure(bot))
    add('номера сезонов не склеиваются', _check_season_numbers_never_merge(bot))
    add('близость историй симметрична', _check_similarity_is_symmetric(bot))
    add('SSRF-защита пользовательских URL', _check_ssrf_guard(bot))
    add('runtime-JSON пишется атомарно', _check_atomic_json_write(tree))
    add('нет мутабельных значений по умолчанию', _check_no_mutable_defaults(tree))
    add('нет голого except', _check_no_bare_except(tree))
    add('у всех HTTP-вызовов есть timeout', _check_every_request_has_timeout(tree))
    add('текстовые файлы с явной кодировкой', _check_text_files_have_encoding(tree))
    add('bounded-кеш вытесняет по одному', _check_bounded_cache_evicts_oldest(bot))
    add('нормализация URL не выдумывает хост', _check_normalize_url_never_invents_host(bot))
    add('retry-задержка ограничена', _check_retry_delay_is_bounded(bot))
    add('метки метрик обрезаны по длине', _check_metric_labels_are_capped(bot))
    add('дашборд закрыт без токена', _check_dashboard_closed_without_token(bot))
    add('claim в ledger эксклюзивен', _check_ledger_claim_is_exclusive(bot))
    add('кеш картинок ограничен по объёму', _check_image_cache_is_capped_in_bytes(bot))
    add('манифест описывает существующие файлы', _check_manifest_describes_reality())
    return rows


def main() -> int:
    sys.path.insert(0, str(ROOT))
    import anime_news_bot as bot

    tree = ast.parse((ROOT / 'anime_news_bot.py').read_text(encoding='utf-8'))
    rows = checks(bot, tree)
    width = max(len(name) for name, _, _ in rows)
    failed = 0
    for name, ok, why in rows:
        if not ok:
            failed += 1
        print(f'{"OK " if ok else "FAIL"}  {name.ljust(width)}  {why}')
    print(f'\n{len(rows) - failed}/{len(rows)} инвариантов держатся')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
