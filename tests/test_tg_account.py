"""Каналы через аккаунт Telegram, обложки без чёрных кадров, без постов о выходе серий.

Аккаунт и сеть подменены: проверяется, что бот делает с ответами Telegram,
а не сам Telegram.
"""
import asyncio
import io
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

import anime_news_bot as bot
import tg_account


# ---------------------------------------------------------------- ссылки на медиа

def test_media_ref_round_trip():
    ref = tg_account.media_ref('nexvlsz', 11585, 'video')
    assert ref == 'tgacc://nexvlsz/11585/video'
    assert tg_account.parse_media_ref(ref) == ('nexvlsz', 11585, 'video')


@pytest.mark.parametrize('junk', ['https://t.me/x/1', 'tgacc://x/1/video', 'tgacc://chan/1/file',
                                  'tgacc://chan/../video', '', None])
def test_foreign_strings_are_not_media_refs(junk):
    assert tg_account.parse_media_ref(junk) is None


# ---------------------------------------------------------------- разбор сообщений

NOW = datetime.now(timezone.utc)


def _msg(msg_id, text='', **kw):
    base = dict(id=msg_id, date=NOW, message=text, grouped_id=None, action=None, photo=None,
                video=None, video_note=None, gif=None, entities=None, reply_markup=None,
                file=NS(duration=None, size=None))
    base.update(kw)
    return NS(**base)


def test_album_becomes_one_post_with_all_photos():
    messages = [_msg(12, photo=object(), grouped_id=7),
                _msg(11, 'Новый постер аниме «Ты здесь» и дата премьеры', photo=object(), grouped_id=7),
                _msg(10, '')]
    posts = tg_account.posts_from_messages('chan', messages)
    assert len(posts) == 1
    assert posts[0].msg_id == 11
    assert posts[0].photo_refs == ['tgacc://chan/11/photo', 'tgacc://chan/12/photo']


def test_video_post_keeps_size_and_duration():
    video = _msg(20, 'Вышел трейлер аниме «Ты здесь»', video=object(),
                 file=NS(duration=80, size=95 * 1024 * 1024))
    post = tg_account.posts_from_messages('chan', [video])[0]
    assert post.video_ref == 'tgacc://chan/20/video'
    assert post.video_thumb_ref == 'tgacc://chan/20/thumb'
    assert (post.video_duration, post.video_size) == (80, 95 * 1024 * 1024)


def test_gif_and_service_messages_are_not_posts_with_video():
    gif = _msg(30, 'Смешная гифка без новости внутри', video=object(), gif=object())
    service = _msg(31, 'Канал закрепил фото', action=object())
    posts = tg_account.posts_from_messages('chan', [service, gif])
    assert len(posts) == 1 and posts[0].video_ref == ''


def test_hidden_and_button_links_are_kept():
    button = NS(url='https://t.me/game_robot?start=promo')
    msg = _msg(40, 'Играй в новую игру по ссылке https://example.com/a',
               entities=[NS(url='https://youtu.be/abcdefghijk')],
               reply_markup=NS(rows=[NS(buttons=[button])]))
    links = tg_account.posts_from_messages('chan', [msg])[0].links
    assert 'https://youtu.be/abcdefghijk' in links
    assert 'https://example.com/a' in links
    assert 'https://t.me/game_robot?start=promo' in links


# ---------------------------------------------------------------- клиент

class FakeClient:
    def __init__(self, authorized=True, messages=(), media=b'photo'):
        self.authorized = authorized
        self.messages = list(messages)
        self.media = media
        self.connected = False

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return NS(first_name='Ридер', last_name='', username='reader')

    async def disconnect(self):
        self.connected = False

    async def get_entity(self, channel):
        return NS(title=f'Канал {channel}')

    async def get_messages(self, channel, limit=None, ids=None):
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        return self.messages[:limit]

    async def download_media(self, msg, file=None, thumb=None):
        if file is bytes:
            return self.media
        Path(file).write_bytes(b'video-bytes')
        return file


def _reader(client):
    return tg_account.AccountReader(1, 'hash', 'session', client_factory=lambda: client)


@pytest.mark.asyncio
async def test_unauthorized_session_is_reported_not_crashed():
    reader = _reader(FakeClient(authorized=False))
    assert await reader.start() is False
    assert 'TG_ACCOUNT_SESSION' in reader.error
    assert reader.ready is False


@pytest.mark.asyncio
async def test_sync_wait_from_event_loop_is_refused():
    """Из цикла событий ждать свой же цикл нельзя — он бы встал навсегда."""
    reader = _reader(FakeClient())
    assert await reader.start()
    with pytest.raises(tg_account.AccountUnavailable, match='цикла событий'):
        reader.run_sync(lambda: reader.fetch('chan'), 0.2)


@pytest.mark.asyncio
async def test_worker_thread_reads_channel_through_the_loop():
    reader = _reader(FakeClient(messages=[_msg(5, 'Анонсирован второй сезон аниме «Ты здесь»')]))
    assert await reader.start()
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        out=reader.run_sync(lambda: reader.fetch('chan'), 5)))
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    title, posts = result['out']
    assert title == 'Канал chan' and posts[0].msg_id == 5


@pytest.mark.asyncio
async def test_video_over_limit_is_not_downloaded(tmp_path):
    big = _msg(9, 'трейлер', video=object(), file=NS(duration=90, size=500 * 1024 * 1024))
    reader = _reader(FakeClient(messages=[big]))
    await reader.start()
    path, note = await reader.download_video('tgacc://chan/9/video', tmp_path, 300 * 1024 * 1024)
    assert path is None and 'больше лимита' in note


@pytest.mark.asyncio
async def test_video_is_downloaded_to_file(tmp_path):
    clip = _msg(9, 'трейлер', video=object(), file=NS(duration=90, size=20 * 1024 * 1024))
    reader = _reader(FakeClient(messages=[clip]))
    await reader.start()
    path, _note = await reader.download_video('tgacc://chan/9/video', tmp_path, 300 * 1024 * 1024)
    assert path.read_bytes() == b'video-bytes'


# ---------------------------------------------------------------- бот: новости из аккаунта

def _post(msg_id=1, text='Вышел трейлер аниме «Ты здесь» от студии Rouseact', **kw):
    base = dict(channel='chan', msg_id=msg_id, date=NOW, text=text, links=[], photo_refs=[],
                video_ref='', video_thumb_ref='', video_duration=None, video_size=None)
    base.update(kw)
    return tg_account.AccountPost(**base)


def test_account_video_post_becomes_news_with_real_cover():
    post = _post(video_ref='tgacc://chan/1/video', video_thumb_ref='tgacc://chan/1/thumb',
                 video_duration=80)
    news = bot._tg_account_news('chan', 'TG: chan', 'Канал', [post])[0]
    assert news['video'] == 'tgacc://chan/1/video'
    assert news['images'] == ['tgacc://chan/1/thumb']
    assert news['link'] == 'https://t.me/chan/1'
    assert news['_thumb_only'] is False


def test_too_long_video_gives_only_its_cover():
    post = _post(video_ref='tgacc://chan/1/video', video_thumb_ref='tgacc://chan/1/thumb',
                 video_duration=bot.TG_VIDEO_MAX_SECONDS + 60)
    news = bot._tg_account_news('chan', 'TG: chan', 'Канал', [post])[0]
    assert news['video'] is None
    assert news['images'] == ['tgacc://chan/1/thumb']


def test_old_and_empty_posts_are_skipped():
    old = _post(2, date=NOW - timedelta(days=30))
    empty = _post(3, text='🔥')
    assert bot._tg_account_news('chan', 'TG: chan', 'Канал', [old, empty]) == []


@pytest.fixture
def account(monkeypatch):
    fake = NS(ready=True, account_name='Ридер', error='')
    monkeypatch.setattr(bot, 'tg_account', fake)
    return fake


def test_channel_is_read_through_account_when_ready(account, monkeypatch):
    account.run_sync = lambda factory, timeout: ('Канал', [_post()])
    account.fetch = lambda channel, limit: None
    monkeypatch.setattr(bot, 'http_get_public_with_retry',
                        lambda *a, **k: pytest.fail('веб-страница не нужна'))
    news = bot.get_telegram_channel('chan', 'TG: chan')
    assert len(news) == 1 and news[0]['_via_tg_account']


def test_account_failure_falls_back_to_web_page(account, monkeypatch):
    def broken(factory, timeout):
        raise tg_account.AccountUnavailable('Telegram не ответил')
    account.run_sync = broken
    account.fetch = lambda channel, limit: None
    web = []
    monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: web.append(1))
    bot.get_telegram_channel('chan', 'TG: chan')
    assert web, 'сбой аккаунта оставил канал без новостей'


def test_account_refs_are_downloaded_by_the_account(account):
    account.run_sync = lambda factory, timeout: b'jpeg'
    account.download_bytes = lambda ref, limit: None
    assert bot._download_image_bytes('tgacc://chan/1/photo') == b'jpeg'
    assert bot._download_needed_host('tgacc://chan/1/photo') is True


@pytest.mark.asyncio
async def test_prepare_video_uses_the_account(account, monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'x' * 10)

    async def download_video(ref, dest, limit):
        return clip, 'скачан через аккаунт'
    account.download_video = download_video
    monkeypatch.setattr(bot, 'settings', NS(video_enabled=True))
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: None)
    monkeypatch.setattr(bot, '_normalize_video_file', lambda p, info=None: p)
    news = {'video': 'tgacc://chan/1/video', 'title': 't'}
    assert await bot._prepare_video_file(news) == clip
    assert news['_video_note'] == 'скачан через аккаунт'


def test_without_account_the_video_note_says_why(monkeypatch):
    monkeypatch.setattr(bot, 'tg_account', None)
    monkeypatch.setattr(bot, 'settings', NS(video_enabled=True))
    news = {'video': 'tgacc://chan/1/video', 'title': 't'}
    assert asyncio.run(bot._prepare_video_file(news, record_failures=False)) is None
    assert 'не подключён' in news['_video_note']


def test_session_string_never_reaches_logs(monkeypatch):
    monkeypatch.setattr(bot, 'TG_ACCOUNT_SESSION', '1BVtsOK4Bu' + 'x' * 300)
    monkeypatch.setattr(bot, 'TG_ACCOUNT_API_HASH', '0123456789abcdef0123456789abcdef')
    text = bot._redact_secrets('ошибка: 1BVtsOK4Bu' + 'x' * 300 + ' hash 0123456789abcdef0123456789abcdef')
    assert 'xxxx' not in text and '0123456789abcdef' not in text


def test_status_line_tells_which_way_channels_are_read(monkeypatch, account):
    monkeypatch.setattr(bot, 'TG_ACCOUNT_API_ID', 1)
    monkeypatch.setattr(bot, 'TG_ACCOUNT_API_HASH', 'h')
    monkeypatch.setattr(bot, 'TG_ACCOUNT_SESSION', 's')
    assert 'через аккаунт Telegram 🟢' in bot._tg_account_status_line()
    account.ready = False
    account.error = 'вход сброшен'
    assert 'вход сброшен' in bot._tg_account_status_line()
    monkeypatch.setattr(bot, 'TG_ACCOUNT_SESSION', '')
    assert 'не настроен' in bot._tg_account_status_line()


# ---------------------------------------------------------------- ужать ролик под лимит

def test_small_video_is_left_as_is(tmp_path):
    clip = tmp_path / 'a.mp4'
    clip.write_bytes(b'x' * 100)
    assert bot._shrink_video_to_limit(clip) == clip


def test_big_video_is_reencoded_to_fit(tmp_path, monkeypatch):
    clip = tmp_path / 'a.mp4'
    clip.write_bytes(b'x' * 200)
    monkeypatch.setattr(bot, 'VIDEO_MAX_FILE_SIZE_MB', 0.0001)        # ~100 байт
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: {'duration': 0.0005})
    commands = []

    def fake_run(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b'y' * 50)
        return NS(returncode=0)
    monkeypatch.setattr(bot.subprocess, 'run', fake_run)
    out = bot._shrink_video_to_limit(clip)
    assert out is not None and out.read_bytes() == b'y' * 50
    assert '-b:v' in commands[0]
    assert not clip.exists()


def test_video_that_would_become_mush_is_dropped(tmp_path, monkeypatch):
    clip = tmp_path / 'a.mp4'
    clip.write_bytes(b'x' * 200)
    monkeypatch.setattr(bot, 'VIDEO_MAX_FILE_SIZE_MB', 0.0001)
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: {'duration': 3600.0})

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b'y' * 50)
        return NS(returncode=0)
    monkeypatch.setattr(bot.subprocess, 'run', fake_run)
    assert bot._shrink_video_to_limit(clip) is None


# ---------------------------------------------------------------- чёрные кадры

def _image(dark: bool) -> bytes:
    if dark:
        im = Image.effect_noise((320, 180), 64).point(lambda v: v // 8).convert('RGB')
    else:
        im = Image.effect_noise((320, 180), 90).convert('RGB')
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=90)
    return out.getvalue()


def test_dark_title_card_is_dark_and_a_real_frame_is_not():
    assert bot._frame_is_dark(bot._image_quality_info(_image(dark=True)))
    assert not bot._frame_is_dark(bot._image_quality_info(_image(dark=False)))


@pytest.mark.asyncio
async def test_black_frame_cut_from_the_video_is_not_posted(monkeypatch):
    frame = 'https://cdn4.telesco.pe/file/video.mp4' + bot.VIDEO_FRAME_SUFFIX
    real = 'https://news.example/poster.jpg'
    pictures = {frame: _image(dark=True), real: _image(dark=False)}
    bot._image_bytes_cache.clear()
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda url: pictures.get(url))
    monkeypatch.setattr(bot, '_download_image_bytes', lambda url: pictures.get(url))
    monkeypatch.setattr(bot, 'fetch_og_image', lambda link: None)
    news = {'images': [frame, real], 'link': 'https://t.me/ch/1'}
    await bot._optimize_news_media(news)
    assert frame not in news['images']


def test_video_cover_skips_dark_opening(monkeypatch, tmp_path):
    clip = tmp_path / 'v.mp4'
    clip.write_bytes(b'video')
    bot._video_thumbnail_cache.clear()
    monkeypatch.setattr(bot, '_media_tool', lambda name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(bot, 'feature_enabled', lambda key: True)
    monkeypatch.setattr(bot, '_probe_video_file', lambda p: {'duration': 80.0})
    frames = iter([_image(dark=True), _image(dark=False)])

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(next(frames))
        return NS(returncode=0)
    monkeypatch.setattr(bot.subprocess, 'run', fake_run)
    cover = bot._generate_video_thumbnail(clip)
    assert not bot._frame_is_dark(bot._image_quality_info(cover))


# ---------------------------------------------------------------- выход серий

@pytest.mark.parametrize('title', [
    'Кадры 12 серии аниме «Магическая битва»',
    'Серия 12 «Магической битвы»: вышли кадры',
    'Эпизод 5 — опубликовано превью',
    'Превью 5 серии «Ледяной стены»',
    'Первая серия аниме выйдет 5 января',
    'Сериал продлили на второй сезон из 12 серий',
])
def test_episode_previews_and_premiere_dates_stay(title):
    assert bot.noise_reason({'title': title}) == ''


def test_model_can_mark_an_episode_release_as_filler():
    assert 'серия' in bot.LLM_KINDS_FILLER
    assert 'серия —' in bot.LLM_SYSTEM_PROMPT
