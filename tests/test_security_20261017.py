"""Аудит безопасности, октябрь 2026."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.mark.parametrize('name', ['TG_ACCOUNT_SESSION', 'TG_ACCOUNT_API_HASH', 'EXPERIMENT_SALT'])
def test_envfile_hides_account_session(name):
    """По строке сессии входят в аккаунт Telegram без кода: в чат её не выводим."""
    value = '1ApWapzMBu' + 'x' * 300
    shown = bot._dotenv_shown_value(name, value)
    assert value[:10] not in shown and 'скрыто' in shown


def _await_input(mode, *, admin, open_moderation, monkeypatch):
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_ID', -100)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_ID', 10138)
    monkeypatch.setattr(bot, 'settings', MagicMock(extra_admins=[], open_moderation=open_moderation))
    monkeypatch.setattr(bot, 'is_admin', lambda update: admin)
    pending = MagicMock()
    pending.get = MagicMock(return_value={'title': 'T', 'link': 'https://x', 'source': 's'})
    pending.update_news = MagicMock(return_value=True)
    monkeypatch.setattr(bot, 'pending_posts', pending)
    scheduled = MagicMock()
    monkeypatch.setattr(bot, 'scheduled_posts', scheduled)
    monkeypatch.setattr(bot, '_update_preview_text', AsyncMock(return_value=True))
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(return_value=1))
    upd = MagicMock()
    upd.effective_chat = MagicMock(id=-100)
    upd.message = MagicMock(message_thread_id=10138, text='Новый текст поста')
    upd.message.reply_text = AsyncMock()
    upd.effective_user = MagicMock(id=424242, full_name='Гость', username=None)
    ctx = MagicMock()
    ctx.user_data = {'await_input': {'mode': mode, 'key': 'k1', 'chat_id': -100, 'thread_id': 10138}}
    try:
        asyncio.run(bot.awaiting_input_handler(upd, ctx))
    except bot.ApplicationHandlerStop:
        pass
    return ctx, pending, scheduled


def test_guest_edit_dropped_after_open_moderation_turned_off(monkeypatch):
    ctx, pending, _ = _await_input('edit', admin=False, open_moderation=False, monkeypatch=monkeypatch)
    pending.update_news.assert_not_called()
    assert 'await_input' not in ctx.user_data


def test_guest_edit_still_works_with_open_moderation(monkeypatch):
    _, pending, _ = _await_input('edit', admin=False, open_moderation=True, monkeypatch=monkeypatch)
    pending.update_news.assert_called_once()


def test_reschedule_needs_admin_even_with_open_moderation(monkeypatch):
    """Разжалованный админ с недоведённым переносом отложки не меняет время."""
    ctx, _, scheduled = _await_input('reschedule', admin=False, open_moderation=True,
                                     monkeypatch=monkeypatch)
    scheduled.get.assert_not_called()
    assert 'await_input' not in ctx.user_data


def test_admin_reschedule_goes_through(monkeypatch):
    _, _, scheduled = _await_input('reschedule', admin=True, open_moderation=False,
                                   monkeypatch=monkeypatch)
    scheduled.get.assert_called()


def test_streaming_dependencies_are_patched():
    """urllib3 < 2.8: злой сервер вешает потоковое чтение (бесконечный цикл
    на сжатом chunked-ответе) или раздувает память строкой размера чанка;
    бот так качает картинки и ролики с чужих серверов. idna < 3.15: имя
    домена из ленты надолго занимает процессор."""
    from pathlib import Path
    pins = Path(bot.__file__).with_name('requirements.txt').read_text(encoding='utf-8')
    assert 'urllib3==2.8.0' in pins and 'idna==3.15' in pins


# ---------- Ввод из чужих источников ----------

def test_feed_body_is_never_opened_as_a_path(tmp_path):
    """Сайт, отдавший телом ответа путь, не заставляет бота читать наш файл."""
    local = tmp_path / 'local.xml'
    local.write_text('<rss version="2.0"><channel><item><title>Секретная лента</title>'
                     '<link>https://local.example/1</link></item></channel></rss>', encoding='utf-8')
    for body in (str(local).encode(), str(local)):
        assert bot._feed_probe_stats(body).get('entries', 0) == 0
        assert bot._parse_rss_bytes(body, 'test', fetch_og=False) == []


def test_feed_text_still_parses():
    rss = ('<rss version="2.0"><channel><item><title>Анонс второго сезона</title>'
           '<link>https://example.com/a</link></item></channel></rss>')
    for body in (rss, rss.encode()):
        assert bot._feedparse(body).entries[0].title == 'Анонс второго сезона'


def test_huge_image_is_refused_before_decoding():
    """PNG в сотни килобайт на 13300×13300 раздувал процесс почти на гигабайт."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('1', (7000, 7000)).save(buf, 'PNG')       # 49 млн пикселей, файл крошечный
    with pytest.raises(Image.DecompressionBombError):
        with Image.open(io.BytesIO(buf.getvalue())) as im:
            im.convert('RGB')
    assert not bot._image_quality_info(buf.getvalue()).get('width')


def test_poster_size_image_still_opens():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4000, 6000)).save(buf, 'JPEG')     # большой постер — 24 млн пикселей
    assert bot._image_quality_info(buf.getvalue()).get('width') == 4000


EVIL = '<a href="https://evil.example/login">Подтвердите права</a>'


@pytest.mark.parametrize('result, requeued', [('failed', False), ('failed', None), ('uncertain', None)])
def test_publisher_alerts_escape_feed_titles(monkeypatch, result, requeued):
    """Заголовок из чужой ленты в письме админу не становится ссылкой."""
    news = {'title': EVIL, 'link': 'https://example.com/a'}
    queue = MagicMock()
    queue.pop_next = AsyncMock(side_effect=[news, None])
    queue.requeue_failed = AsyncMock(return_value=requeued)
    queue.ack_done = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, 'post_queue', queue)
    monkeypatch.setattr(bot, '_channel_quiet_now', lambda: False)
    monkeypatch.setattr(bot, '_llm_prefetch_queue_head', AsyncMock())
    monkeypatch.setattr(bot, 'send_news', AsyncMock(return_value=result))
    sent = []
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(side_effect=lambda _b, t: sent.append(t) or 1))
    asyncio.run(bot._publish_one_from_queue_locked(MagicMock()))
    assert sent and '<a href' not in sent[-1] and '&lt;a href=' in sent[-1]


def test_auto_disabled_source_alert_escapes_name(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, 'notify_admin', AsyncMock(side_effect=lambda _b, t: sent.append(t) or 1))
    monkeypatch.setattr(bot, '_pending_admin_alerts', [])
    monkeypatch.setattr(bot, '_auto_disabled_pending', [(EVIL, 'тишина <b>24 ч</b>')])
    asyncio.run(bot._drain_admin_alerts(MagicMock()))
    assert '<a href' not in sent[0] and '<b>' not in sent[0]


@pytest.mark.parametrize('text', ['Билеты на Evil.Shop уже продаются.', 'Подробнее на пример.рф'])
def test_post_text_drops_more_domains(text):
    from post_text import _strip_links
    out = _strip_links(text)
    assert 'Evil.Shop' not in out and 'пример.рф' not in out


def test_title_with_dot_is_not_a_domain():
    from post_text import _strip_links
    assert _strip_links('Трейлер Dr.Stone вышел.') == 'Трейлер Dr.Stone вышел.'
