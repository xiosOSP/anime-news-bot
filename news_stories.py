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


# ============== ТАЙТЛ НОВОСТИ НА ЛЮБОМ ЯЗЫКЕ ==============
# Одну новость пишут на разных языках и разными словами: «Постер к 4-му сезону
# аниме "Необъятный океан"», «Grand Blue Season 4 Announced», «Annunciata la
# quarta stagione di Grand Blue Dreaming». Общих слов у заголовков нет, и
# сходство строк их не склеит. Склеивает тайтл: имена-кандидаты отсюда бот
# сверяет с базой Shikimori, где у тайтла есть русское, английское и японское
# название. Здесь — только разбор текста, без сети.

_WORK_ANCHOR = (r'(?:аниме(?:-сериал\w*|-фильм\w*)?|мульт\w*|манг[аиуеой]|ранобэ|новелл[аыеуой]|'
                r'сериал[аеу]?|фильм[аеу]?|экранизаци[июя]|тайтл[аеу]?)')
# Имя в кавычках сразу после слова «аниме», «манги»…: самое надёжное место.
_WORK_QUOTED_AFTER_ANCHOR = re.compile(
    rf'{_WORK_ANCHOR}\s+(?:по\s+\w+\s+)?[«"“]([^»"”]{{2,80}})[»"”]', re.IGNORECASE)
# Без кавычек: «Анонсирован 4 сезон аниме Необъятный океан.» — до знака препинания.
_WORK_BARE_AFTER_ANCHOR = re.compile(
    rf'{_WORK_ANCHOR}\s+(?:по\s+\w+\s+)?([А-ЯЁA-Z][^.!?\n«»"“”()]{{2,80}})')
_WORK_QUOTED = re.compile(r'«([^»]{2,80})»|"([^"]{2,80})"|“([^”]{2,80})”|『([^』]{1,60})』|「([^」]{1,60})」')
# MyAnimeList пишет тайтл в одинарных кавычках: «'Phantom Busters' TV Anime
# Announced For 2027». Без них имя бралось вместе с хвостом — «Phantom
# Busters' TV», база его не узнавала, и анонс уходил в канал вторым постом.
# Кавычка засчитывается только на границе слова: апостроф в «JoJo's» и
# «Don’t» — не кавычка.
_WORK_SINGLE_QUOTED = re.compile(
    r"(?:^|(?<=[\s(«\"“:—–-]))['‘]([A-ZА-ЯЁ0-9][^'‘’\n]{1,80}?)['’](?=[\s,.:;!?)»”]|$)")
# Перед кавычками — песня, а не тайтл: «опенинг "SPIN"», «кавер на "…"».
_WORK_SONG_BEFORE = re.compile(
    r'(?:песн\w*|опенинг\w*|эндинг\w*|трек\w*|кавер\w*|сингл\w*|song|opening|ending|theme|'
    r'single|cover|sigla|by|di|\)\s*:)[^«"“』」]{0,25}$', re.IGNORECASE)
# Альтернативное название в скобках начинается с заглавной: «(Grand Blue)».
# Строчные скобки — пояснения: «(attualmente in corso)», «(pre-rilascio)».
_WORK_PAREN_LATIN = re.compile(r'\((?!(?:Part|Parte|Season|Stagione)\b)([A-Z][^()]{3,80})\)')
# Латинское имя внутри русского или итальянского текста: «Super Psychic
# Policeman Chojo», «Grand Blue Dreaming». Служебные слова внутри имени
# допускаются, с них имя начинаться не может.
# «&» внутри имени тоже связка: «Welsh & Shedar» рвалось на два огрызка, и
# итальянский анонс того же тайтла оставался без ключа — отдельным постом.
_WORK_LATIN_RUN = re.compile(
    r"(?:[A-Z][\w'’.!-]*|\d+)(?:[ :]+(?:[A-Z][\w'’.!-]*|(?:and|the|of|to|an|in|on|no|wa|ga|a|x)\b|&|\d+))*"
    r"(?:[ :]+(?:[A-Z][\w'’.!-]*|\d+))")
# Английский заголовок: имя стоит в начале, до первого слова о событии.
_WORK_EN_STOP = re.compile(
    r"(?:’s|'s)\b|\s+(?:Season|Part|Episode|Movie|Film|Anime|Manga|Opening|Ending|Trailer|Teaser|"
    r"PV|Key|Visual|Reveals?|Announces?|Announced|Gets|Drops|Shares|Releases?|Released|Debuts|"
    r"Confirms|Confirmed|Returns|Unveils|Launches|New|First|Final|Officially|Author|Creator|"
    r"Director|Studio|Cast|Covers|Is|Will|Sets|Premieres?|Streaming|Live-Action)\b")
_WORK_SEASON_TAIL = re.compile(
    r'(?:[\s:,-]*(?:season|сезон|part|часть|cour)\s*\d+|\s+\d+(?:st|nd|rd|th)?\s+season|'
    r'\s+(?:ii|iii|iv|v|vi)|\s+\d{1,2})\s*$', re.IGNORECASE)
_LATIN_LETTERS = re.compile(r'[A-Za-z]')
_OTHER_LETTERS = re.compile(r'[А-Яа-яЁё぀-ヿ一-鿿]')


def _clean_work_name(name: str) -> str:
    name = re.sub(r'\s+', ' ', str(name or '')).strip(' .,:;!?—–-"\'«»“”‘’')
    return _WORK_SEASON_TAIL.sub('', name).strip(' .,:;!?—–-')


# Слова оформления поста, а не имени: итальянский канал пишет «Main PV della
# serie…», «Key visual e main PV della Parte 2…», и первым «тайтлом» из
# заголовка выходило «Main PV» или «Parte». Такой кандидат тратил запрос к
# базе на пустое место, а тайтл, стоявший дальше, до сверки не доживал.
_WORK_GENERIC_WORDS = frozenset((
    'main', 'key', 'pv', 'cm', 'teaser', 'trailer', 'visual', 'visuals', 'opening', 'ending',
    'parte', 'part', 'season', 'stagione', 'episode', 'episodio', 'anime', 'manga', 'official',
    'new', 'nuovo', 'nuova', 'final', 'promo', 'video', 'tv', 'the', 'and', 'of', 'e', 'il',
    'la', 'di', 'della', 'dell', 'cour'))


def _generic_work_name(name: str) -> bool:
    words = re.findall(r"[\w'’]+", name.casefold())
    return bool(words) and all(w in _WORK_GENERIC_WORDS or w.isdigit() for w in words)


# Английская «голова» заголовка кончается на первом слове о событии, но
# начинаться может с чужого: «Rudy Takes Action in Mushoku Tensei Season 3…».
# Хвост головы из слов с заглавной — «Mushoku Tensei» — отдельный кандидат.
_WORK_EN_TAIL_RUN = re.compile(r"\s(?:in|from|for|with|at|on|by)\s+((?:[A-Z][\w'’!-]*\s+)+[A-Z][\w'’!-]*)\s*$")


def _explicit_work_names(text: str) -> list[str]:
    """Имена, выделенные самим текстом: кавычками, скобками, словом «аниме»."""
    found: list[str] = []
    found += [m.group(1) for m in _WORK_QUOTED_AFTER_ANCHOR.finditer(text)]
    found += [m.group(1) for m in _WORK_BARE_AFTER_ANCHOR.finditer(text)]
    found += _WORK_PAREN_LATIN.findall(text)
    for m in _WORK_QUOTED.finditer(text):
        if not _WORK_SONG_BEFORE.search(text[max(0, m.start() - 40):m.start()]):
            found.append(next(group for group in m.groups() if group))
    for m in _WORK_SINGLE_QUOTED.finditer(text):
        if not _WORK_SONG_BEFORE.search(text[max(0, m.start() - 40):m.start()]):
            found.append(m.group(1))
    return found


def story_explicit_work_names(news_or_title) -> list[str]:
    """Имена из заголовка, которые источник выделил сам: кавычки, скобки,
    «аниме по манге X». В отличие от «головы» английского заголовка это
    действительно название, а не «Netflix Locks Down»."""
    out: list[str] = []
    for name in _explicit_work_names(_story_title_of(news_or_title)):
        name = _clean_work_name(name)
        if (len(re.findall(r'\w', name)) >= 3 and not _generic_work_name(name)
                and name.casefold() not in (x.casefold() for x in out)):
            out.append(name)
    return out


_FIRST_SENTENCE = re.compile(r'^.*?(?:[.!?。！？](?=\s|$)|$)', re.DOTALL)


def _summary_lead(news_or_title) -> str:
    """Первое предложение описания — только для новости-словаря."""
    if not isinstance(news_or_title, dict):
        return ''
    summary = re.sub(r'\s+', ' ', str(news_or_title.get('summary') or '')).strip()[:400]
    match = _FIRST_SENTENCE.match(summary)
    return match.group(0).strip() if match else ''


def story_work_names(news_or_title, limit: int = 4) -> list[str]:
    """Имена тайтла из заголовка — в порядке надёжности, без повторов.

    Порядок важен: бот проверяет кандидатов по очереди и берёт первого
    подтверждённого. Имя после слова «аниме» надёжнее любых кавычек: в
    кавычках бывает и песня («опенинг "SPIN"»), и цитата.
    """
    title = _story_title_of(news_or_title)
    found: list[str] = _explicit_work_names(title)
    latin = len(_LATIN_LETTERS.findall(title))
    other = len(_OTHER_LETTERS.findall(title))
    english = latin and not other and not re.search(r"\b(?:dell|della|di|il|la|stagione|annunciat\w*)\b",
                                                   title, re.IGNORECASE)
    if english:
        head = _WORK_EN_STOP.split(title, maxsplit=1)[0]
        found.append(head)
        tail = _WORK_EN_TAIL_RUN.search(head)
        if tail:
            found.append(tail.group(1))
    else:
        found += [m.group(0) for m in _WORK_LATIN_RUN.finditer(title)]
    # Длинное название с подзаголовком Shikimori может не найти целиком:
    # «Старик из деревни становится Святым мечом: Хоть я и был…» — пробуем и
    # часть до двоеточия, если в ней больше одного слова.
    found += [name.split(':')[0] for name in list(found)
              if ':' in name and len(name.split(':')[0].split()) >= 2]
    out: list[str] = []

    def add(names) -> None:
        for name in names:
            name = _clean_work_name(name)
            letters = len(re.findall(r'\w', name))
            if (letters >= 3 and not _generic_work_name(name)
                    and name.casefold() not in (x.casefold() for x in out)):
                out.append(name)

    add(found)
    if not out:
        # Заголовок-лозунг без имени: «🍀 КЛЕВЕР ПОЙДЕТ ДО КОНЦА.». Тайтл тогда
        # назван в первой фразе («…2 сезон «Черного Клевера» станет
        # финальным»), и без неё новость не склеивалась с двумя другими
        # пересказами того же инсайда. Берём оттуда только явно выделенные
        # имена: первая фраза статьи — не заголовок, «голова» в ней — мусор.
        add(_explicit_work_names(_summary_lead(news_or_title)))
    return out[:limit]


# Тип события на любом языке. Разные слова одной новости сводятся к одному
# роду: «постер», «ключевой визуал» и «анонс сезона» в один день — один анонс;
# трейлер, серия, песня, перерыв и озвучка — самостоятельные новости.
_EVENT_CLASS_PATTERNS = {
    # «Gets Third Season» и «получит второй сезон» — тоже анонс: так пишут
    # MyAnimeList и половина русских каналов.
    'announce': r'анонс\w*|продл\w*|объявл\w*|подтверд\w*|announce\w*|confirm\w*|renew\w*|'
                r'greenlit|annunciat\w*|confermat\w*|制作決定|決定|'
                r'gets\s+(?:a\s+)?(?:\w+\s+)?(?:season|sequel|anime|movie|film)|'
                r'получ(?:ит|ил|ила|ило|ат)\s+(?:\w+\s+)?(?:сезон\w*|продолжени\w*|экранизаци\w*)',
    'visual': r'постер\w*|иллюстрац\w*|визуал\w*|обложк\w*|poster\w*|visual\w*|key art|first look|'
              r'new look|ビジュアル|ポスター',
    'date': r'премьер\w*|дата\s+(?:выхода|премьеры)|выйд[её]т|стартует|premiere\w*|release date|'
            r'inizier\w*|放送開始|配信開始',
    # «Тизер-постер» и «teaser visual» — картинка, а не ролик.
    # «Main Promo» у MyAnimeList — тот же ролик, что «Trailer» у Crunchyroll.
    'trailer': r'трейлер\w*|(?:тизер|teaser)\w*(?![\s-]*(?:визуал|постер|visual|poster))|отрыв\w*|'
               r'фрагмент\w*|ролик\w*|промо\w*|trailer\w*|sneak peek|(?<![a-z])pv(?![a-z])|予告|'
               r'(?<![a-z])promo(?![a-z])|promotional video',
    'episode': r'сери[яиюей]|эпизод\w*|кадры|episode\w*|episodio|第\s*\d+\s*話|あらすじ|場面カット',
    'music': r'опенинг\w*|эндинг\w*|песн\w*|саундтрек\w*|opening|ending|theme song|(?<![a-z])ost(?![a-z])|'
             r'sigla|主題歌',
    'break': r'перерыв\w*|пауз\w*|hiatus|on break|(?<![a-z])break(?![a-z])|pausa|休載',
    'cast': r'озвуч\w*|сэйю|актёр\w*|актер\w*|(?<![a-z])cast(?![a-z])|voice actor\w*|声優|キャスト',
    'health': r'больниц\w*|госпитализ\w*|hospital\w*|入院',
    'collab': r'коллаборац\w*|collab\w*|コラボ',
    'death': r'скончал\w*|(?<![а-я])умер(?:ла|ли)?(?![а-я])|passed away|(?<![a-z])died(?![a-z])|逝去|死去',
    'delay': r'перенос\w*|перенес\w*|отлож\w*|delay\w*|postpone\w*|rinviat\w*|延期',
    # Инсайд — своё событие. Три канала пересказали одну утечку про «Чёрный
    # клевер» тремя заголовками без единого слова о событии, и все три ушли
    # в канал: без рода события склейка по тайтлу запрещена.
    'rumor': r'слух\w*|инсайд\w*|утечк\w*|(?<![a-z])leak\w*|rumou?r\w*|insider\w*|reportedly|リーク',
}
_EVENT_CLASS_RE = {name: re.compile(pattern, re.IGNORECASE)
                   for name, pattern in _EVENT_CLASS_PATTERNS.items()}
_EVENT_FAMILY = {'announce': 'announce', 'visual': 'announce', 'date': 'announce',
                 'trailer': 'trailer', 'episode': 'episode', 'music': 'music',
                 'break': 'break', 'cast': 'cast', 'health': 'health', 'collab': 'collab',
                 'death': 'death', 'delay': 'delay', 'rumor': 'rumor'}


def _event_classes_of(text: str) -> frozenset:
    return frozenset(name for name, pattern in _EVENT_CLASS_RE.items() if pattern.search(text))


def story_event_classes(news_or_title) -> frozenset:
    """Род события по заголовку; заголовок-лозунг — по первой фразе текста.

    «🍀 КЛЕВЕР ПОЙДЕТ ДО КОНЦА.» ничего не говорит о событии, а первая фраза
    («Инсайдеры утверждают, что…») говорит. Текст смотрим, только когда
    заголовок молчит: в глубине статьи упоминается что угодно.
    """
    classes = _event_classes_of(_story_title_of(news_or_title))
    if not classes:
        classes = _event_classes_of(_summary_lead(news_or_title))
    return classes


def story_event_families(news_or_title) -> set:
    return {_EVENT_FAMILY[c] for c in story_event_classes(news_or_title)}


def story_events_compatible(a, b) -> bool:
    """Одно ли событие по типу: тип распознан у обоих и совпадает по роду.

    Не распознан хотя бы у одного — не склеиваем. «Коллаборация „Атаки
    титанов“ с Бургер Кингом» и «Attack on Titan Shares New Look» — один
    тайтл и ни одного знакомого слова о событии, а новости разные. У большой
    франшизы таких «безымянных» новостей несколько в день, и лишний повтор
    дешевле тихой потери.
    """
    return bool(story_event_families(a) & story_event_families(b))


# Номер сезона, части или серии. Порядковое слово считается номером только
# рядом с таким словом: «First Look» — не первый сезон, а «первые два
# сезона» — не номер. Дата — не номер: «October 22» у одного источника не
# должна разводить его с тем, кто дату не назвал.
# Опенинг, эндинг, том и глава — тоже номер: «седьмой опенинг» и
# «четырнадцатый эндинг» одного тайтла — разные песни.
_WORK_UNIT = (r'(?:season|seasons|part|cour|episode|opening|ending|volume|chapter|'
              r'сезон\w*|част\w*|сери\w*|эпизод\w*|опенинг\w*|эндинг\w*|том\w*|глав\w*|'
              r'stagione|parte|episodio|sigla)')
# До десятого английские и русские порядковые знает _ordinal_word_value;
# здесь — дальше десятого (у опенингов и эндингов они бывают) и итальянские.
_WORK_ORDINAL = {
    'eleventh': '11', 'twelfth': '12', 'thirteenth': '13', 'fourteenth': '14',
    'fifteenth': '15', 'sixteenth': '16', 'seventeenth': '17', 'eighteenth': '18',
    'nineteenth': '19', 'twentieth': '20',
    'prima': '1', 'seconda': '2', 'terza': '3', 'quarta': '4', 'quinta': '5', 'sesta': '6',
    'primo': '1', 'secondo': '2', 'terzo': '3', 'quarto': '4', 'quinto': '5', 'sesto': '6',
}
_WORK_ORDINAL_BEFORE_UNIT = re.compile(
    rf'\b([a-zа-яё]+)\s+{_WORK_UNIT}\b', re.IGNORECASE)
# Итальянские месяцы — для итальянского телеграм-канала: «dal 2 ottobre»
# считалось номером сезона «2», и анонс расходился с тем же анонсом без даты.
_WORK_MONTHS = (r'(?:jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|'
                r'dec\w*|январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|'
                r'сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|gennaio|febbraio|marzo|aprile|maggio|'
                r'giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)')
_WORK_DATE = re.compile(
    rf'\b{_WORK_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:-?го)?\s+{_WORK_MONTHS}\b'
    r'|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b', re.IGNORECASE)


def story_work_numbers(news_or_title) -> frozenset:
    """Номера сезона, части, серии — без годов и дат."""
    title = _WORK_DATE.sub(' ', _story_title_of(news_or_title))
    numbers = {n.lstrip('0') or '0' for n in re.findall(r'(?<![\w.])\d{1,4}(?![\w.])', title)
               if not 1900 <= int(n) <= 2100}
    for match in _WORK_ORDINAL_BEFORE_UNIT.finditer(title):
        value = _WORK_ORDINAL.get(match.group(1).lower()) or _ordinal_word_value(match.group(1))
        if value:
            numbers.add(value)
    return frozenset(numbers)


def same_work_event(a: dict, b: dict) -> bool:
    """Та же новость о том же тайтле: ключ тайтла, номера, тип и носитель.

    Ключ ставит бот после сверки с базой тайтлов (``_work_key``). Без ключа у
    любого из двух — ответа нет: сравнение по словам делают другие проверки.
    """
    key_a, key_b = a.get('_work_key'), b.get('_work_key')
    if not key_a or key_a != key_b:
        return False
    # Номер, названный только одним источником, — не разница: «Отрывок из
    # «Cyberpunk: Edgerunners 2»» и «Тизер нового сезона «Киберпанк»» —
    # один ролик, но требование равных номеров разводило их, и в канал
    # ушли три поста. Разные номера у обоих («сезон 2» и «сезон 3») — разные
    # новости по-прежнему.
    if numbers_conflict(story_work_numbers(a), story_work_numbers(b)):
        return False
    media_a, media_b = _story_media(a), _story_media(b)
    if media_a and media_b and not (media_a & media_b):
        return False
    return story_events_compatible(a, b)


def numbers_conflict(a, b) -> bool:
    """Номера противоречат, только если у КАЖДОЙ стороны есть свой номер.

    {2} и {1, 2} — «2 сезон» и «1 серия 2 сезона»: один источник назвал
    больше, это не другая новость. {12} и {14} — разные серии.
    """
    a, b = set(a or ()), set(b or ())
    return bool(a - b) and bool(b - a)


# ============== ОБЩЕЕ ЯДРО ЗАГОЛОВКОВ ДЛЯ ДЕДУПА ==============
# Рубрика — слова, которыми источник оформляет любую новость: «кадры»,
# «серии», «постер», «announced for». Совпадение по ним одним ничего не
# говорит о том, что новость та же: «Кадры 12 серии «Табакошки»» и «Кадры
# к 14 серии «Реинкарнации безработного»» совпадали на две трети слов, а
# «…Anime Announced for 2027» у двух разных тайтлов — общей строкой в 16
# букв «announcedfor2027». Обе пары считались повтором, и настоящая новость
# молча терялась. Дубль признаём, только если общее есть и помимо рубрики.
_RUBRIC_EXACT = frozenset((
    # английские
    'season', 'seasons', 'episode', 'episodes', 'trailer', 'trailers', 'teaser', 'visual',
    'visuals', 'key', 'main', 'poster', 'cast', 'staff', 'theme', 'song', 'songs', 'opening',
    'ending', 'promo', 'video', 'preview', 'anime', 'manga', 'announced', 'announces',
    'announce', 'reveals', 'revealed', 'unveils', 'unveiled', 'new', 'for', 'part', 'cour',
    'premiere', 'premieres', 'release', 'releases', 'released', 'date', 'film', 'movie',
    'series', 'official', 'additional', 'more', 'first', 'final', 'info', 'character',
    'characters', 'adaptation', 'gets', 'confirmed', 'confirms', 'tv', 'the', 'and', 'with',
    'from', 'its', 'this', 'that', 'will', 'has', 'have', 'watch', 'now', 'launches',
    'debuts', 'coming', 'shares', 'drops', 'returns', 'animation', 'animated',
    # русские служебные
    'выдали', 'показали', 'представили', 'опубликовали', 'вышел', 'вышла', 'вышли',
    'новый', 'новая', 'новое', 'новые', 'нового', 'новой', 'новую', 'аниме', 'манга',
    'манги', 'манге', 'мангу', 'для', 'уже', 'года', 'году',
    # итальянские (VanitasNews) и месяцы
    'stagione', 'annunciata', 'annunciato', 'annunciati', 'annunciate', 'dell', 'della',
    'del', 'che', 'inizierà', 'prossimamente', 'nuovo', 'nuova', 'serie', 'animata', 'sarà',
    'diretta', 'diretto', 'presso', 'trasmessa', 'dal', 'parte', 'adattamento', 'episodi',
    'episodio', 'disponibile', 'ora', 'doppiato', 'italiano', 'essere', 'giappone',
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september',
    'october', 'november', 'december', 'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio',
    'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre',
))
# Русские рубрики — по основе: «серии», «серия», «серий» — одно слово.
_RUBRIC_STEMS = ('кадр', 'сери', 'сезон', 'постер', 'трейлер', 'тизер', 'опенинг', 'эндинг',
                 'эпизод', 'анонс', 'премьер', 'релиз', 'дат', 'визуал', 'промо', 'ролик',
                 'отрыв', 'фрагмент', 'половин', 'част', 'озвуч', 'каст', 'состав', 'фильм',
                 'январ', 'феврал', 'март', 'апрел', 'июн', 'июл', 'август', 'сентябр',
                 'октябр', 'ноябр', 'декабр')


def is_rubric_word(word: str) -> bool:
    """Слово оформления, а не названия: само по себе не делает новости одной."""
    low = str(word or '').casefold().replace('ё', 'е')
    if not low or low.isdigit() or low in _RUBRIC_EXACT:
        return True
    if _ordinal_word_value(low) is not None or low in _WORK_ORDINAL:
        return True
    return bool(re.fullmatch(r'[а-я]+', low)) and any(
        low.startswith(stem) and len(low) - len(stem) <= 4 for stem in _RUBRIC_STEMS)


def title_core_words(words) -> set:
    """Слова заголовка без рубрики и чисел — то, что называет предмет новости."""
    return {w for w in words or () if not is_rubric_word(w)}


def title_numbers(text: str, words=()) -> set:
    """Номера заголовка: цифры и порядковые числительные («второй», «fourth»)."""
    found = {n.lstrip('0') or '0' for n in re.findall(r'\d+', str(text or ''))}
    for word in words or ():
        value = _ordinal_word_value(word) or _WORK_ORDINAL.get(str(word).casefold())
        if value:
            found.add(value)
    return found
