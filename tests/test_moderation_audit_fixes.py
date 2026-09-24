"""Модерация: ошибки, найденные аудитом на живых комментариях и в симуляциях.

Каждый тест — случай, в котором бот наказывал невиновного, пропускал
нарушение или молча выключал проверку. Реплики пересказаны, живые сообщения
участников в репозиторий не попадают.
"""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

import anime_news_bot as bot
import moderation_media as media
import moderation_rules as rules

CHAT = -100


@pytest.fixture
def state(tmp_path, monkeypatch):
    bot._init_globals()
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(CHAT, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [1])
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports', '_moderation_report_bursts'):
        monkeypatch.setattr(bot, name, {})
    for name in ('_moderation_latest', '_moderation_album_verdicts'):
        monkeypatch.setattr(bot, name, bot.OrderedDict())
    return store


def message(number=1, text='обычная реплика', uid=7, **kw):
    row = dict(chat_id=CHAT, message_id=number, from_user=NS(id=uid, full_name=f'U{uid}', is_bot=False),
               text=text, caption=None, sender_chat=None, reply_to_message=None, media_group_id=None,
               is_automatic_forward=False, entities=())
    row.update(kw)
    return NS(**row)


def telegram(status='member'):
    return NS(get_chat_member=AsyncMock(return_value=NS(status=status, until_date=None)),
              delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(return_value=True),
              ban_chat_member=AsyncMock(), send_message=AsyncMock(return_value=NS(message_id=900)),
              edit_message_text=AsyncMock())


async def handle(tg, msg):
    await bot.moderation_message_handler(
        NS(effective_message=msg, effective_user=msg.from_user, effective_chat=NS(id=CHAT, type='supergroup'),
           edited_message=None), NS(bot=tg))


def admin_dms(tg):
    return [call for call in tg.send_message.call_args_list if call.args[0] == 1]


# ---------- детектор медиа не выключается чужими файлами ----------

def _doc(name, mime):
    return NS(sticker=None, animation=None, video=None, video_note=None, photo=None,
              document=NS(file_id=name, file_unique_id=name, file_name=name, mime_type=mime, file_size=60))


@pytest.mark.parametrize(('name', 'mime'), [
    ('ep01.srt', 'application/x-subrip'), ('ep01.ass', 'application/octet-stream'),
    ('notes.txt', 'text/plain'), ('scan.pdf', 'application/pdf'), ('pack.zip', 'application/zip'),
    ('README', 'text/plain'),                      # текст без расширения
])
def test_subtitles_and_documents_are_not_media(name, mime):
    """Три файла субтитров подряд ставили детектор на паузу на 10 минут."""
    assert media.media_attachment(_doc(name, mime)) is None


@pytest.mark.parametrize(('name', 'mime', 'kind'), [
    ('art.png', 'image/png', 'image'), ('clip.mp4', 'video/mp4', 'video'),
    ('file.bin', '', 'unknown'),                   # без имени и типа — вскрываем в воркере
    ('ep01.srt', 'image/jpeg', 'image'),           # MIME картинки важнее имени
])
def test_real_media_documents_are_still_checked(name, mime, kind):
    assert media.media_attachment(_doc(name, mime))[1] == kind


def test_bad_files_do_not_pause_the_detector():
    scanner = media.MediaScanner()
    for _ in range(scanner.failure_limit + 1):
        scanner._note_worker_result(media.Scan('unchecked', reason='Файл не декодируется: OSError',
                                               bad_input=True))
    assert scanner.paused_for() == 0
    for _ in range(scanner.failure_limit):
        scanner._note_worker_result(media.Scan('unchecked', reason='Детектор убит (SIGKILL)'))
    assert scanner.paused_for() > 0


def test_broken_image_is_bad_input_not_detector_failure(tmp_path, monkeypatch):
    from PIL import Image
    whole = tmp_path / 'whole.png'
    Image.new('RGB', (64, 64), 'red').save(whole)
    broken = tmp_path / 'broken.png'
    broken.write_bytes(whole.read_bytes()[:60])            # заголовок цел, данных нет
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    monkeypatch.setattr(media, 'scan_frame', lambda *a, **k: (_ for _ in ()).throw(AssertionError('не дошли')))
    result = media.scan_file(broken, 'image')
    assert result.status == 'unchecked' and result.bad_input and 'не декодируется' in result.reason


def test_text_file_renamed_as_media_is_not_a_failure(tmp_path, monkeypatch):
    path = tmp_path / 'content'
    path.write_bytes(b'1\n00:00:01,000 --> 00:00:02,000\nHello\n')
    monkeypatch.setattr(media, 'build_detector', lambda: object())
    result = media.scan_file(path, 'unknown')
    assert result.bad_input and result.review is False


# ---------- свой канал ----------

@pytest.mark.asyncio
async def test_channel_post_forwarded_into_the_discussion_is_not_moderated(state):
    """Пост канала про «Трампа» удалялся вместе с веткой комментариев."""
    tg = telegram()
    channel = NS(id=-1001234, title='Канал', username='ourchannel')
    await handle(tg, message(text='Трамп похвалил новое аниме', uid=777000, sender_chat=channel,
                             is_automatic_forward=True))
    tg.delete_message.assert_not_awaited()
    assert not tg.send_message.await_count


@pytest.mark.asyncio
async def test_post_signed_by_our_channel_is_not_moderated(state, monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001234)
    tg = telegram()
    await handle(tg, message(text='Трамп похвалил новое аниме', uid=136817688,
                             sender_chat=NS(id=-1001234, title='Канал', username='')))
    tg.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_channels_are_still_moderated(state, monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001234)
    tg = telegram()
    await handle(tg, message(text='Трамп похвалил новое аниме', uid=136817688,
                             sender_chat=NS(id=-1009999, title='Чужой', username='other')))
    tg.delete_message.assert_awaited_once()


@pytest.mark.parametrize(('channel_id', 'sender', 'own'), [
    ('@OurChannel', NS(id=-1, username='ourchannel'), True),
    ('@OurChannel', NS(id=-1, username='other'), False),
    (-1001234, NS(id=-1001234, username=''), True),
    (-1001234, None, False),
])
def test_own_channel_by_id_or_username(monkeypatch, channel_id, sender, own):
    monkeypatch.setattr(bot, 'CHANNEL_ID', channel_id)
    assert bot._mod_is_own_channel(sender) is own


# ---------- реклама удаляется, флуд — одно нарушение ----------

@pytest.mark.asyncio
async def test_promo_with_invite_is_deleted(state):
    tg = telegram()
    await handle(tg, message(text='вступай в наш чат https://t.me/+AbCdEfGh', uid=55))
    tg.delete_message.assert_awaited_once_with(CHAT, 1)


@pytest.mark.asyncio
async def test_repeated_link_is_deleted_but_repeated_sticker_is_not(state, monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_REPEAT_LIMIT', 3)
    monkeypatch.setattr(bot, 'MODERATION_FLOOD_MESSAGES', 20)
    tg = telegram()
    for number in range(1, 4):
        await handle(tg, message(number, text='смотри https://example.org/page', uid=56))
    assert tg.delete_message.await_count == 1
    assert state.warn_count(CHAT, 56) == 1


@pytest.mark.asyncio
async def test_repeated_sticker_is_warned_not_deleted(state, monkeypatch):
    """Три одинаковых стикера — предупреждение без удаления (решение PR #59)."""
    monkeypatch.setattr(bot, 'MODERATION_REPEAT_LIMIT', 3)
    monkeypatch.setattr(bot, 'MODERATION_FLOOD_MESSAGES', 20)
    tg = telegram()
    for number in range(1, 4):
        await handle(tg, message(number, text=None, uid=57, sticker=NS(file_unique_id='same', emoji='🙂')))
    tg.delete_message.assert_not_awaited()
    assert state.warn_count(CHAT, 57) == 1


@pytest.mark.asyncio
async def test_one_flood_burst_is_one_warning(state):
    """11 сообщений за секунду давали предупреждение, мут на час и мут на сутки."""
    tg = telegram()
    await asyncio.gather(*(handle(tg, message(n, text=f'реплика номер {n}')) for n in range(1, 12)))
    assert state.warn_count(CHAT, 7) == 1
    tg.restrict_chat_member.assert_not_awaited()


# ---------- налёт не превращается в шестьдесят писем ----------

@pytest.mark.asyncio
async def test_raid_reports_are_capped_and_quiet(state):
    tg = telegram()
    for uid in range(100, 110):
        await handle(tg, message(uid, text='вступай в наш чат https://t.me/+AbCdEfGh', uid=uid))
    dms = admin_dms(tg)
    assert len(dms) == bot.MODERATION_REPORT_BURST_MAX
    assert [call.kwargs.get('disable_notification') for call in dms] == [False, True, True]
    assert 'Похоже на налёт' in dms[-1].args[1]
    assert tg.delete_message.await_count == 10              # удаляется всё, молчат только письма


@pytest.mark.asyncio
async def test_hidden_reports_are_counted_in_the_next_window(state, monkeypatch):
    tg = telegram()
    for uid in range(100, 105):
        await handle(tg, message(uid, text='вступай в наш чат https://t.me/+AbCdEfGh', uid=uid))
    key = (CHAT, 'spam')
    started, sent, hidden = bot._moderation_report_bursts[key]
    assert hidden == 2
    bot._moderation_report_bursts[key] = (started - bot.MODERATION_REPORT_BURST_SEC - 1, sent, hidden)
    await handle(tg, message(200, text='вступай в наш чат https://t.me/+AbCdEfGh', uid=200))
    assert 'без писем: 2 похожих' in admin_dms(tg)[-1].args[1]


# ---------- проверка по альбому ----------

@pytest.mark.asyncio
async def test_nsfw_album_is_deleted_entirely(state, monkeypatch):
    """Половина альбома упиралась в очередь детектора и оставалась в чате."""
    answers = iter([media.Scan('unchecked', reason='Очередь локальной проверки переполнена')] * 2
                   + [media.Scan('checked', 'nsfw', 'нагота', 1, .97)])
    monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(side_effect=lambda *a: next(answers))))
    tg = telegram()
    photos = [message(n, text=None, media_group_id='album1', photo=[NS(file_unique_id=f'p{n}', file_id='f')])
              for n in range(1, 5)]
    for photo in photos[:3]:
        await handle(tg, photo)
    await handle(tg, photos[3])                              # после вердикта — без очереди
    deleted = sorted(call.args[1] for call in tg.delete_message.call_args_list)
    assert deleted == [1, 2, 3, 4]


# ---------- статус участника не отвечает ----------

@pytest.mark.asyncio
async def test_rate_limited_role_lookup_is_retried(state, monkeypatch):
    monkeypatch.setattr(bot.asyncio, 'sleep', AsyncMock())
    tg = telegram()
    tg.get_chat_member.side_effect = [RetryAfter(1), NS(status='member', until_date=None)]
    await handle(tg, message(text='ты мразь', uid=77))
    assert tg.get_chat_member.await_count == 2
    assert state.warn_count(CHAT, 77) == 1


# ---------- команды и прочее ----------

@pytest.mark.asyncio
async def test_admin_command_in_the_group_is_not_moderated(state):
    """/modtest с оскорблением, как советует справка, стоил админу предупреждения."""
    tg = telegram()
    command = NS(type='bot_command', offset=0, length=8)
    await handle(tg, message(text='/modtest твоя мать шлюха', uid=1, entities=(command,)))
    tg.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_command_is_moderated(state):
    tg = telegram()
    command = NS(type='bot_command', offset=0, length=8)
    await handle(tg, message(text='/modtest твоя мать шлюха', uid=7, entities=(command,)))
    tg.delete_message.assert_awaited_once()


@pytest.mark.parametrize(('attr', 'value', 'marker'), [
    ('dice', NS(emoji='🎰'), '[кубик 🎰]'),
    ('poll', NS(question='?'), '[опрос]'),
    ('contact', NS(phone_number='1'), '[контакт]'),
    ('location', NS(latitude=1), '[геометка]'),
    ('venue', NS(title='кафе'), '[место]'),
])
def test_dice_polls_and_contacts_are_visible_to_flood(attr, value, marker):
    fields = dict(text=None, caption=None, sticker=None, photo=None, animation=None, video=None,
                  video_note=None, voice=None, audio=None, document=None, dice=None, poll=None,
                  contact=None, location=None, venue=None, story=None, game=None)
    fields[attr] = value
    assert bot._mod_message_text(NS(**fields)) == marker


# ---------- правила: обычная речь и настоящие нарушения ----------

@pytest.mark.parametrize('text', [
    'сука, мать твою, опять филлер',          # восклицание, а не о матери
    'твою мать, сука, перенос',
    'ну твою мать! опять перенос, сука',
    'Сука, твой брат опять выиграл',
    'я идиот, твоя сестра была права',
    'кто-то слил билд 1.0.2.3',               # номер версии, не IP
    'заспамим лайками канал студии',          # фанатская акция
    'флудим этот чат стикерами в честь финала',
    'на-то была причина',                     # не «нато»
    'админ, сука, спасибо за перевод!',
    'Админ, говно серия, зачем постишь',
    'убью тебя за такие спойлеры 😂',
    'я тебя сейчас убью ахаха',
    'Он сказал ей: я тебя убью',              # пересказ сюжета
    'а потом он такой: убью тебя',
    'Дьявол сказал: сгори в аду',
    'можно смотреть без риска спойлеров @someone',
    'пиши на mail@example.com без риска спойлеров https://x.ru',
    'скинь пароль от вайфая',
])
def test_ordinary_speech_is_not_a_violation(text):
    assert rules.check_text(text, reply_to_user=True) is None


@pytest.mark.parametrize(('text', 'category'), [
    ('твоя мать шлюха', 'family'), ('шлюха твоя мать', 'family'), ('твоя мать — шлюха', 'family'),
    ('Твою мать назову шлюхой', 'family'), ('я ебал твою мать', 'family'),
    ('админ ты сука', 'toxic_admin'), ('админ, ты сука', 'toxic_admin'), ('сука админ', 'toxic_admin'),
    ('убью тебя', 'aggression'), ('я тебя убью, мразь', 'aggression'), ('я сказал: убью тебя', 'aggression'),
    ('вот его адрес ул. Ленина 5 и ip 192.168.1.10, сливаю', 'doxxing'),
    ('атакуем их канал https://t.me/x', 'raid'), ('заспамим их чат', 'raid'), ('рейдим канал стикерами', 'raid'),
    ('удвою твои деньги без риска https://x.ru', 'scam'), ('без риска, доход 300% пиши @casino_boss', 'scam'),
    ('скинь пароль от аккаунта', 'scam'),
    ('иди нахуй', 'aggression'), ('пошла нахуй', 'aggression'),
    ('НАТО опять', 'politics'), ('ты уебок', 'toxic'), ('ты долбаеб', 'toxic'),
])
def test_real_violations_are_still_caught(text, category):
    verdict = rules.check_text(text, reply_to_user=True)
    assert verdict is not None and verdict.category == category and verdict.confident


def test_email_is_not_a_channel_mention():
    assert rules._LINK.search('пиши на mail@example.com') is None
    assert rules._LINK.search('пиши @casino_boss')


def test_plain_english_words_keep_their_letters():
    """Рукописные подмены (n→п, u→и) — только в словах, где уже есть кириллица."""
    assert bot._mod_variants('run fun') == ('run fun',)


def test_accent_marks_do_not_hide_words():
    assert rules.normalize('пи́дор') == 'пидор'
    assert rules.normalize('йод') == 'йод'                  # «й» не распадается


@pytest.mark.parametrize('text', [
    'Он родился в Нигере', 'мы пидоры, признаём', 'кто такой хач?',
    'это было потрясающе :hurray:',            # смайл Shikimori собирался в оскорбление
    'hello under the run', 'хачапури вкусные', 'Нигерия выиграла',
])
def test_group_slur_exceptions(state, text):
    verdict = bot._mod_local_check(9001, 1, text, reply_to_user=True)
    assert verdict is None or verdict.get('category') != 'hate'


@pytest.mark.parametrize('text', [
    'ты nидор', 'ты пuдор', 'ты пuдоr', 'ты пи́дор', 'ты pidor', 'ты p1d0r',
    'Узкоглазые япошки опять', 'вы все пидорасы, правильно я понял?',
])
def test_disguised_slurs_are_caught(state, text):
    assert bot._mod_local_check(9002, 1, text, reply_to_user=True) == {'category': 'hate', 'confident': True}


# ---------- хранилище, /moderation, /modlog ----------

def test_corrupt_store_is_set_aside_and_reported(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(bot, '_queue_admin_alert', alerts.append)
    path = tmp_path / 'mod.json'
    path.write_text('{"chats": [-100], "mode": "act', encoding='utf-8')
    store = bot.ChatModerationStore(path)
    assert store.load_error and 'копия файла' in store.load_error
    backups = list(tmp_path.glob('mod.json.corrupt-*'))
    assert len(backups) == 1 and backups[0].read_text(encoding='utf-8').startswith('{"chats"')
    assert alerts and 'повреждён' in alerts[0]


def test_healthy_store_has_no_load_error(tmp_path):
    path = tmp_path / 'mod.json'
    path.write_text(json.dumps({'chats': [-100], 'mode': 'active'}), encoding='utf-8')
    store = bot.ChatModerationStore(path)
    assert store.load_error == '' and store.is_enabled(-100)


async def _moderation(update_user, chat_id, args, status='creator'):
    message = NS(reply_text=AsyncMock(), sender_chat=None)
    update = NS(effective_chat=NS(id=chat_id, type='supergroup'), effective_message=message,
                effective_user=update_user)
    tg = NS(get_chat_member=AsyncMock(return_value=NS(status=status)))
    await bot.moderation_command(update, NS(args=args, bot=tg))
    return message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_strangers_cannot_switch_moderation_on_in_their_groups(state, monkeypatch):
    """Чужая группа с ботом тратила общий бюджет модели и засыпала админов письмами."""
    monkeypatch.setattr(bot, 'is_admin', lambda update: False)
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    reply = await _moderation(NS(id=5, is_bot=False), -555, ['on'])
    assert 'не подключён' in reply and not state.is_enabled(-555)
    reply = await _moderation(NS(id=5, is_bot=False), -555, ['off'])
    assert 'выключена' in reply


@pytest.mark.asyncio
async def test_known_chat_admin_and_bot_owner_can_switch_it_on(state, monkeypatch):
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'is_admin', lambda update: False)
    state.set_chat(-777, False)                             # владелец когда-то подключал
    await _moderation(NS(id=5, is_bot=False), -777, ['on'])
    assert state.is_enabled(-777)
    monkeypatch.setattr(bot, 'is_admin', lambda update: True)
    await _moderation(NS(id=1, is_bot=False), -888, ['on'])
    assert state.is_enabled(-888)


def test_modlog_is_cut_by_whole_entries():
    """Срез [:4000] после экранирования рвал «&quot;» и <blockquote>."""
    rows = [dict(at=1_790_000_000 + n, category='toxic', action='warn', source='локальные правила',
                 name='"' * 60, user_id=n, reason='"&' * 150, text='"' * 400) for n in range(40)]
    text = bot._moderation_log_text(rows)
    assert len(text) <= 4000
    assert text.count('<blockquote>') == text.count('</blockquote>') >= 1
    assert text.endswith('/unwarn ответом на сообщение участника.')
    body = text.split('</blockquote>')[-2]
    assert not body.rstrip().endswith('&')
