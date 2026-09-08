"""Conservative, offline chat rules. Uncertain context is left for review."""
import ipaddress
import re
import unicodedata
from dataclasses import dataclass


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
_MASKED_WORD = re.compile(
    r'(?:шлюх\w*|проститут\w*|мраз\w*|долбоеб\w*|хуесос\w*|пидор\w*|'
    r'уеб\w*|ебан\w*|ебал\w*|сука|суки|сучк\w*|дебил\w*|идиот\w*|'
    r'мать|матери|мам[ауыое]|мамк\w*|тво[яюейих]+|сдохни\w*|убейся|'
    r'убью|зарежу|повесься|путин\w*|зеленск\w*|нато|сво)\Z')
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
        part = ''.join(c for c in part if unicodedata.category(c) != 'Cf')

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


_INSULT = r'(?:шлюх\w*|проститут\w*|твар(?:ь|и|ей|ям|ью|ями|ях)|мраз\w*|уеб\w*|ебан\w*|долбоеб\w*|пидор\w*|сука|суки|сучк\w*|говно|хуесос\w*|дебил\w*|идиот\w*)'
_FAMILY = r'(?:мать|матер[ьиьюям]+|мам[ауыое]|мамк\w*|мамаш\w*|отец|отц\w*|пап[ауыое]|бат[яюеи]|сестр\w*|брат\w*|родител\w*|семь\w*|семе[йью]+)'
_YOUR = r'(?:тво(?:я|е|ю|и|й|его|ей|ему|им|их|ими|ем)|ваш(?:а|е|у|и|его|ей|ему|им|их|ими|ем)?)'
_TARGET_FAMILY = rf'(?:{_YOUR}\s+(?:вся\s+|все\s+|всю\s+)?{_FAMILY}|{_FAMILY}\s+{_YOUR})'
_FAMILY_BRIDGE = r'(?:[\s,—–:]+(?:это|просто|еще|та|такие|все|сам\w*|настоящ\w*|кончен\w*|ебан\w*|туп\w*|полн\w*|сборищ\w*|назову|считаю)\b){0,4}[\s,—–:]+'
_LINK = re.compile(r'https?://\S+|t\.me/\S+|discord\.gg/\S+|@[a-z0-9_]{5,}', re.IGNORECASE)
_POLITICS = re.compile(
    r'\b(?:путин\w*|зеленск\w*|трамп\w*|байден\w*|навальн\w*|лукашенко|'
    r'нато|сво|слава украине|героям слава|голосуйте за|единая россия|'
    r'выборы президента|война (?:в|на|с) (?:украин\w*|росси\w*)|'
    r'putin|zelensky\w*|trump|biden|nato)\b')
_PHONE = re.compile(r'(?<!\d)\+?\d[\d ()\-]{8,20}\d(?!\d)')
_IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
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
_PRIORITY = {'family': 100, 'doxxing': 100, 'scam': 100, 'raid': 100,
             'politics': 95, 'nsfw': 95, 'toxic_admin': 90,
             'aggression': 70, 'toxic': 60, 'belittling': 10}


def _direct_speech(text: str) -> str:
    """Remove attributed reports/quotes, not arbitrary quoted evasion."""
    return _REPORT_QUOTE.sub(' ', text)


def _negated(text: str, match: re.Match) -> bool:
    prefix = text[max(0, match.start() - 40):match.start()]
    return bool(re.search(r'\b(?:не|нельзя|никогда не|никому не|запрещено)\s*$', prefix))


def _forbidden_infinitive(text: str, match: re.Match) -> bool:
    """'Заспамить чат запрещено' is a rule, not a call to spam."""
    return match.group().endswith('ить') and bool(re.match(
        r'[^,;.!?]{0,40}\b(?:запрещено|нельзя)\b', text[match.end():]))


def _conditional_game_joke(text: str, threat: re.Match) -> bool:
    """Keep the chat's spoiler/raid banter exception narrow, not all 'if' threats."""
    if 'убью' not in threat.group():
        return False
    if re.search(r'\b(?:нож\w*|зарежу|адрес|деньги|переведешь|в реале|у дома)\b', text):
        return False
    return bool(re.match(
        r'[\s,:—-]*(?:если\s+(?:ты\s+)?(?:заспойлер\w*|спойлер\w*|расскажешь концовку)|'
        r'когда\s+(?:мы\s+)?встретимся\s+в\s+(?:рейде|игре|данже))\b',
        text[threat.end():]))


def _clauses(text: str):
    # Keep domains, IPs and address abbreviations intact.
    return re.split(r'[;!\n]+|(?<!ул)(?<!пер)(?<!д)(?<!кв)\.(?=\s)|\bно\b|\bзато\b', text)


def _private_details(text: str) -> bool:
    if _ADDRESS.search(text) or any(10 <= sum(c.isdigit() for c in m.group()) <= 15 for m in _PHONE.finditer(text)):
        return True
    for candidate in _IP.findall(text):
        try:
            ipaddress.IPv4Address(candidate)
            return True
        except ipaddress.AddressValueError:
            pass
    return False


def check_text(text: str, *, reply_to_user: bool = False,
               reply_to_admin: bool = False) -> Verdict | None:
    """Only clear breaches are automatic; mild banter remains possible."""
    direct = _direct_speech(normalize(text))
    strongest = None
    for clause in _clauses(direct):
        verdict = _check_clause(clause, reply_to_user=reply_to_user,
                                reply_to_admin=reply_to_admin)
        if verdict is not None and (strongest is None or
                (_PRIORITY[verdict.category], verdict.severity) >
                (_PRIORITY[strongest.category], strongest.severity)):
            strongest = verdict
    return strongest


def _check_clause(value: str, *, reply_to_user: bool,
                  reply_to_admin: bool) -> Verdict | None:
    direct = value
    family = re.search(rf'\b{_TARGET_FAMILY}\b{_FAMILY_BRIDGE}{_INSULT}\b|\b{_INSULT}\b{_FAMILY_BRIDGE}{_TARGET_FAMILY}\b', direct)
    sexual_family = re.search(
        r'\b(?:ебал|выебал|трахал|трахну|выебу)\s+(?:(?:твою|вашу)\s+)?(?:мать|маму|сестру)\b|'
        r'\b(?:сын|дочь)\s+(?:шлюхи|проститутки)\b', direct)
    if family and re.search(r'\b(?:не|неправда)\b', family.group()):
        family = None
    if sexual_family and _negated(direct, sexual_family):
        sexual_family = None
    if family or sexual_family:
        return Verdict('family', 'Оскорбление семьи собеседника', 3)
    if re.search(r'https?://(?:www\.)?(?:pornhub\.com|xvideos\.com|xnxx\.com|xhamster\.com)(?:/|\b)', value, re.IGNORECASE):
        return Verdict('nsfw', 'Ссылка на порнографический сайт', 3)
    if _POLITICS.search(value):
        return Verdict('politics', 'Обсуждение реальной политики')
    # Personal contact details are not doxxing without a target/disclosure cue.
    disclosure = _DOX_INTENT.search(direct)
    if (disclosure and not _negated(direct, disclosure) and _private_details(value)
            and not re.search(r'\b(?:не сливайте|не публикуйте|не присылайте|нельзя публиковать)\b', direct)):
        return Verdict('doxxing', 'Раскрытие чужих контактных данных', 3)
    solicitation = list(re.finditer(r'\b(?:пришли|пришлите|отправь|отправьте|введи|введите|скинь|скиньте|сообщи|сообщите|перешли|перешлите)\b', direct))
    secrets = re.search(r'\b(?:код (?:из|от|для входа в) (?:смс|sms|телеграм\w*|telegram)|пароль|seed(?:[ -]phrase)?|сид[ -]?фраз\w*|секретн\w* ключ|приватн\w* ключ)\b', direct)
    profit = list(re.finditer(r'\b(?:удвою|удвоим|гарантированн\w* доход|без риска|получи\w* бесплатно|заработок без вложений)\b', direct))
    payment = re.search(r'\b(?:переведи|переведите|оплати|оплатите|предоплат\w*|кошелек|крипт\w*)\b', direct)
    if ((secrets and any(not _negated(direct, item) for item in solicitation)) or
            (any(not _negated(direct, item) for item in profit) and (_LINK.search(value) or payment))):
        return Verdict('scam', 'Запрос секретов или обещание гарантированного заработка', 3)
    raids = re.finditer(r'\b(?:рейдим|рейдить|зарейдим|заспамим|заспамить|флудим|спамим|атакуем|набег|завалим спамом)\b', direct)
    destination = _LINK.search(value) or re.search(r'\b(?:чужой чат|их чат|этот чат|канал|группу)\b', direct)
    game = re.search(r'\b(?:игровой рейд|босс\w*|подземел\w*|данж\w*|гильди\w*|wow|варкрафт)\b', direct)
    explicit_spam = re.search(r'\b(?:заспам\w*|флудим|спамим|завалим спамом)\b', direct)
    if destination and (not game or explicit_spam) and any(
            not _negated(direct, item) and not _forbidden_infinitive(direct, item)
            for item in raids):
        return Verdict('raid', 'Призыв к атаке на чат или канал', 3)
    if any(not _negated(direct, threat) and not _conditional_game_joke(direct, threat)
           for threat in _THREAT.finditer(direct)):
        return Verdict('aggression', 'Прямая угроза или пожелание смерти', 3)
    direct_admin = any(not re.search(r'\bне\b', item.group()) for item in re.finditer(
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
    if re.search(r'\b(?:ты|вы)\s+(?:мразь|тварь|хуесос\w*|уебище|долбоеб\w*)\b', direct):
        return Verdict('toxic', 'Грубое личное оскорбление')
    # One mild insult can be a joke; repeated personal put-downs are tracked.
    dismissals = re.finditer(r'\b(?:заткнись|завали (?:рот|ебало)|пошел на хуй|пошел нахуй)\b', direct)
    if any(not _negated(direct, item) for item in dismissals):
        return Verdict('aggression', 'Агрессивное обращение к собеседнику')
    if (reply_to_user or re.search(r'\bты\b', direct)) and re.search(
            r'\b(?:тебя не спрашивали|твое мнение никому не нужно|ты никто|ты ничего не понимаешь|с тобой все ясно)\b', direct):
        return Verdict('belittling', 'Повторяемое принижение собеседника', 1)
    return None
