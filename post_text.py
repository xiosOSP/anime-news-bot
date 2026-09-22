"""Текст поста: предложения, обрывки, ссылки и заголовки телеграм-постов.

Чистая текстовая логика без состояния. Здесь решается, как выглядит пост без
модели: какое предложение целое, где многоточие — обрыв, а не конец мысли,
какая первая строка телеграм-поста — рубрика или баннер, а какая — заголовок.
Перевод, отправка и настройки остаются в боте.

Вынесено из anime_news_bot.py вторым, после news_stories.py: у этого кода нет
подмен в тестах, поэтому перенос не меняет того, что тесты проверяют.
"""
import re


def smart_truncate(text: str, limit: int) -> str:
    """Обрезает текст по границе слова, укладываясь РОВНО в limit символов.

    Многоточие — тоже символ: раньше ``text[:limit] + '…'`` давало limit + 1 и
    подпись, посчитанная впритык под лимит Telegram, отвергалась Bot API.
    """
    if not text or len(text) <= limit:
        return text
    if limit <= 1:
        return '…'[:max(0, limit)]
    cut = text[:limit - 1].rsplit(' ', 1)[0]
    # Не оставляем "хвост" в виде запятой/тире
    cut = cut.rstrip(',—-:;')
    return cut + '…'


# Строка, в которой нет ни одной буквы и ни одной цифры, — это украшение:
# эмодзи-разделитель, ряд точек, стрелка. Заголовком она быть не может.
_TG_MEANINGFUL_RE = re.compile(r'[A-Za-zА-Яа-яЁё0-9]')


# Подпись канала под постом: «@channel», ссылка на t.me, либо короткая строка
# с названием самого источника.
_TG_SIGNATURE_RE = re.compile(r'(?:^|\s)@[A-Za-z0-9_]{4,}\s*$|t\.me/', re.IGNORECASE)


def _tg_strip_decoration(value: str) -> str:
    """Строка без эмодзи и пунктуации — для сравнения с названием канала."""
    return re.sub(r'[^A-Za-zА-Яа-яЁё0-9]+', ' ', str(value or '')).strip().lower()


def _tg_is_signature(line: str, channel: str, label: str) -> bool:
    """Похожа ли строка на подпись канала, а не на текст новости."""
    if len(line) > 60:
        return False                     # длинная строка — это уже содержание
    if _TG_SIGNATURE_RE.search(line):
        return True
    clean = _tg_strip_decoration(line)
    if not clean:
        return False
    for name in (channel, label):
        other = _tg_strip_decoration(name)
        # Название канала целиком внутри короткой строки — это подпись.
        if other and len(other) >= 4 and (clean == other or other in clean or clean in other):
            return True
    return False


# Рубрика вместо заголовка. Русские каналы открывают пост одним словом —
# «Манга.», «Аниме», «Слух:», — и это не заголовок, а полка, на которую канал
# кладёт новость. В нашем посте такая строка занимала место заголовка: читатель
# видел «Манга.», а суть новости уезжала в тело.
_TG_CATEGORY_WORDS = frozenset({
    'аниме', 'манга', 'манхва', 'манхуа', 'маньхуа', 'ранобэ', 'ранобе',
    'новость', 'новости', 'слух', 'слухи', 'анонс', 'анонсы', 'трейлер',
    'тизер', 'релиз', 'кино', 'фильм', 'фильмы', 'сериал', 'сериалы',
    'игра', 'игры', 'косплей', 'арт', 'арты', 'дата', 'даты', 'музыка',
    'клип', 'обзор', 'подборка', 'объявление', 'важное', 'интересное',
    'anime', 'manga', 'manhwa', 'news', 'rumor', 'rumour', 'trailer',
    'teaser', 'release', 'movie', 'games', 'game',
})


# Редакционный голос источника. «Напоминаем, что…» — это чужой канал напоминает
# о том, что публиковал сам; у нас той публикации не было, и фраза превращает
# пост в чей-то чужой разговор.
# «что» обязательно: без него правило съедало бы сказуемое — «Отметим премьеру»
# превращалось в «Премьеру».
_TG_EDITORIAL_LEADIN_RE = re.compile(
    r'^(?:напоминаем|напомним|отметим|отмечу|подчеркнём|подчеркнем|добавим|'
    r'уточним|заметим)[,]?\s+(?:о том,\s*)?что\s+',
    re.IGNORECASE)


def _tg_is_category_line(line: str) -> bool:
    """Строка-рубрика или баннер: одно-два слова без содержания новости."""
    clean = _tg_strip_decoration(line)
    if not clean:
        return False
    words = clean.split()
    # Двусловные рубрики вроде «аниме новости» тоже встречаются, но всё, что
    # длиннее, уже несёт факт — такую строку трогать нельзя.
    if len(words) > 2:
        return False
    if all(word in _TG_CATEGORY_WORDS for word in words):
        return True
    # Список слов закрыт, а баннеры каналы придумывают свои: «🔥 СРОЧНО»,
    # «⚡️ ВАЖНО», «BREAKING». Общий признак у них один — капслок: заголовок
    # из одного-двух слов капсом содержания не несёт, он кричит о нём.
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _tg_drop_editorial_voice(text: str) -> str:
    """Убирает чужой редакционный зачин, оставляя сам факт."""
    cleaned = _TG_EDITORIAL_LEADIN_RE.sub('', str(text or '').strip(), count=1)
    if not cleaned:
        return str(text or '').strip()
    return cleaned[0].upper() + cleaned[1:]


def _tg_split_leading_sentence(line: str) -> tuple[str, str]:
    """Делит строку на первое предложение и остаток. Остаток пуст — делить нечего.

    Границу берём ту же, что и при разборе описаний: точка после сокращения
    («12 окт.», «2022 г.») предложением не заканчивается, и заголовок по ней
    рвать нельзя.
    """
    line = str(line or '').strip()
    match = next((m for m in _SENTENCE_END_RE.finditer(line)
                  if line[:m.end()].count('«') == line[:m.end()].count('»')
                  and line[:m.end()].count('(') == line[:m.end()].count(')')
                  and not re.search(r'\b(?:Dr|Mr|Mrs|Ms|No|vol|д-р)\.$',
                                    line[:m.end()], re.I)), None)
    if not match:
        return line, ''
    head, tail = line[:match.end()].strip(), line[match.end():].strip()
    # Заголовок в два слова — это рубрика, а не заголовок: «Слух.» с текстом
    # под ним выглядит ровно той поломкой, от которой уходим.
    if not tail or len(head.split()) < 3:
        return line, ''
    return head, tail


def _tg_title_and_summary(full_text: str, channel: str, label: str) -> tuple[str, str]:
    """Делит текст телеграм-поста на заголовок и тело.

    Первая строка не всегда заголовок. Каналы начинают пост декоративным
    эмодзи на отдельной строке, а заканчивают подписью с собственным именем.
    Раньше эмодзи становился заголовком — в канал уходило «🔍.», — а подпись
    источника уезжала в тело поста, хотя своё имя канал у себя не публикует.

    Ровно та же беда со строкой-рубрикой: в канал уходил заголовок «Манга.».
    """
    lines = [ln.strip() for ln in str(full_text or '').split('\n') if ln.strip()]
    kept = [ln for ln in lines
            if _TG_MEANINGFUL_RE.search(ln) and not _tg_is_signature(ln, channel, label)]
    if not kept:
        return '', ''
    # Рубрику снимаем, только пока под ней есть содержание: пост, кроме неё не
    # состоящий ни из чего, лучше отдать как есть, чем потерять.
    while len(kept) > 1 and _tg_is_category_line(kept[0]):
        # A rumour label carries factual uncertainty, unlike a topic banner.
        if _tg_strip_decoration(kept[0]) in ('слух', 'слухи', 'rumor', 'rumour'):
            kept[1] = 'Слух: ' + kept[1]
        kept = kept[1:]
    head = _tg_drop_editorial_voice(kept[0])
    rest = [_tg_drop_editorial_voice(line) for line in kept[1:]]
    # Первая строка бывает целым абзацем. Заголовком тогда служил весь абзац,
    # а тело поста оставалось пустым: читатель получал стену текста вместо
    # заголовка и ничего под ним. Заголовок — первое предложение, остальное
    # спускаем в тело, где ему и место.
    head, tail = _tg_split_leading_sentence(head)
    if tail:
        rest.insert(0, tail)
    # The source headline is evidence for the editor: never cut it mid-name.
    return head, ' '.join(rest)[:3500]


# Ссылки в тексте поста. Ловим и голые домены: RSS-описания и телеграм-посты
# сплошь и рядом пишут «читайте на animenewsnetwork.com» без схемы.
_POST_URL_RE = re.compile(
    r'\b(?:https?://|www\.)\S+'
    r'|\bt\.me/\S+'
    r'|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|ru|io|tv|jp|me|one|gg|co|info|news)'
    r'(?:/\S*)?',
    re.IGNORECASE)


# Подводка к ссылке. После выкидывания URL она остаётся висеть предлогом в
# никуда: «читайте на .», «подписывайтесь на».
_LINK_LEADIN_WORDS = (
    r'читайте|читать|смотрите|смотреть|подробности|подробнее|источник|'
    r'подписывайтесь|подписаться|больше|оригинал|via|source|read\s+more|'
    r'read|watch|see|more|full\s+story|details'
)


_LEADIN_BEFORE_RE = re.compile(
    rf'(?:\b(?:{_LINK_LEADIN_WORDS})\b[^.!?]{{0,30}}?)?'
    rf'[\s,:;—–-]*\b(?:на|в|по|у|at|on|in|to|by|from)?\s*$', re.IGNORECASE)


# Союзы И предлоги: «Аниме про» — такой же обрывок, как «Аниме и», но предлоги
# в списке отсутствовали, и хвост доезжал до поста.
_TRAILING_CONNECTOR_RE = re.compile(
    r'[\s,;:—–-]*\b(?:и|а|но|или|же|что|чтобы|как|где|когда|'
    r'про|при|об|обо|о|для|из|изо|от|ото|до|без|через|под|над|перед|между|'
    r'к|ко|с|со|у|во|во время|в|на|по|за|the|and|or|but|'
    r'with|for|of|to|in|on|at|by|from)\s*$', re.IGNORECASE)


# Слова, которые сами по себе ничего не сообщают: служебные и те, что были
# подводкой к ссылке. Предложение, состоящее только из них, — не текст.
_LOW_CONTENT_WORDS = frozenset("""
читайте читать смотрите смотреть подробности подробнее источник подписывайтесь
подписаться больше оригинал сайте сайт здесь тут ниже выше наш нашем нашего
и а но или же что чтобы как где когда на в по у из от для про при об о
read more watch see source via full story details here our site link click
the and or but with for of to in on at by from you it this that all
""".split())


def _sentence_is_empty_without_link(text: str) -> bool:
    """Осталось ли в предложении хоть что-то своё после удаления ссылки.

    «Читайте на сайте» без ссылки не несёт ничего, а вот «Премьера в апреле»
    несёт — и выбрасывать её вместе со ссылкой нельзя. Считаем не длину, а
    содержательные слова: длинная подводка длиннее короткого факта, и по
    длине их не различить.
    """
    words = re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).strip().lower().split()
    meaningful = [w for w in words if len(w) > 2 and w not in _LOW_CONTENT_WORDS]
    return len(meaningful) < 2


def _strip_links(text: str) -> str:
    """Убирает ссылки из текста поста вместе с подводкой к ним.

    В канал ссылки не идут: подписчику некуда по ним ходить, а чужой t.me в
    своём канале — прямая реклама конкурента. Убирать надо до перевода:
    переводчик коверкает домены («www.Crunchyroll.com») и тратит на них лимит.

    Чистим по предложениям. Предложение, от которого после удаления ссылки
    осталась одна подводка, выбрасываем целиком: «Читайте на» без адреса —
    это не текст, а огрызок. Предложение с собственным смыслом сохраняем,
    убрав ссылку и подводку к ней.
    """
    if not text or not _POST_URL_RE.search(str(text)):
        return text or ''
    out = []
    for sentence in re.split(r'(?<=[.!?…])\s+', str(text)):
        if not _POST_URL_RE.search(sentence):
            out.append(sentence)
            continue
        cleaned = _POST_URL_RE.sub('\u0001', sentence)
        head, _, tail = cleaned.partition('\u0001')
        head = _LEADIN_BEFORE_RE.sub('', head)
        tail = tail.replace('\u0001', ' ')
        merged = f'{head.strip()} {tail.strip()}'.strip()
        merged = re.sub(r'\s+([.,;:!?…])', r'\1', merged)
        merged = re.sub(r'\s{2,}', ' ', merged).strip(' ,;:—–-')
        if not merged or _sentence_is_empty_without_link(merged):
            continue
        if merged and merged[0].islower():
            merged = merged[0].upper() + merged[1:]
        if not merged.endswith(('.', '!', '?', '…')):
            merged += '.'
        out.append(merged)
    return ' '.join(x.strip() for x in out if x.strip()).strip()


def _extract_sentences(text: str, max_sentences: int = 3, max_len: int = 700) -> str:
    """Извлекает до max_sentences первых предложений (для более полного текста поста).
    Границы предложений — латинские/кириллические . ! ? и японские 。！？.
    Общая длина ограничена max_len. Хвосты-обрывки чистятся как в _extract_first_sentence."""
    if not text:
        return ''
    text = text.strip()
    # Чистим хвосты обрезки источником
    text = re.sub(r'\s*\[\.{2,3}\]\s*$', '', text)
    text = re.sub(r'\s*\[…\]\s*$', '', text)
    text = re.sub(r'\s*\(?(?:read more|continue reading|подробнее)\)?\s*$', '', text, flags=re.IGNORECASE)

    sentences: list[str] = []
    pos = 0
    # Тот же паттерн границы, что и для одного предложения (учитывает сокращения и цифры)
    pattern = re.compile(r'(?<!\s\d)[.!?](?:\s+[«"A-ZА-ЯЁ]|\s*$)|[。！？]')
    for m in pattern.finditer(text):
        end = m.start() + 1
        chunk = text[pos:end].strip()
        if chunk:
            sentences.append(chunk)
        pos = end
        if len(sentences) >= max_sentences:
            break
    # Если границ не нашлось совсем — берём весь текст как одно «предложение»
    if not sentences:
        sentences = [text]

    result = ' '.join(sentences).strip()
    if len(result) > max_len:
        result = smart_truncate(result, max_len)
    # Финальная чистка висящих знаков
    result = re.sub(r'\s*,\s*(?:…|\.{2,3})\s*$', '', result)
    result = re.sub(r'\s*\([^)]{0,6}$', '', result)
    result = re.sub(r'[\s,;:—–-]+$', '', result)
    return _drop_unfinished_tail(result.strip())


# Конец предложения и хвост-многоточие — рядом, потому что работают в паре:
# первое ищет настоящую границу, второе отличает обрыв от точки.
#
# Следующее предложение обязано начинаться с заглавной: точка в «12 окт.» и
# «2022 г.» — это сокращение, и резать по ней значит рвать фразу пополам. Тот
# же признак уже используется при разборе описаний.
_SENTENCE_END_RE = re.compile(
    r'(?<!\s\d)[.!?](?=\s+[«"„“A-ZА-ЯЁ]|\s*$)|[。！？]')


_ELLIPSIS_TAIL_RE = re.compile(r'\s*(?:…|\.{2,})\s*$')


# Граница части предложения, после которой остаётся осмысленный кусок.
# 80 символов — примерно строка: короче него обрывок уже ничего не сообщает.
_CLAUSE_END_RE = re.compile(r'^(.{80,})\s*[,;:—–]\s+\S', re.DOTALL)


def _drop_unfinished_tail(text: str) -> str:
    """Отрезает незаконченный хвост, оставляя только целые предложения.

    RSS-описания часто обрываются на полуслове, и граница предложения в них
    просто не встречается. Тогда в пост уходило что-то вроде «…снят человеком,
    чьё имя действительно очень длинное и» — читателю от такого хвоста нет
    никакой пользы, а пост выглядит сломанным.

    Лучше короче, но целиком: если целого предложения не осталось вовсе,
    отдаём пустоту, и пост живёт одним заголовком — он самодостаточен.
    """
    text = (text or '').strip()
    if not text:
        return ''
    # Многоточие в конце — не конец мысли, а отметка обрыва: так обрезает
    # описание сам источник («Сериал выйдет...») и так же обрезаем мы сами
    # в smart_truncate. Раньше эта строка считалась законченной и уходила в
    # пост как есть — это и есть тот «обрывистый пост», на который жалуются.
    if _ELLIPSIS_TAIL_RE.search(text):
        text = _ELLIPSIS_TAIL_RE.sub('', text).rstrip()
        bounds = list(_SENTENCE_END_RE.finditer(text))
        if bounds:
            return text[:bounds[-1].end()].strip()
        # Целого предложения нет вовсе — описание состоит из одной длинной
        # фразы. Выбросить её целиком значит потерять все факты, поэтому
        # отступаем до ближайшей границы части предложения: мысль обрывается,
        # но на паузе, а не на полуслове. Огрызок короче строки не спасти —
        # «Сериал выйдет» не сообщает ничего, и пост живёт заголовком.
        clause = _CLAUSE_END_RE.search(text)
        return clause.group(1).strip() if clause else ''
    if text.endswith(('.', '!', '?', '。', '！', '？')):
        return text
    # Ищем последнюю настоящую границу предложения и обрезаем по ней.
    bounds = list(_SENTENCE_END_RE.finditer(text))
    if bounds:
        return text[:bounds[-1].end()].strip()
    # Целого предложения нет. Обрывок на союзе или предлоге — это мусор:
    # такой хвост не сообщает ничего и только портит вид поста.
    if _TRAILING_CONNECTOR_RE.search(text):
        return ''
    return text


# ============== ОТПРАВКА ==============
def fit_to_limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'
