"""Conservative, offline chat rules. Uncertain context is left for review."""
from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Verdict:
    category: str
    reason: str
    severity: int = 2
    confident: bool = True

    def as_dict(self):
        return vars(self).copy()


_HOMOGLYPHS = str.maketrans('aceopxykmth', 'асеорхукмтн')
_SEPARATED = re.compile(r'(?<!\w)(?:[а-яa-z][.\-_*|\s]){3,}[а-яa-z](?!\w)')


def normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', str(text or '')).casefold().replace('ё', 'е')
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    # Translate lookalikes only inside Cyrillic words; preserve URLs/English.
    def fold(match):
        word = match.group()
        if re.search('[а-я]', word):
            return word.translate(_HOMOGLYPHS).replace('0', 'о')
        return word
    text = re.sub(r'[\w]+', fold, text)
    text = _SEPARATED.sub(lambda m: re.sub(r'[.\-_*|\s]', '', m.group()), text)
    return re.sub(r'\s+', ' ', text).strip()


_INSULT = r'(?:шлюх\w*|проститут\w*|твар\w*|мраз\w*|уеб\w*|ебан\w*|долбоеб\w*|пидор\w*|сука|суки|сучк\w*|говно|хуесос\w*|дебил\w*|идиот\w*)'
_FAMILY = r'(?:мать|матер[ьиью]|мам[ауыое]|мамк\w*|мамаш\w*|отец|отц\w*|пап[ауыое]|бат[яюе]|сестр\w*|брат\w*|родител\w*|семь\w*)'
_YOUR = r'(?:тво\w*|ваш\w*)'
_TARGET_FAMILY = rf'(?:{_YOUR}\s+{_FAMILY}|{_FAMILY}\s+{_YOUR})'
_LINK = re.compile(r'https?://\S+|t\.me/\S+|discord\.gg/\S+|@[a-z0-9_]{5,}', re.I)
_POLITICS = re.compile(
    r'\b(?:путин\w*|зеленск\w*|трамп\w*|байден\w*|навальн\w*|лукашенко|'
    r'нато|сво|слава украине|героям слава|голосуйте за|единая россия|'
    r'выборы президента|война (?:в|на|с) (?:украин\w*|росси\w*)|'
    r'putin|zelensky\w*|trump|biden|nato)\b')
_PHONE = re.compile(r'(?<!\d)\+?\d[\d ()\-]{8,20}\d(?!\d)')
_IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_ADDRESS = re.compile(r'\b(?:улица|ул\.|проспект|пр-т)\s+[а-яa-z][\w -]{1,40}[, ]+\d{1,4}\b')
_DOX_INTENT = re.compile(r'\b(?:сливаю|слейте|деанон\w*|докс\w*|его номер|ее номер|его адрес|ее адрес|вот (?:номер|адрес)\s+@\w+)\b')
_THREAT = re.compile(
    r'\b(?:я\s+)?(?:тебя|вас)\s+(?:убью|зарежу|изобью|найду и убью)|'
    r'\b(?:убью|зарежу|изобью)\s+(?:тебя|вас)\b|'
    r'\b(?:сдохни|сдохните|убейся|выпились|повесься)\b')


def _direct_speech(text: str) -> str:
    """Remove attributed reports/quotes, not arbitrary quoted evasion."""
    if re.search(r'\b(?:он|она|мне|модератор|пользователь)\b.{0,30}\b(?:сказал\w*|написал\w*|ответил\w*)\s*:', text):
        return re.sub(r'[«“"].+?[»”"]', '', text)
    return text


def check_text(text: str, *, reply_to_user: bool = False,
               reply_to_admin: bool = False) -> Verdict | None:
    """Only clear breaches are automatic; mild banter remains possible."""
    value = normalize(text)
    direct = _direct_speech(value)
    family = re.search(rf'{_TARGET_FAMILY}.{{0,25}}{_INSULT}|{_INSULT}.{{0,25}}{_TARGET_FAMILY}', direct)
    sexual_family = re.search(
        r'\b(?:ебал|выебал|трахал)\s+(?:(?:твою|вашу)\s+)?(?:мать|маму|сестру)\b|'
        r'\b(?:сын|дочь)\s+(?:шлюхи|проститутки)\b', direct)
    if family and re.search(r'\b(?:не|неправда)\b', family.group()):
        family = None
    if sexual_family and re.search(r'\bне\s+$', direct[:sexual_family.start()]):
        sexual_family = None
    if family or sexual_family:
        return Verdict('family', 'Оскорбление семьи собеседника', 3)
    if re.search(r'https?://(?:www\.)?(?:pornhub\.com|xvideos\.com|xnxx\.com|xhamster\.com)(?:/|\b)', value):
        return Verdict('nsfw', 'Ссылка на порнографический сайт', 3)
    if _POLITICS.search(value):
        return Verdict('politics', 'Обсуждение реальной политики')
    # Personal contact details are not doxxing without a target/disclosure cue.
    if _DOX_INTENT.search(direct) and (_PHONE.search(value) or _IP.search(value) or _ADDRESS.search(value)):
        if not re.search(r'\b(?:не сливайте|не публикуйте|не присылайте|нельзя публиковать)\b', direct):
            return Verdict('doxxing', 'Раскрытие чужих контактных данных', 3)
    solicitation = re.search(r'\b(?:пришли|пришлите|отправь|отправьте|введи|введите|скинь|скиньте|сообщи|сообщите)\b', direct)
    secrets = re.search(r'\b(?:код из (?:смс|sms|телеграм\w*)|пароль|seed|сид[ -]?фраз\w*|секретн\w* ключ)\b', direct)
    safety = re.search(r'\b(?:никому|никогда|не (?:отправляй|присылай|вводи|сообщай))\b', direct)
    profit = re.search(r'\b(?:удвою|удвоим|гарантированн\w* доход|без риска|получи\w* бесплатно|заработок без вложений)\b', direct)
    payment = re.search(r'\b(?:переведи|переведите|оплати|оплатите|предоплат\w*|кошелек|крипт\w*)\b', direct)
    if not safety and ((solicitation and secrets) or (profit and (_LINK.search(value) or payment))):
        return Verdict('scam', 'Запрос секретов или обещание гарантированного заработка', 3)
    raid = re.search(r'\b(?:рейдим|рейдить|зарейдим|заспамим|заспамить|флудим|спамим|атакуем|набег)\b', direct)
    if raid and (_LINK.search(value) or re.search(r'\b(?:чужой чат|их чат|этот чат|канал|группу)\b', direct)):
        if not re.search(r'\b(?:не спамим|не флудим|не рейдим|запрещен\w*|нельзя)\b', direct):
            return Verdict('raid', 'Призыв к атаке на чат или канал', 3)
    if _THREAT.search(direct):
        threat = _THREAT.search(direct)
        if not re.search(r'\bне\s+$', direct[:threat.start()]):
            return Verdict('aggression', 'Прямая угроза или пожелание смерти', 3)
    insult = re.search(_INSULT, direct)
    addressed_admin = reply_to_admin or bool(re.search(r'\b(?:админ\w*|модератор\w*)\b', direct))
    if insult and addressed_admin and (reply_to_admin or re.search(rf'\b(?:админ\w*|модератор\w*)\W+(?:\w+\W+){{0,2}}{_INSULT}|{_INSULT}\W+(?:админ\w*|модератор\w*)', direct)):
        return Verdict('toxic_admin', 'Оскорбление администратора', 3)
    if re.search(rf'\bты\s+(?:кончен\w*|ебан\w*|туп\w*)\s+{_INSULT}\b', direct):
        return Verdict('toxic', 'Явное личное оскорбление')
    # One mild insult can be a joke; repeated personal put-downs are tracked.
    if re.search(r'\b(?:заткнись|завали (?:рот|ебало)|пошел на хуй|пошел нахуй)\b', direct):
        return Verdict('aggression', 'Агрессивное обращение к собеседнику')
    if (reply_to_user or re.search(r'\bты\b', direct)) and re.search(
            r'\b(?:тебя не спрашивали|твое мнение никому не нужно|ты никто|ты ничего не понимаешь|с тобой все ясно)\b', direct):
        return Verdict('belittling', 'Повторяемое принижение собеседника', 1)
    return None
