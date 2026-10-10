"""Качество постов, октябрь 2026: что увидели в канале и чем это лечится.

За одно утро в канале вышли три новости, которые админы уже опубликовали
сами, два «юбилейных» поста и несколько постов, где текст пересказывает
заголовок. Примеры ниже — пересказ тех постов, а не их копия.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


# ---------- посты админов попадают в память дедупа ----------

@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'story_registry', bot.StoryRegistry(tmp_path / 'reg.json'))
    monkeypatch.setattr(bot, 'published_texts', bot.PublishedTexts(tmp_path / 'texts.json'))
    monkeypatch.setattr(bot, 'image_hashes', bot.ImageHashes(tmp_path / 'img.json'))
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: key != 'work_identity')
    return bot


ADMIN_POST = ('🔥 Сервис подтвердил аниме-адаптацию манхвы «Боксёр»\n\n'
              'Производством займётся студия, дата выхода пока не объявлена.')


def test_greeting_is_not_remembered():
    assert bot._manual_post_news('🌤 Доброе утро!', 'https://t.me/c/1') is None


def test_admin_post_becomes_news_for_dedup():
    news = bot._manual_post_news(ADMIN_POST, 'https://t.me/chan/5')
    assert news is not None
    assert 'Боксёр' in news['title']
    assert news['source'] == bot.MANUAL_POST_SOURCE


def test_bot_does_not_repeat_what_admin_posted(memory):
    bot._remember_manual_channel_post(bot._manual_post_news(ADMIN_POST, 'https://t.me/chan/5'))
    later = {'title': 'Сервис подтвердил аниме-адаптацию манхвы «Боксёр»',
             'summary': 'Дата выхода не объявлена.', 'link': 'https://example.com/boxer'}
    result = bot.story_registry.observe(later, ['Другой канал'], [later['link']])
    assert result['delivery_duplicate'] is True
    assert bot.published_texts.find_similar(later['title'])


def test_admin_photo_and_youtube_are_remembered(memory):
    news = bot._manual_post_news(ADMIN_POST + '\nhttps://youtu.be/abcdefghijk', 'https://t.me/chan/6')
    bot._remember_manual_channel_post(news, 'f' * 16)
    stored = {item['h'] for item in bot.image_hashes._items}
    assert 'f' * 16 in stored
    assert 'v:abcdefghijk' in stored


@pytest.fixture
def channel(memory, monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_ID', -1001)
    remembered = []
    monkeypatch.setattr(bot, '_remember_manual_channel_post',
                        lambda news, fp='': remembered.append((news, fp)))
    return remembered


def _post(chat_id, text, photo=()):
    return NS(channel_post=NS(chat=NS(id=chat_id, username='chan'), message_id=7,
                              text=text, caption=None, photo=photo))


@pytest.mark.asyncio
async def test_handler_remembers_post_from_our_channel(channel):
    await bot.manual_channel_post_handler(_post(-1001, ADMIN_POST), MagicMock())
    assert len(channel) == 1
    assert channel[0][0]['link'] == 'https://t.me/chan/7'


@pytest.mark.asyncio
async def test_handler_ignores_other_channels(channel):
    await bot.manual_channel_post_handler(_post(-2002, ADMIN_POST), MagicMock())
    assert channel == []


@pytest.mark.asyncio
async def test_handler_fingerprints_admin_photo(channel, monkeypatch):
    tg_file = NS(download_as_bytearray=AsyncMock(return_value=bytearray(b'img')))
    context = NS(bot=NS(get_file=AsyncMock(return_value=tg_file)))
    monkeypatch.setattr(bot, '_image_fingerprint', lambda data: 'hash-of-' + data.decode())
    await bot.manual_channel_post_handler(_post(-1001, ADMIN_POST, photo=(NS(file_id='p'),)), context)
    assert channel[0][1] == 'hash-of-img'


def test_channel_username_match(monkeypatch):
    monkeypatch.setattr(bot, 'CHANNEL_ID', '@Fubuki61')
    assert bot._is_our_channel(NS(id=-5, username='fubuki61'))
    assert not bot._is_our_channel(NS(id=-5, username='other'))


# ---------- юбилеи и дни рождения — не новость ----------

@pytest.mark.parametrize('title', [
    'Четыре года назад вышло аниме «Синяя тюрьма»',
    '11 лет со дня премьеры аниме «Паразит»',
    'Сегодня день рождения Мурасакибары из «Баскетбола Куроко»',
    'Аниме «Боксёр» исполнилось 5 лет',
    'One Piece turns 25 today',
])
def test_anniversary_is_noise(title):
    assert bot.noise_reason({'title': title}) == 'годовщина и ностальгия'


@pytest.mark.parametrize('title', [
    'К 10-летию аниме выйдет новый фильм',
    'Bleach 20th Anniversary project announced',
    'Анонсирован 2 сезон аниме «Синяя тюрьма»',
    'Сериал выйдет через три года',
])
def test_anniversary_with_real_event_is_news(title):
    assert bot.noise_reason({'title': title}) == ''


def test_model_can_mark_anniversary_as_filler():
    assert 'юбилей' in bot.LLM_KINDS_FILLER
    assert 'юбилей' in bot.LLM_SYSTEM_PROMPT


# ---------- текст не пересказывает заголовок ----------

def test_echo_sentence_inside_paragraph_is_dropped():
    title = 'Показан первый тизер второго сезона аниме «Обжорство берсерка»'
    body = ['Аниме «Обжорство берсерка» получило первый тизер второго сезона. Премьера в 2027 году.']
    assert bot._drop_repetitive_paragraphs(title, body) == ['Премьера в 2027 году.']


def test_sentence_with_new_number_stays():
    title = 'Вышел трейлер аниме «Ледяная стена»'
    body = ['Вышел трейлер аниме «Ледяная стена». Второй сезон стартует 9 января.']
    assert bot._drop_repetitive_paragraphs(title, body) == ['Второй сезон стартует 9 января.']


def test_news_verbs_do_not_count_as_new_content():
    """«Вышел» и «представлен» — дежурные слова заметки, не новый факт."""
    assert bot._too_similar('Вышел тизер аниме «Ледяная стена»',
                            'Представлен тизер аниме «Ледяная стена»')


# ---------- пресет Gemini работает для нового ключа ----------

def test_gemini_preset_is_open_to_new_keys():
    """С сентября 2026 модели 2.5 отдаются только тем, кто ими уже пользовался:
    новый GEMINI_API_KEY на пресете 2.5 Flash получал бы отказ."""
    _url, model = bot.LLM_PRESETS['gemini']
    assert not model.startswith('gemini-2.')


# ---------- октябрь 2026, вторая неделя: жаргон, дефисы, пересказ через «;» ----------

@pytest.mark.parametrize('raw,expected', [
    ('Вышел KV и главный трейлер аниме.', 'Вышел постер и главный трейлер аниме.'),
    ('Показан второй PV аниме.', 'Показан второй промо-ролик аниме.'),
    ('Представлен ключевой визуал, без ключевого визуала никак.',
     'Представлен ключевой постер, без ключевого постера никак.'),
    ('Аниме‑адаптация вышла.', 'Аниме-адаптация вышла.'),
])
def test_jargon_and_nonbreaking_hyphens_are_cleaned(raw, expected):
    assert bot._polish_post_text(raw) == expected


def test_model_names_with_kv_are_left_alone():
    assert bot._polish_post_text('Танк KV-2, самолёт PV-1 и студия KVA.') == \
        'Танк KV-2, самолёт PV-1 и студия KVA.'


def test_llm_post_goes_through_polish(monkeypatch):
    monkeypatch.setattr(bot, 'settings', None)
    text = bot.format_news_short({'_llm_text': 'Вышел KV аниме‑проекта «X».', 'title': 't'})
    assert 'KV' not in text and '‑' not in text


def test_echo_half_of_a_semicolon_sentence_is_dropped():
    title = 'Показан постер аниме «Едок: Сверхзаказ»'
    body = ['Постер аниме «Едок: Сверхзаказ» опубликован; анимацию делает студия Cab.']
    assert bot._drop_repetitive_paragraphs(title, body) == ['Анимацию делает студия Cab.']


def test_semicolon_sentence_without_echo_is_kept_as_written():
    assert bot._drop_repetitive_paragraphs('Анонс', ['Премьера в 2027 году; студия MAPPA.']) == \
        ['Премьера в 2027 году; студия MAPPA.']


def test_subject_is_asked_in_latin_to_match_russian_and_english_sources():
    """Бот дважды выложил перенос «Призрака Цусимы»: один источник английский,
    другой русский, и ключи «Ghost of Tsushima» и «Призрак Цусимы» не совпали."""
    assert 'ЛАТИНИЦЕЙ' in bot.LLM_SYSTEM_PROMPT and 'Ghost of Tsushima: Legends' in bot.LLM_SYSTEM_PROMPT
    subjects = bot.RecentSubjects
    assert subjects._key('Ghost of Tsushima: Legends') == subjects._key('ghost of tsushima legends')
