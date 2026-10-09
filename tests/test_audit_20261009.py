"""Аудит 9 октября 2026: найденные ошибки и проверки, что они не вернутся.

Каждый тест — на конкретный сбой: что ломалось, и почему это было видно
владельцу (потерянные отложенные посты, падение бота при старте, модель,
которая тратит суточный лимит на заведомые отказы, и т. п.).
"""
import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot
import moderation_media as media
from conftest import with_media_senders


# ---------- секреты дополнительных провайдеров ----------

def test_extra_provider_key_is_hidden(monkeypatch):
    """Роутер может вернуть ключ в тексте ошибки 401 — в лог и админу он не идёт."""
    secret = 'gsk_FAKEextraProviderKey1234567890'
    monkeypatch.setattr(bot, '_LLM_EXTRA_ENV', {'groq': (secret, '')})
    out = bot._redact_secrets(f'HTTP 401: invalid api key {secret}')
    assert secret not in out
    assert '<скрыто>' in out


# ---------- битые байты в файлах хранилищ не роняют запуск ----------

BAD_BYTES = b'\xff\xfe\x00{"items": ['


def test_history_with_bad_bytes_does_not_crash_start(tmp_path):
    path = tmp_path / 'sent.json'
    path.write_bytes(BAD_BYTES)
    store = bot.SentLinksStore(path)          # раньше — UnicodeDecodeError
    assert store is not None


def test_queue_with_bad_bytes_is_quarantined(tmp_path):
    path = tmp_path / 'queue.json'
    path.write_bytes(BAD_BYTES)
    queue = bot.PostQueue(path)
    assert asyncio.run(queue.peek_size()) == 0
    assert list(tmp_path.glob('queue.json.corrupt-*')), 'битый файл не отложен в сторону'


def test_anilist_cache_with_bad_bytes_starts_empty(tmp_path):
    path = tmp_path / 'anilist.json'
    path.write_bytes(BAD_BYTES)
    client = bot.AniListClient(path)
    assert client._cache == {}


# ---------- отложка, посты в ветке и свои источники не затираются ----------

def _news(n):
    return {'title': f'Новость {n}', 'link': f'https://example.com/{n}', 'summary': ''}


def test_broken_schedule_file_is_kept_aside_not_overwritten(tmp_path):
    path = tmp_path / 'scheduled.json'
    first = bot.ScheduledPosts(path)
    when = bot.datetime.now(bot.timezone.utc)
    for n in range(3):
        first.add(_news(n), when)
    original = path.read_text(encoding='utf-8')
    path.write_text(original[: len(original) // 2], encoding='utf-8')   # обрыв записи

    second = bot.ScheduledPosts(path)
    key = second.add(_news(9), when)
    copies = list(tmp_path.glob('scheduled.json.corrupt-*'))
    assert copies, 'битую отложку перезаписали без копии'
    assert copies[0].read_text(encoding='utf-8') == original[: len(original) // 2]
    assert int(key) > 1000, 'ключи начались с нуля и совпадут со старыми кнопками'


def test_broken_pending_file_does_not_reuse_old_button_keys(tmp_path):
    """Под старыми постами в ветке висят кнопки pub:1, pub:2… — новый пост
    с тем же ключом опубликовался бы по чужой кнопке."""
    path = tmp_path / 'pending.json'
    path.write_text('{"counter": 2, "items": {', encoding='utf-8')
    pending = bot.PendingPosts(path)
    assert list(tmp_path.glob('pending.json.corrupt-*'))
    key = pending.add(_news(1))
    assert key not in {'1', '2', '3'}


def _unreadable(monkeypatch, path):
    """Файл есть, но чтение падает (права, сбой диска) — запись при этом работает."""
    real = bot.Path.read_text

    def read_text(self, *a, **k):
        if self == path:
            raise PermissionError('нет доступа')
        return real(self, *a, **k)
    monkeypatch.setattr(bot.Path, 'read_text', read_text)


def test_unreadable_schedule_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / 'scheduled.json'
    path.write_text('{"counter": 5, "items": {}}', encoding='utf-8')
    _unreadable(monkeypatch, path)
    store = bot.ScheduledPosts(path)
    with pytest.raises(OSError):                # админ узнает, что отложка не записалась
        store.add(_news(1), bot.datetime.now(bot.timezone.utc))
    monkeypatch.undo()
    assert path.read_text(encoding='utf-8') == '{"counter": 5, "items": {}}'


def test_broken_custom_sources_are_kept_aside(tmp_path):
    path = tmp_path / 'custom.json'
    path.write_text('[{"type": "rss", "value": "https://a', encoding='utf-8')
    sources = bot.CustomSources(path)
    assert sources.all() == []
    assert list(tmp_path.glob('custom.json.corrupt-*')), 'источники админа потеряны без копии'


def test_unreadable_custom_sources_are_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / 'custom.json'
    original = '[{"type": "rss", "value": "https://a.example/rss", "label": "A"}]'
    path.write_text(original, encoding='utf-8')
    _unreadable(monkeypatch, path)
    sources = bot.CustomSources(path)
    sources._save()
    monkeypatch.undo()
    assert path.read_text(encoding='utf-8') == original


# ---------- долгий Retry-After у модели ----------

def _reply(status, text='{}', headers=None):
    r = MagicMock(status_code=status, text=text, headers=headers or {})
    r.json.return_value = {'choices': [{'message': {'content': 'готово'}}]}
    return r


@pytest.fixture
def alone(monkeypatch, tmp_path):
    for name, value in (
        ('LLM_BASE_URL', 'https://one.test/v1'), ('LLM_API_KEY', 'key-one-aaaa'),
        ('LLM_MODEL', 'model-one'), ('LLM_MODEL_ALTERNATES', ()),
        ('LLM_FALLBACK_API_KEY', ''), ('LLM_FAST_API_KEY', ''),
        ('LLM_MIN_INTERVAL', 0.0), ('LLM_INLINE_RETRY_MAX_SEC', 20.0),
        ('_llm_using_fallback', False), ('_llm_candidate', ()),
        ('_llm_tried_candidates', set()), ('_llm_disabled_runtime', False),
        ('_llm_disabled_reason', ''), ('_llm_fail_streak', 0),
        ('_llm_circuit_until', 0.0), ('_llm_last_call', 0.0), ('_llm_pace', {}),
        ('_llm_wait_hint_sec', 0.0), ('LLM_CIRCUIT_MAX_SEC', 7200),
        ('_llm_json_mode', False), ('_queue_admin_alert', lambda _m: None),
    ):
        monkeypatch.setattr(bot, name, value)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    bot._llm_last_failure.clear()
    return monkeypatch


@pytest.mark.asyncio
async def test_hour_long_retry_after_closes_the_provider_for_an_hour(alone):
    """С потолком 30 с бот спрашивал исчерпанного провайдера каждые полминуты,
    и каждый такой вызов съедал суточный лимит."""
    calls = []

    def post(*a, **k):
        calls.append(1)
        return _reply(429, 'daily quota exceeded', {'Retry-After': '3600'})

    alone.setattr(bot.requests, 'post', post)
    assert await bot._llm_call([{'role': 'user', 'content': 'hi'}], 100) is None
    assert bot._llm_circuit_until - time.monotonic() > 3000


def test_retry_after_keeps_short_cap_for_inline_sleeps():
    assert bot._parse_retry_after('3600') == bot.HTTP_RETRY_MAX_DELAY
    assert bot._parse_retry_after('3600', cap=7200) == 3600


# ---------- дополнительный провайдер как основной ----------

GEMINI = ('https://generativelanguage.googleapis.com/v1beta/openai', 'AIza-test-key', 'gemini-x')


@pytest.fixture
def only_gemini(monkeypatch, tmp_path):
    for name, value in (('LLM_API_KEY', ''), ('LLM_BASE_URL', ''), ('LLM_MODEL', ''),
                        ('LLM_FALLBACK_API_KEY', ''), ('LLM_FAST_API_KEY', ''),
                        ('_llm_using_fallback', False), ('_llm_candidate', ())):
        monkeypatch.setattr(bot, name, value)
    monkeypatch.setattr(bot, 'LLM_EXTRA_SLOTS', {'extra_gemini': GEMINI})
    monkeypatch.setattr(bot, 'LLM_SLOTS', ('primary', 'fallback', 'fast', 'extra_gemini'))
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    return monkeypatch


def test_single_extra_key_is_enough_for_the_model(only_gemini):
    """Только GEMINI_API_KEY в окружении — модель работает, а не «не настроена»."""
    assert bot._llm_primary_slot() == 'extra_gemini'
    assert bot._llm_configured() is True


def test_extra_provider_can_be_chosen_as_primary(tmp_path):
    settings = bot.BotSettings(tmp_path / 's.json')
    settings.llm_primary_slot = 'extra_gemini'
    assert settings.llm_primary_slot == 'extra_gemini'
    assert bot.BotSettings(tmp_path / 's.json').llm_primary_slot == 'extra_gemini'
    settings.llm_primary_slot = 'extra_<script>'
    assert settings.llm_primary_slot == ''


# ---------- пост канала с забракованным пересказом не ждёт 4 часа ----------

@pytest.fixture
def llm(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://x/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    return bot


def _llm_reply(payload):
    return MagicMock(status_code=200, text='', headers={},
                     json=lambda: {'choices': [{'message': {
                         'content': json.dumps(payload, ensure_ascii=False)}}]})


def test_invented_numbers_mark_the_rewrite_as_rejected(llm, monkeypatch):
    news = {'title': 'Сериал продлили', 'summary': 'Студия объявила продолжение.',
            'source': 'TG:somechannel'}
    answer = {'relevant': True, 'topic': 'аниме', 'title': 'Сериал продлили на 3 сезон',
              'summary': 'Премьера 12 октября 2027 года.'}
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: _llm_reply(answer))
    asyncio.run(bot._llm_enrich(news, use_cache=False))
    assert '_llm_text' not in news
    assert news.get('_editorial_rejection') == 'numbers_dates'


def test_oversized_rewrite_is_marked_as_rejected(llm, monkeypatch):
    news = {'title': 'Короткий', 'summary': 'Два слова.', 'source': 'TG:somechannel'}
    answer = {'relevant': True, 'topic': 'аниме', 'title': 'Норм', 'summary': 'Мусор. ' * 400}
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: _llm_reply(answer))
    asyncio.run(bot._llm_enrich(news, use_cache=False))
    assert '_llm_text' not in news
    assert news.get('_editorial_rejection') == 'limits'


# ---------- «долг» не ловит «долгожданный» ----------

@pytest.mark.parametrize('text', ['Долгожданный трейлер второго сезона', 'Фанаты долго ждали',
                                  'Долгий путь героя'])
def test_long_awaited_is_not_corporate_news(text):
    assert not bot._AFFINITY_BUSINESS_RE.search(text)


@pytest.mark.parametrize('text', ['Долг студии вырос', 'миллиардов долга', 'долговая нагрузка'])
def test_real_debt_is_still_corporate_news(text):
    assert bot._AFFINITY_BUSINESS_RE.search(text)


# ---------- кнопка «Отправить сейчас» не теряет отложенный пост ----------

def test_send_now_keeps_deferred_post_in_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=False,
                                                   require_image=False))
    queue = bot.PostQueue(tmp_path / 'q.json')
    asyncio.run(queue.push_many([_news(1)]))
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, 'is_admin', lambda u: True)
    monkeypatch.setattr(bot, 'send_news', AsyncMock(return_value='deferred'))
    q = MagicMock(data='queue:send_now')
    for m in ('answer', 'edit_message_text'):
        setattr(q, m, AsyncMock())
    upd = MagicMock(callback_query=q)
    upd.effective_user = MagicMock(id=bot.ADMIN_ID, full_name='Dobe')
    asyncio.run(bot.settings_callback(upd, MagicMock(bot=MagicMock())))
    assert asyncio.run(queue.peek_size()) == 1, 'отложенный пост пропал из очереди'


# ---------- модерация: разрешённое 16+ не глушит проверку текста ----------

@pytest.fixture
def chat(tmp_path, monkeypatch):
    bot._init_globals()
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', False)
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    store.set_mode('active')
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_all_admin_ids', lambda: [7])
    monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_MEDIA_ENABLED', True)
    monkeypatch.setattr(bot, 'MODERATION_ACTION_COOLDOWN_SEC', 0)
    for name in ('_moderation_action_lock', '_moderation_update_lock'):
        monkeypatch.setattr(bot, name, asyncio.Lock())
    for name in ('_MOD_LAST_ACTION', '_moderation_seen_updates', '_moderation_recent',
                 '_moderation_windows', '_moderation_user_notices', '_moderation_report_recent',
                 '_moderation_media_reports'):
        monkeypatch.setattr(bot, name, {})
    classify = AsyncMock(return_value={'violation': True, 'category': 'scam', 'severity': 3,
                                       'confidence': 1.0, 'reason': 'мошенническая ссылка'})
    monkeypatch.setattr(bot, '_moderation_classify', classify)
    # Текст подозрительный, но уверенности нет — его должна оценить модель.
    monkeypatch.setattr(bot, '_mod_local_check', lambda *a, **k: {
        'category': 'scam', 'confident': False, 'severity': 2, 'reason': 'похоже на развод'})

    async def run(scan, caption='Забирай халяву по ссылке', number=1):
        tg = with_media_senders(NS(
            get_chat_member=AsyncMock(return_value=NS(status='member')),
            delete_message=AsyncMock(return_value=True), restrict_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(), promote_chat_member=AsyncMock(),
            send_message=AsyncMock(return_value=NS(message_id=900)),
            edit_message_text=AsyncMock()))
        monkeypatch.setattr(bot, '_moderation_media_scanner', NS(check=AsyncMock(return_value=scan)))
        msg = NS(chat_id=-100, message_id=number, text=None, caption=caption, sender_chat=None,
                 reply_to_message=None, media_group_id=None, has_media_spoiler=False,
                 from_user=NS(id=50 + number, full_name='Участник', is_bot=False),
                 animation=NS(file_unique_id=f'g{number}', file_id='f', mime_type='video/mp4'))
        await bot.moderation_message_handler(
            NS(effective_message=msg, effective_user=msg.from_user,
               effective_chat=NS(id=-100), edited_message=None), NS(bot=tg))
        return tg
    return NS(run=run, store=store, classify=classify)


BUTTOCKS = 'Откровенный контент требует спойлера: BUTTOCKS_EXPOSED'


@pytest.mark.asyncio
async def test_allowed_16_plus_picture_does_not_hide_scam_text(chat):
    await chat.run(media.Scan('checked', 'spoiler_16', BUTTOCKS, 16, .97, hits=8))
    chat.classify.assert_awaited()


@pytest.mark.asyncio
async def test_old_auto_16_block_does_not_hide_scam_text(chat):
    """Записи 16+, которые бот завёл сам до разрешения 16+, больше не действуют."""
    chat.store.block_media('auto1', -100, 'spoiler_16', ['g1'], [], 'auto')
    await chat.run(media.Scan('checked', '', '', 4, 0.0))
    chat.classify.assert_awaited()


@pytest.mark.asyncio
async def test_admin_blocked_16_plus_copy_is_deleted_without_sanction(chat):
    """/modmiss spoiler_16 обещает «копии будут удаляться сразу»."""
    chat.store.block_media('miss:1', -100, 'spoiler_16', ['g1'], [], 'admin')
    tg = await chat.run(media.Scan('checked', '', '', 4, 0.0), caption=None)
    tg.delete_message.assert_awaited_once_with(-100, 1)
    tg.restrict_chat_member.assert_not_awaited()
    assert chat.store.warn_count(-100, 51) == 0


def test_admin_16_entry_is_effective_and_auto_is_not(monkeypatch):
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', False)
    assert bot._mod_effective_block({'category': 'spoiler_16', 'source': 'auto'}) == {}
    admin = {'category': 'spoiler_16', 'source': 'admin'}
    assert bot._mod_effective_block(admin) == admin
    nsfw = {'category': 'nsfw', 'source': 'auto'}
    assert bot._mod_effective_block(nsfw) == nsfw
    monkeypatch.setattr(bot, 'MODERATION_PUNISH_16', True)
    auto16 = {'category': 'spoiler_16', 'source': 'auto'}
    assert bot._mod_effective_block(auto16) == auto16
