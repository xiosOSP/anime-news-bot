"""Conservative, offline chat rules. Uncertain context is left for review."""
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Verdict:
    category: str
    reason: str
    severity: int = 2
    confident: bool = True

    def as_dict(self):
        return vars(self).copy()


_HOMOGLYPHS = str.maketrans('aceopxykmth03', 'асеорхукмтноз')
_URL = r'(?:https?://|www\.|t\.me/|discord\.gg/)\S+'
# «Нато» из списка убрано: «на-то была причина» склеивалось в «нато» и
# удалялось как политика. Замаскированное «н.а.т.о» ловит модель.
_MASKED_WORD = re.compile(
    r'(?:шлюх\w*|проститут\w*|мраз\w*|долбоеб\w*|хуесос\w*|пидор\w*|'
    r'уеб\w*|ебан\w*|ебал\w*|сука|суки|сучк\w*|дебил\w*|идиот\w*|'
    r'мать|матери|мам[ауыое]|мамк\w*|тво[яюейих]+|сдохни\w*|убейся|'
    r'убью|зарежу|повесься|путин\w*|зеленск\w*|сво)\Z')
_SEPARATED = re.compile(r'(?<!\w)[а-яa-z0-9](?:[.\-_*|\s]+[а-яa-z0-9]){2,}(?!\w)')
_PUNCTUATED = re.compile(r'(?<!\w)[а-яa-z0-9]+(?:[.\-_*|]+[а-яa-z0-9]+)+(?!\w)')


def normalize(text: str) -> str:
    text = str(text or '')

    def fold(word):
        if re.search('[а-я]', word):
            return word.translate(_HOMOGLYPHS)
        return word

    def normalize_part(part):
        part = unicodedata.normalize('NFKC', part).casefold().replace('ё', 'е')
        # Ударения и прочие надстрочные знаки: «пи́дор» с ударением проходило
        # мимо всех правил. «Й» после NFKC — одна буква, её это не задевает.
        part = ''.join(c for c in part if unicodedata.category(c) not in ('Cf', 'Mn'))

        def unmask(match):
            original = match.group()
            joined = fold(re.sub(r'[.\-_*|\s]', '', original))
            joined = re.sub(r'(.)\1{2,}', r'\1', joined)
            # Do not join arbitrary words, initials or English phrases.
            return joined if _MASKED_WORD.fullmatch(joined) else original
        part = _PUNCTUATED.sub(unmask, part)
        part = _SEPARATED.sub(unmask, part)
        part = re.sub(r'\w+', lambda m: fold(m.group()), part)
        return re.sub(r'([а-я])\1{2,}', r'\1', part)

    # Paths and query strings can contain Cyrillic: preserve the whole URL.
    parts = re.split(f'({_URL})', text, flags=re.IGNORECASE)
    text = ''.join(part if index % 2 else normalize_part(part)
                   for index, part in enumerate(parts))
    return re.sub(r'\s+', ' ', text).strip()


# Nouns only: «админ, идиотский вопрос» addresses an admin but describes
# the question. A shared root does not make the adjective a personal insult.
_MILD_INSULT = r'(?:дебил|идиот)(?:а|у|ом|е|ы|ов|ам|ами|ах|ка|ки|ке|ку|кой|кою|ок|кам|ками|ках)?'
_INSULT = rf'(?:шлюх\w*|проститут\w*|твар(?:ь|и|ей|ям|ью|ями|ях)|мраз\w*|уеб\w*|ебан\w*|долбоеб\w*|пидор\w*|сука|суки|сучк\w*|говно|хуесос\w*|{_MILD_INSULT})'
_FAMILY = r'(?:мать|матер[ьиьюям]+|мам[ауыое]|мамк\w*|мамаш\w*|отец|отц\w*|пап[ауыое]|бат[яюеи]|сестр\w*|брат\w*|родител\w*|семь\w*|семе[йью]+)'
_YOUR = r'(?:тво(?:я|е|ю|и|й|его|ей|ему|им|их|ими|ем)|ваш(?:а|е|у|и|его|ей|ему|им|их|ими|ем)?)'
_TARGET_FAMILY = rf'(?:{_YOUR}\s+(?:вся\s+|все\s+|всю\s+)?{_FAMILY}|{_FAMILY}\s+{_YOUR})'
_FAMILY_BRIDGE = r'(?:[\s,—–:]+(?:это|просто|еще|та|такие|все|сам\w*|настоящ\w*|кончен\w*|ебан\w*|туп\w*|полн\w*|сборищ\w*|назову|считаю)\b){0,4}[\s,—–:]+'
# Оскорбление ПЕРЕД семьёй — «шлюха твоя мать» — без запятой. С запятой это
# междометие: «сука, твой брат опять выиграл», «я идиот, твоя сестра была
# права» — за такое был мут на сутки как за оскорбление семьи.
_FAMILY_BRIDGE_TIGHT = r'(?:[\s—–:]+(?:это|просто|еще|та|такие|все|сам\w*|настоящ\w*|кончен\w*|ебан\w*|туп\w*|полн\w*)\b){0,4}[\s—–:]+'
# «Твою мать», «мать твою» — ругательство-восклицание, а не слово о матери:
# «твою мать, сука, опять перенос». Оскорбление матери говорит о ней в
# именительном: «твоя мать …». Сексуальные фразы ловит отдельная проверка.
# Восклицание отделено знаком: «твою мать, сука, опять перенос». Без знака
# это уже дополнение: «твою мать назову шлюхой» — оскорбление.
_FAMILY_IDIOM = re.compile(r'\b(?:(?:твою|вашу)\s+(?:мать|маму)|(?:мать|маму)\s+(?:твою|вашу))\s*[,!?.—–]')
# «@» после буквы — это почта, а не упоминание канала.
_LINK = re.compile(r'https?://\S+|t\.me/\S+|discord\.gg/\S+|(?<![\w.])@[a-z0-9_]{5,}', re.IGNORECASE)
# «Без риска» — обещание скама только рядом с деньгами: «смотреть без риска
# спойлеров» удалялось как мошенничество, если рядом была ссылка.
_MONEY_CONTEXT = re.compile(r'\b(?:вложени\w*|доход\w*|деньг\w*|денег|заработ\w*|прибыл\w*|'
                            r'инвест\w*|руб\w*|usdt|крипт\w*|ставк\w*)|[%₽$]')
# «Трамп» с любым хвостом — это и «трамплин», и «трампарк»: в аниме про спорт
# трамплин встречается чаще, чем президент. Окончания перечислены закрытым
# списком, как у оскорблений: цена ошибки здесь — вердикт «политика» за
# разговор о прыжках с трамплина.
_TRUMP = r'трамп(?:а|у|ом|е|ы|ов|ам|ами|ах|ист\w*)?'
# Президент школьного совета — сюжет половины школьных аниме: арка «Госпожи
# Кагуи» целиком о выборах президента студсовета. Правила чата прямо говорят,
# что политика в сюжете — не политика, а без этой оговорки сообщение о ней
# удалялось с вызовом админа.
_PRESIDENT = (r'президента(?!\s+(?:(?:школьн|студенческ|ученическ)\w*\s+совет\w*|'
              r'студсовет\w*|клуба|класса))')
_POLITICS = re.compile(
    rf'\b(?:путин\w*|зеленск\w*|{_TRUMP}|байден\w*|навальн\w*|лукашенко|'
    r'нато|сво|слава украине|героям слава|единая россия|'
    rf'голосуйте за\s+(?:\w+\s+){{0,2}}(?:партию|{_PRESIDENT}|депутата|мэра|губернатора)|'
    rf'выборы {_PRESIDENT}|война (?:в|на|с) (?:украин\w*|росси\w*)|'
    r'putin|zelensky\w*|trump|biden|nato)\b')
# Формы, которые одинаково читаются и как фамилия, и как обычное слово.
# Фамилия: путина (род./вин.), путину (дат.), путине (предл.). Рыболовный
# сезон: путина (им.), путины, путине, путину, путиной.
_POLITICS_AMBIGUOUS = frozenset({'путина', 'путину', 'путине', 'путины', 'путиной'})
_PHONE = re.compile(r'(?<!\d)\+?\d[\d ()\-]{8,20}\d(?!\d)')
_IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_VERSION_BEFORE = re.compile(r'\b(?:билд\w*|верси\w*|патч\w*|обновлени\w*|build|version|ver|v)\W*$')
_ADDRESS = re.compile(r'\b(?:улиц[аеуы]|ул\.|проспект|пр-т|переулок|пер\.)\s+[а-яa-z][\w -]{1,40}[, ]+(?:(?:д\.|дом)\s*)?\d{1,4}\b')
_DOX_INTENT = re.compile(r'\b(?:сливаю|слейте|слил|деанон\w*|докс\w*|(?:его|ее|твой) (?:номер|адрес|телефон|ip)|вот (?:номер|адрес|телефон)\s+@\w+)\b')
_THREAT = re.compile(
    r'\b(?:тебя|вас)\s+(?:(?:я|сейчас|завтра|скоро|лично|нахуй)\s+){0,3}(?:убью|зарежу|изобью|найду и убью)\b|'
    r'\b(?:убью|зарежу|изобью)\s+(?:(?:нахуй|сегодня|завтра|скоро|лично)\s+){0,2}(?:тебя|вас)\b|'
    r'\b(?:сломаю|переломаю)\s+(?:тебе|вам)\s+(?:ноги|руки|шею)\b|'
    r'\b(?:сдохни|сдохните|убейся|выпились|повесься|сгори в аду)\b')
_REPORT_QUOTE = re.compile(
    r'\b(?:он|она|мне|модератор|пользователь|участник)\b[^\n.!?«“"\']{0,45}'
    r'\b(?:сказал\w*|написал\w*|ответил\w*|прислал\w*|угрожал\w*)\s*:?\s*'
    r'(?:«[^»]*»|“[^”]*”|"[^"\n]*"|\'[^\'\n]*\')')
# Фрагмент в кавычках. Границу держим короткой: незакрытая кавычка на абзац
# иначе съела бы полтекста.
_QUOTED = re.compile(r'«[^»]{1,200}»|“[^”]{1,200}”|"[^"\n]{1,200}"|\'[^\'\n]{1,200}\'')
# Require actual ownership of the quoted words, not just a nearby «я»:
# «я прочитал фразу…» and «я посмотрел сцену…» still quote someone else.
_SELF_ATTRIBUTION = re.compile(
    r'\b(?:я|мы)(?:\s*:\s*|\s+(?:тебе|вам)\s*:?\s*|\s+'
    r'(?:(?:тебе|вам)\s+)?(?:говорю|говорим|скажу|скажем|сказал[аи]?|'
    r'пишу|пишем|напишу|напишем|написал[аи]?|отвечаю|ответил[аи]?|'
    r'желаю|советую|кричу)(?:\s+(?:тебе|вам))?\s*:?\s*)$')


def _strip_foreign_quotes(text: str) -> str:
    """Убирает из текста кавычки с чужой речью, оставляя свою.

    Процитированное принадлежит автору, только если он прямо приписал
    слова себе. Два исключения из «чужого»:
    сообщение целиком в кавычках (это оформление своих слов, а не пересказ)
    и фрагмент, перед которым автор назвал себя говорящим.
    """
    whole = text.strip()
    out, pos, previous = [], 0, 0
    for match in _QUOTED.finditer(text):
        # Контекст берём от конца прошлой кавычки: «я» внутри предыдущей
        # цитаты — это чужое «я», и присваивать по нему нельзя.
        before = text[max(previous, match.start() - 40):match.start()]
        mine = (match.group().strip() == whole
                or _SELF_ATTRIBUTION.search(before))
        if not mine:
            out.append(text[pos:match.start()])
            out.append(' ')
            pos = match.end()
        previous = match.end()
    out.append(text[pos:])
    return ''.join(out)
_PRIORITY = {'family': 100, 'doxxing': 100, 'scam': 100, 'raid': 100,
             'politics': 95, 'nsfw': 95, 'toxic_admin': 90,
             'aggression': 70, 'toxic': 60, 'belittling': 10}


# Спам-боту отвечают так, как он заслужил: «шлюхобот пошел нахуй». Это
# реакция на спам, а не агрессия к участнику — человек, которого разозлил
# бот, предупреждения не заслужил. Встретилось на живых комментариях.
_BOT_ADDRESSEE = r'(?:шлюхобот\w*|спамбот\w*|спам-бот\w*|порнобот\w*|бот(?:ы|яра)?|спамер\w*)'
# «Заткнись и возьми мои деньги» — мем-восторг, а не приказ собеседнику.
_SHUT_UP_MEME = re.compile(r'\s+и\s+(?:возьми|бери|забери|забирай)\s+(?:мои|моих)\s+деньг')


def _harmless_dismissal(text: str, match: re.Match) -> bool:
    """Грубость, обращённая не к человеку: к спам-боту или в меме."""
    if match.group().startswith('заткнись') and _SHUT_UP_MEME.match(text[match.end():]):
        return True
    # «Ты бот, пошел нахуй» обзывает ботом человека — это уже ему.
    if re.search(r'\b(?:ты|вы)\b', text):
        return False
    before = text[max(0, match.start() - 30):match.start()]
    after = text[match.end():match.end() + 30]
    return bool(re.search(rf'\b{_BOT_ADDRESSEE}[\s,!.]*$', before)
                or re.match(rf'[\s,!.]*{_BOT_ADDRESSEE}\b', after))


# Пересказ реплики персонажа без кавычек: «он сказал ей: я тебя убью»,
# «а потом он такой: убью тебя», «дьявол сказал: сгори в аду». За пересказ
# сюжета выдавался мут. Говорящий — третье лицо: «я сказал: убью» остаётся
# своей речью.
_REPORT_COLON = re.compile(
    r'\b(?:сказал|сказала|сказали|говорит|говорил\w*|кричит|крикнул\w*|кричал\w*|'
    r'ответил\w*|заявил\w*|орет|орал\w*|такой|такая|такие)'
    r'(?:\s+(?:ей|ему|им|мне|всем))?\s*:\s*[^\n]*')


def _strip_reported_colon(text: str) -> str:
    def cut(match):
        before = text[max(0, match.start() - 30):match.start()]
        return match.group() if re.search(r'\b(?:я|мы)\s+(?:\w+\s+)?$', before) else ' '
    return _REPORT_COLON.sub(cut, text)


def _direct_speech(text: str) -> str:
    """Remove attributed reports/quotes, not arbitrary quoted evasion."""
    return _strip_reported_colon(_REPORT_QUOTE.sub(' ', text))


def _negated(text: str, match: re.Match) -> bool:
    prefix = text[max(0, match.start() - 40):match.start()]
    return bool(re.search(
        r'\b(?:не|нельзя|никогда не|никому не|запрещено)\s*'
        r'(?:(?:писать|пиши(?:те)?|говорить|говори(?:те)?|кричать|кричи(?:те)?)\s+)?$',
        prefix))


def _forbidden_infinitive(text: str, match: re.Match) -> bool:
    """'Заспамить чат запрещено' is a rule, not a call to spam."""
    return match.group().endswith('ить') and bool(re.match(
        r'[^,;.!?]{0,40}\b(?:запрещено|нельзя)\b', text[match.end():]))


_LAUGHTER = re.compile(r'(?:ах){2,}|(?:ха){2,}|(?:хе){2,}|\bхд\b|\bxd\b|[😂🤣😆😹]|\){2,}')


def _conditional_game_joke(text: str, threat: re.Match) -> bool:
    """Keep the chat's spoiler/raid banter exception narrow, not all 'if' threats."""
    if 'убью' not in threat.group():
        return False
    if re.search(r'\b(?:нож\w*|зарежу|адрес|деньги|переведешь|в реале|у дома)\b', text):
        return False
    after = text[threat.end():]
    # «Убью тебя за такие спойлеры 😂», «я тебя сейчас убью ахаха» — шутка
    # в споре о спойлерах, за которую выдавался мут. Нож, адрес и «в реале»
    # выше по-прежнему делают угрозу угрозой.
    if re.match(r'[\s,:—-]*за\s+(?:так\w*\s+|эт\w*\s+)?спойлер\w*', after) or _LAUGHTER.search(text):
        return True
    return bool(re.match(
        r'[\s,:—-]*(?:если\s+(?:ты\s+)?(?:заспойлер\w*|спойлер\w*|расскажешь концовку)|'
        r'когда\s+(?:мы\s+)?встретимся\s+в\s+(?:рейде|игре|данже))\b',
        after))


def _admin_interjection(text: str, match: re.Match) -> bool:
    """«Админ, сука, спасибо за перевод!» и «админ, говно серия» — не про админа.

    Обращение, потом «сука» между запятыми — восклицание; «говно» перед
    существительным — оценка серии. За обе фразы был мут на час как за
    оскорбление администратора. «Админ, ты сука» по-прежнему оскорбление.
    """
    found = match.group()
    if re.search(r'\b(?:ты|вы|сам|сами)\b', found):
        return False
    after = text[match.end():]
    if re.search(r',\s*(?:сука|суки)$', found) and re.match(r'\s*,\s*\w', after):
        return True
    return found.endswith('говно') and bool(re.match(r'\s+[а-яa-z]', after))


def _clauses(text: str):
    # Keep domains, IPs and address abbreviations intact.
    return re.split(r'[;!\n]+|(?<!ул)(?<!пер)(?<!д)(?<!кв)\.(?=\s)|\bно\b|\bзато\b', text)


def _private_details(text: str) -> bool:
    if _ADDRESS.search(text) or any(10 <= sum(c.isdigit() for c in m.group()) <= 15 for m in _PHONE.finditer(text)):
        return True
    for match in _IP.finditer(text):
        # «Слил билд 1.0.2.3» — номер версии, а не чей-то IP: за него был мут
        # на сутки как за раскрытие чужих данных.
        if _VERSION_BEFORE.search(text[max(0, match.start() - 20):match.start()]):
            continue
        try:
            ipaddress.IPv4Address(match.group())
            return True
        except ipaddress.AddressValueError:
            pass
    return False


def _rank(verdict: Verdict) -> tuple:
    return (verdict.confident, _PRIORITY[verdict.category], verdict.severity)


def _strongest(text: str, *, reply_to_user: bool, reply_to_admin: bool) -> Verdict | None:
    strongest = None
    for clause in _clauses(text):
        verdict = _check_clause(clause, reply_to_user=reply_to_user,
                               reply_to_admin=reply_to_admin)
        if verdict is not None and (strongest is None or _rank(verdict) > _rank(strongest)):
            strongest = verdict
    return strongest


def check_text(text: str, *, reply_to_user: bool = False,
               reply_to_admin: bool = False) -> Verdict | None:
    """Only clear breaches are automatic; mild banter remains possible."""
    direct = _direct_speech(normalize(text))
    strongest = _strongest(direct, reply_to_user=reply_to_user,
                           reply_to_admin=reply_to_admin)
    if strongest is None:
        return None
    outside_text = _strip_foreign_quotes(direct)
    if outside_text == direct:
        return strongest
    # Нарушение нашлось, но пропадает вместе с чужими кавычками — значит оно
    # жило в чужой речи. Аниме-чат пересказывает сюжеты («он кричит "сдохни!"»),
    # а модератор цитирует правило («за "заткнись" будет предупреждение») — за
    # это выдавался мут. Список глаголов пересказа латать бесполезно: их сотни,
    # поэтому смотрим не на глагол, а на то, претендует ли автор на слова.
    outside = _strongest(outside_text, reply_to_user=reply_to_user,
                         reply_to_admin=reply_to_admin)
    # Своя речь нарушает — судим по ней, и уверенно: цитата рядом не смягчает
    # собственное оскорбление, даже если сама была тяжелее.
    if outside is not None:
        return outside
    # Своего нарушения нет. Вердикт не исчезает — кавычки не должны стать
    # лазейкой, — но перестаёт быть уверенным: сообщение уходит на второй
    # уровень, а не в наказание.
    return replace(strongest, confident=False)


def _check_clause(value: str, *, reply_to_user: bool,
                  reply_to_admin: bool) -> Verdict | None:
    # Paths and query parameters are identifiers, not the author's speech.
    # Keep the original value for domain and scam-destination checks.
    direct = re.sub(_URL, ' ', value, flags=re.IGNORECASE)
    uncertain = None
    family = re.search(rf'\b{_TARGET_FAMILY}\b{_FAMILY_BRIDGE}{_INSULT}\b|\b{_INSULT}\b{_FAMILY_BRIDGE_TIGHT}{_TARGET_FAMILY}\b', direct)
    if family and _FAMILY_IDIOM.search(family.group()):
        family = None
    # Чья мать — решает всё: «ебал твою мать» оскорбляет собеседника, а «он
    # трахал сестру всю мангу» пересказывает сюжет. Без «твою/вашу» фраза
    # о чужой семье, и правила чата сюжет не запрещают.
    sexual_family = re.search(
        r'\b(?:ебал|выебал|трахал|трахну|выебу)\s+(?:(?:твою|вашу)\s+(?:мать|маму|сестру)|'
        r'(?:мать|маму|сестру)\s+(?:твою|вашу))\b|'
        r'\b(?:сын|дочь)\s+(?:шлюхи|проститутки)\b', direct)
    if family and re.search(r'\b(?:не|неправда)\b', family.group()):
        family = None
    if sexual_family and _negated(direct, sexual_family):
        sexual_family = None
    if family or sexual_family:
        return Verdict('family', 'Оскорбление семьи собеседника', 3)
    if re.search(r'https?://(?:www\.)?(?:pornhub\.com|xvideos\.com|xnxx\.com|xhamster\.com)(?:/|\b)', value, re.IGNORECASE):
        return Verdict('nsfw', 'Ссылка на порнографический сайт', 3)
    politics = list(_POLITICS.finditer(direct))
    if politics:
        # «путина» — это и фамилия в косвенном падеже, и рыболовный сезон;
        # различить их нельзя, регистр к этому месту уже снят. Категория
        # удаляет сообщение, поэтому на одной лишь двусмысленной форме бот
        # сообщение не трогает, а зовёт человека — как и всюду, где сомнение.
        certain = any(m.group().lower() not in _POLITICS_AMBIGUOUS
                      or re.search(r'\bголосуйте за\s*$', direct[:m.start()])
                      for m in politics)
        verdict = Verdict('politics', 'Обсуждение реальной политики', confident=certain)
        if certain:
            return verdict
        uncertain = verdict
    # Personal contact details are not doxxing without a target/disclosure cue.
    disclosure = _DOX_INTENT.search(direct)
    if (disclosure and not _negated(direct, disclosure) and _private_details(direct)
            and not re.search(r'\b(?:не сливайте|не публикуйте|не присылайте|нельзя публиковать)\b', direct)):
        return Verdict('doxxing', 'Раскрытие чужих контактных данных', 3)
    solicitation = list(re.finditer(r'\b(?:пришли|пришлите|отправь|отправьте|введи|введите|скинь|скиньте|сообщи|сообщите|перешли|перешлите)\b', direct))
    secrets = re.search(r'\b(?:код (?:из|от|для входа в) (?:смс|sms|телеграм\w*|telegram)|пароль|seed(?:[ -]phrase)?|сид[ -]?фраз\w*|секретн\w* ключ|приватн\w* ключ)\b', direct)
    # «получи\w* бесплатно» ловило «получится бесплатно» и «получилось
    # бесплатно» — обычную фразу чата, за которую сообщение удалялось. Обещание
    # обращено ко ВТОРОМУ лицу: скам предлагает выгоду тебе. Безличные формы
    # ничего не предлагают, поэтому список окончаний закрыт.
    profit = list(re.finditer(r'\b(?:удвою|удвоим|гарантированн\w* доход|без риска|'
                              r'получи(?:те|шь)?\s+бесплатно|заработок без вложений)\b', direct))
    payment = re.search(r'\b(?:переведи|переведите|оплати|оплатите|предоплат\w*|кошелек|крипт\w*)\b', direct)
    active_requests = [item for item in solicitation if not _negated(direct, item)]
    profit = [item for item in profit
              if item.group() != 'без риска' or _MONEY_CONTEXT.search(direct)]
    # Пароль от вайфая просят у друзей, а не у жертвы.
    if secrets and re.search(r'пароль\s+от\s+(?:вай-?фа\w*|wi-?fi|вифи|роутер\w*|сети)\b', direct):
        secrets = None
    profit_request = (any(not _negated(direct, item) for item in profit)
                      and (_LINK.search(value) or payment))
    if (secrets and active_requests) or profit_request:
        # Entering a password can be ordinary login help, including a link
        # to the service. Requesting disclosure is a different action.
        clear_request = any(item.group() not in ('введи', 'введите')
                            for item in active_requests)
        verdict = Verdict('scam', 'Запрос секретов или обещание гарантированного заработка',
                          3, confident=bool(profit_request or clear_request))
        if verdict.confident:
            return verdict
        uncertain = verdict
    raids = re.finditer(r'\b(?:рейдим|рейдить|зарейдим|заспамим|заспамить|флудим|спамим|атакуем|набег|завалим спамом)\b', direct)
    destination = _LINK.search(value) or re.search(r'\b(?:чужой чат|их чат|этот чат|канал|группу)\b', direct)
    # «Атакуем» и «набег» — обычные слова сюжета: «атакуем группу разведчиков»
    # — это «Атака титанов», а не рейд. Им нужна цель, которая точно чат:
    # ссылка или «их/этот/чужой» чат, канал, группа.
    chat_target = _LINK.search(value) or re.search(
        r'\b(?:(?:чужой|их|этот|тот|вражеский)\s+чат|'
        r'(?:их|этот|тот|чужой|чужую|эту|ту|вражеск\w*)\s+(?:канал|группу))\b', direct)
    game = re.search(r'\b(?:игровой рейд|босс\w*|подземел\w*|данж\w*|гильди\w*|wow|варкрафт)\b', direct)
    explicit_spam = re.search(r'\b(?:заспам\w*|флудим|спамим|завалим спамом)\b', direct)
    # «Заспамим лайками канал студии», «флудим этот чат стикерами в честь
    # финала» — фанатская акция, а не атака: мут на сутки за такое — ошибка.
    # «Рейдим» сюда не относится: рейд со стикерами — всё ещё рейд.
    raids = [item for item in raids if not (
        item.group() in ('заспамим', 'заспамить', 'флудим', 'спамим') and not _LINK.search(value)
        and re.match(r'(?:\s+\w+){0,3}?\s+(?:лайк\w*|реакци\w*|голос\w*|сердечк\w*|стикер\w*|'
                     r'эмодзи|смайл\w*|комментари\w*)\b', direct[item.end():]))]
    if destination and (not game or explicit_spam) and any(
            not _negated(direct, item) and not _forbidden_infinitive(direct, item)
            and (chat_target or item.group() not in ('атакуем', 'набег'))
            for item in raids):
        return Verdict('raid', 'Призыв к атаке на чат или канал', 3)
    if any(not _negated(direct, threat) and not _conditional_game_joke(direct, threat)
           for threat in _THREAT.finditer(direct)):
        return Verdict('aggression', 'Прямая угроза или пожелание смерти', 3)
    direct_admin = any(not re.search(r'\bне\b', item.group()) and not _admin_interjection(direct, item)
                       for item in re.finditer(
        rf'\b(?:админ\w*|модератор\w*)\W+(?:(?:ты|вы|сам|сами|реально|просто|настоящ\w*|полн\w*|кончен\w*|туп\w*)\W+){{0,3}}{_INSULT}\b|'
        rf'\b{_INSULT}\W+(?:админ\w*|модератор\w*)', direct))
    # Bind each insult to its recipient; self-irony elsewhere in the message
    # must neither trigger moderation nor exempt an actual targeted insult.
    admin_reply = reply_to_admin and (
        re.search(rf'\b(?:ты|вы|тебе|вам)\W+(?:(?:реально|просто|настоящ\w*|полн\w*|кончен\w*|туп\w*)\W+){{0,2}}{_INSULT}\b', direct)
        or re.fullmatch(rf'\W*(?:ну\W+)?{_INSULT}\W*', direct))
    if direct_admin or admin_reply:
        return Verdict('toxic_admin', 'Оскорбление администратора', 3)
    if re.search(rf'\bты\s+(?:кончен\w*|ебан\w*|туп\w*)\s+{_INSULT}\b', direct):
        return Verdict('toxic', 'Явное личное оскорбление')
    # «Уебок» и «долбаеб» через «а» раньше проходили мимо.
    if re.search(r'\b(?:ты|вы)\s+(?:мразь|тварь|хуесос\w*|уеб(?:ище|ок|ан)\w*|долб[оа]еб\w*)\b', direct):
        return Verdict('toxic', 'Грубое личное оскорбление')
    # One mild insult can be a joke; repeated personal put-downs are tracked.
    # «Иди нахуй» и женский род раньше проходили вовсе без реакции, а
    # «пошел нахуй» получал предупреждение — на живых ответах было три таких.
    dismissals = re.finditer(r'\b(?:заткнись|завали (?:рот|ебало)|'
                             r'(?:пошел|пошла|пошли|иди|идите|вали|валите)\s+(?:на хуй|нахуй))\b', direct)
    if any(not _negated(direct, item) and not _harmless_dismissal(direct, item)
           for item in dismissals):
        return Verdict('aggression', 'Агрессивное обращение к собеседнику')
    if (reply_to_user or re.search(r'\bты\b', direct)) and re.search(
            r'\b(?:тебя не спрашивали|твое мнение никому не нужно|ты никто|ты ничего не понимаешь|с тобой все ясно)\b', direct):
        return Verdict('belittling', 'Повторяемое принижение собеседника', 1)
    # Uncertain context must not hide an independent, explicit violation.
    return uncertain
