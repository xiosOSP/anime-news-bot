"""Повторы новостей: одно ли это событие, и какое.

Чистая текстовая логика без состояния: токены заголовка, номера и порядковые
числительные, тип события и носитель (аниме или манга), ядро названия и
сходство двух заголовков. Хранилища, настройки из окружения и кластеризация
пачки остаются в боте — здесь только то, что можно проверить на двух
строках, без сети, диска и Telegram.

Вынесено из anime_news_bot.py первым: у этого кода почти нет подмен в тестах
(две против 455 у слоя моделей), поэтому перенос не меняет того, что тесты
на самом деле проверяют.
"""
import difflib
from functools import lru_cache
import re
from typing import Optional


@lru_cache(maxsize=8192)
def normalize_title(title: str) -> str:
    """Нормализует заголовок для сравнения: убираем регистр, пробелы, пунктуацию.

    Кеш здесь не украшение: один и тот же заголовок нормализуется десятки раз за
    цикл (дедуп ledger, clustering, story registry), а regex по строке — самая
    заметная часть этой работы. Заголовки короткие, потолок кеша ограничен.
    """
    if not title:
        return ''
    return re.sub(r'[^\w]+', '', title, flags=re.UNICODE).lower()


def _is_generic_anchor(word: str) -> bool:
    """Служебное ли это слово вроде «сезон», «трейлер», «аниме».

    Проверка по началу слова: в русском одно и то же слово приходит в разных
    падежах — «сезон», «сезона», «сезону». Точное сравнение их не связывало, и
    падежные формы засоряли счёт общих якорей, мешая опознать один сюжет.
    """
    low = str(word or '').lower().replace('ё', 'е')
    if low in _STORY_UPDATE_GENERIC:
        return True
    return any(low.startswith(base) and len(low) - len(base) <= 3
               for base in _STORY_UPDATE_GENERIC if len(base) >= 4)


def _story_anchor_stem(word: str) -> str:
    """Грубая основа английского слова: confirms = confirmed, weeks = week.

    Ядра сравниваются на точное равенство — намеренно, чтобы Solo Leveling и
    Solo Leveling Ragnarok не слились. Но тогда одна буква разводила один
    сюжет на два: «Manga Ends» и «Manga to End», «Takes Three-Week Break» и
    «Goes on Break for Three Weeks». Срезаются только латинские окончания, так
    что кириллица не меняется сама собой: русская морфология так не лечится.
    """
    w = str(word or '')
    if len(w) <= 3:
        return w
    if w.endswith('ies') and len(w) > 4:
        return w[:-3] + 'y'
    if w.endswith('ed') and len(w) > 5:
        return w[:-2]
    if w.endswith('s') and not w.endswith('ss'):
        return w[:-1]
    return w


# Глаголы-связки английских заголовков. Суть новости в них нет: «Goes on
# Break» и «Takes a Break» — один перерыв. Хранятся уже в виде основ.
_STORY_IDENTITY_FILLER = {
    _story_anchor_stem(w) for w in (
        'go', 'goes', 'take', 'takes', 'get', 'gets', 'set', 'confirm', 'confirms',
        'confirmed', 'announce', 'announces', 'announced', 'reveal', 'reveals', 'revealed',
        'this', 'until', 'will', 'its', 'now', 'officially',
    )
}


def _story_identity_anchors(value) -> set[str]:
    """Консервативное ядро названия для delivery-дедупа."""
    title = value.get('title', '') if isinstance(value, dict) else str(value or '')

    # В скобках источники часто добавляют альтернативное название
    # ("Голубая шкатулка (Ao no Hako)"). Однословные и событийные уточнения
    # сохраняем: (Remake) и (Final Trailer) могут быть самостоятельной новостью.
    def strip_alias(match: re.Match) -> str:
        inner = match.group(0)[1:-1]
        words = re.findall(r'[A-Za-zА-Яа-яЁё]+', inner.casefold())
        distinguishing = {
            'remake', 'reboot', 'spinoff', 'final',
            'ремейк', 'ребут', 'спинофф', 'финальный', 'финальная',
        }
        if (len(words) >= 2 and not _story_event_markers(inner)
                and not (set(words) & distinguishing)):
            return ' '
        return match.group(0)

    title = re.sub(r'\([^()]{1,80}\)', strip_alias, title)
    anchors = set()
    for anchor in _story_update_anchor({'title': title}):
        if (_is_generic_anchor(anchor) or _ordinal_word_value(anchor) is not None
                or anchor in _STORY_IDENTITY_NOISE):
            continue
        # Слово-событие — не часть названия: тип события сравнивается
        # маркерами отдельно, а в ядре «delayed» против «postponed» разводили
        # один перенос на две новости.
        if _STORY_MARKER_CANON.get(anchor, anchor) in _STORY_EVENT_MARKERS:
            continue
        stem = _story_anchor_stem(anchor)
        if stem in _STORY_IDENTITY_FILLER or stem in _STORY_EVENT_MARKERS:
            continue
        anchors.add(stem)
    return anchors


def _anchor_identity_match(news: dict, old_title: str,
                           new_markers: set, old_markers: set,
                           new_numbers: set, old_numbers: set) -> bool:
    """Один ли это сюжет, если предмет новости от модели недоступен."""
    if not new_markers or new_markers != old_markers:
        return False
    if new_numbers != old_numbers:
        return False
    new_anchor = _story_identity_anchors(news)
    old_anchor = _story_identity_anchors(old_title)
    if len(new_anchor) < 2 or len(old_anchor) < 2:
        return False
    # Только равные смысловые ядра. Сравнение по меньшему множеству считало
    # Solo Leveling и Solo Leveling Ragnarok одним сюжетом, а обычный и
    # финальный трейлер — одним событием. Лишний дубль безопаснее тихой потери
    # самостоятельной новости.
    return new_anchor == old_anchor


_STORY_STOPWORDS = {
    'anime', 'manga', 'news', 'reveals', 'revealed', 'announces', 'announced', 'gets',
    'new', 'the', 'and', 'for', 'with', 'from', 'official', 'visual', 'video', 'trailer',
    'аниме', 'манга', 'новый', 'новая', 'новое', 'анонс', 'анонсирован', 'показали',
    'представили', 'вышел', 'вышла', 'трейлер', 'тизер', 'постер', 'опубликован',
}


_STORY_EVENT_MARKERS = {
    'trailer', 'teaser', 'visual', 'poster', 'cast', 'staff', 'release', 'premiere',
    'delay', 'delayed', 'canceled', 'cancelled', 'episode', 'season', 'movie', 'film',
    'game', 'manga', 'novel', 'adaptation', 'streaming',
    'трейлер', 'тизер', 'постер', 'каст', 'состав', 'релиз', 'премьера', 'перенос',
    'отложен', 'отменен', 'отменён', 'эпизод', 'сезон', 'фильм', 'игра', 'манга',
    'новелла', 'экранизация',
}


# Одно событие разными словами. «Delayed» было маркером переноса, а
# «postponed» — нет, «premiere» было, а «premieres» — нет: у двух заголовков об
# одном переносе типы событий не совпадали, и повтор уходил в канал. Film и
# movie — тоже одно и то же.
_STORY_MARKER_CANON = {
    'delayed': 'delay', 'delays': 'delay', 'postponed': 'delay', 'postpone': 'delay',
    'postpones': 'delay', 'cancelled': 'canceled', 'cancels': 'canceled', 'cancel': 'canceled',
    'premieres': 'premiere', 'premiered': 'premiere', 'trailers': 'trailer',
    'teasers': 'teaser', 'visuals': 'visual', 'posters': 'poster', 'seasons': 'season',
    'episodes': 'episode', 'movies': 'movie', 'film': 'movie', 'films': 'movie',
    'games': 'game', 'novels': 'novel', 'adaptations': 'adaptation',
    'отложен': 'перенос', 'отложена': 'перенос', 'перенесли': 'перенос',
    'отменён': 'отменен', 'отменили': 'отменен',
}


def _story_canonical_markers(markers) -> set[str]:
    """Маркеры к одному написанию — в том числе сохранённые до этой правки."""
    return {_STORY_MARKER_CANON.get(str(m), str(m)) for m in markers} & _STORY_EVENT_MARKERS


def _story_event_markers(news_or_title) -> set[str]:
    title = (news_or_title.get('title', '')
             if isinstance(news_or_title, dict) else str(news_or_title or ''))
    words = set(re.findall(r'[A-Za-zА-Яа-яЁё]+', title.casefold()))
    return _story_canonical_markers(words)


# Носитель новости. «Аниме» и «манга» выброшены из токенов как стоп-слова —
# иначе «X Anime Gets Season 2» и «X Season 2» не совпадали бы. Цена этого:
# «перерыв у манги One Piece» и «перерыв у аниме One Piece» получали сходство
# 1.00, и вторая, самостоятельная новость тихо терялась.
_STORY_MEDIUM_RE = (
    ('anime', re.compile(r'(?<![a-zа-яё])(?:anime|аниме)(?![a-zа-яё])', re.IGNORECASE)),
    ('manga', re.compile(r'(?<![a-zа-яё])(?:manga|манг[аиуеойю]\w*)(?![a-zа-яё])', re.IGNORECASE)),
)


def _story_media(news_or_title) -> set[str]:
    title = (news_or_title.get('title', '')
             if isinstance(news_or_title, dict) else str(news_or_title or ''))
    return {name for name, pattern in _STORY_MEDIUM_RE if pattern.search(title)}


def _story_events_conflict(a, b) -> bool:
    """Заведомо разные события, как бы похоже ни звучали заголовки.

    Тип события названы оба, и он разный: трейлер и ключевой визуал одного
    сезона — две новости. Носитель назван у обоих и разный: аниме и манга.
    Если у одного заголовка тип не назван, конфликта нет — «X Season 2» и
    «X Season 2 Trailer» решает сходство.
    """
    ma, mb = _story_event_markers(a), _story_event_markers(b)
    if ma and mb and ma != mb:
        return True
    da, db = _story_media(a), _story_media(b)
    return bool(da and db and not (da & db))


def _story_title_of(news_or_title) -> str:
    """Заголовок из news-словаря либо готовая строка — единая точка входа."""
    if isinstance(news_or_title, dict):
        return str(news_or_title.get('title', '') or '')
    return str(news_or_title or '')


@lru_cache(maxsize=4096)
def _story_tokens_cached(title: str) -> frozenset:
    tokens = re.findall(r'[A-Za-zА-Яа-яЁё0-9]+', title.lower())
    return frozenset(t for t in tokens if len(t) >= 3 and t not in _STORY_STOPWORDS)


def _story_tokens(news_or_title) -> set[str]:
    # Clustering сравнивает каждого кандидата с сотней представителей кластеров,
    # и без кеша токены одного и того же заголовка пересчитывались сотни раз.
    return set(_story_tokens_cached(_story_title_of(news_or_title)))


# Русские источники пишут «второй сезон», английские и часть телеграм-каналов —
# «2 сезон». Без этой таблицы одна и та же новость выглядела для дедупа разной:
# у одной числа пустые, у другой — {'2'}, и схожесть падала ниже порога склейки.
_RU_ORDINAL_STEMS = {
    'перв': '1', 'втор': '2', 'трет': '3', 'четверт': '4', 'пят': '5',
    'шест': '6', 'седьм': '7', 'восьм': '8', 'девят': '9', 'десят': '10',
}


_EN_ORDINAL_WORDS = {
    'first': '1', 'second': '2', 'third': '3', 'fourth': '4', 'fifth': '5',
    'sixth': '6', 'seventh': '7', 'eighth': '8', 'ninth': '9', 'tenth': '10',
}


_RU_ORDINAL_SUFFIXES = {
    'ый', 'ий', 'ой', 'ая', 'яя', 'ое', 'ее', 'ые', 'ие',
    'ого', 'его', 'ей', 'ому', 'ему', 'ым', 'им', 'ом', 'ем',
    'ую', 'юю', 'ых', 'их', 'ыми', 'ими',
    'ья', 'ье', 'ьи', 'ьего', 'ьей', 'ьему', 'ьим', 'ьем', 'ью', 'ьих', 'ьими',
}


_ORDINAL_RE = re.compile(r'[a-zA-Zа-яёА-ЯЁ]+')


def _ordinal_word_value(word: str) -> Optional[str]:
    """Порядковое числительное целым словом, без совпадений вроде «пятно»."""
    low = str(word or '').lower().replace('ё', 'е')
    if low in _EN_ORDINAL_WORDS:
        return _EN_ORDINAL_WORDS[low]
    for stem, value in _RU_ORDINAL_STEMS.items():
        if low.startswith(stem) and low[len(stem):] in _RU_ORDINAL_SUFFIXES:
            return value
    return None


def _ordinal_numbers(title: str) -> set[str]:
    """Числа, записанные словом: «второго сезона» -> {'2'}."""
    out: set[str] = set()
    for word in _ORDINAL_RE.findall(title or ''):
        value = _ordinal_word_value(word)
        if value is not None:
            out.add(value)
    return out


@lru_cache(maxsize=4096)
def _story_numbers_cached(title: str) -> frozenset:
    return frozenset(re.findall(r'(?<!\w)\d{1,4}(?!\w)', title)) | frozenset(_ordinal_numbers(title))


def _story_numbers(news_or_title) -> set[str]:
    return set(_story_numbers_cached(_story_title_of(news_or_title)))


_STORY_UPDATE_GENERIC = {
    'anime', 'аниме', 'manga', 'манга', 'trailer', 'трейлер', 'visual', 'постер',
    'release', 'released', 'релиз', 'premiere', 'премьера', 'date', 'дата', 'new', 'новый',
    'новая', 'reveals', 'revealed', 'announces', 'announced', 'анонс', 'season', 'сезон',
    'project', 'проект', 'gets', 'получил', 'получила', 'официальный', 'official',
}


# Глаголы оформления заголовка не являются частью названия франшизы. Держим
# список локальным для fallback identity, чтобы не менять общий clustering.
_STORY_IDENTITY_NOISE = {
    'выдали', 'показан', 'показана', 'показали', 'представлен', 'представлена',
    'представили', 'опубликовали', 'опубликован', 'опубликована', 'вышел', 'вышла',
    'released', 'revealed', 'unveiled', 'published', 'out',
}


def _story_update_anchor(news_or_title) -> set[str]:
    """Stable franchise-ish tokens; intentionally ignores event words."""
    return {t for t in _story_tokens(news_or_title) if t not in _STORY_UPDATE_GENERIC}


def _story_similarity(a: dict, b: dict) -> float:
    """Консервативная близость двух заголовков для cross-source clustering."""
    title_a, title_b = _story_title_of(a), _story_title_of(b)
    ta, tb = _story_tokens_cached(title_a), _story_tokens_cached(title_b)
    if not ta or not tb:
        return 0.0
    nums_a, nums_b = _story_numbers_cached(title_a), _story_numbers_cached(title_b)
    # Season 2 и Season 3 нельзя сливать даже при почти одинаковом шаблоне заголовка.
    if nums_a and nums_b and nums_a != nums_b:
        return 0.0
    common = ta & tb
    if len(common) < 2:
        return 0.0
    union = ta | tb
    jaccard = len(common) / max(1, len(union))
    containment = len(common) / max(1, min(len(ta), len(tb)))
    # SequenceMatcher — самая дорогая часть цикла сборки (квадратичен по длине
    # заголовка и вызывается для каждой пары кандидат/кластер). Считаем его
    # только когда он ещё способен изменить ответ: даже при seq == 1.0 итог не
    # превысит jaccard, если 0.55 * containment + 0.45 <= jaccard.
    if 0.55 * containment + 0.45 <= jaccard:
        return jaccard
    seq = difflib.SequenceMatcher(None, normalize_title(title_a),
                                  normalize_title(title_b)).ratio()
    return max(jaccard, 0.55 * containment + 0.45 * seq)
