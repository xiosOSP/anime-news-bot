"""Stage 19: regression tests for reliable video discovery and delivery."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anime_news_bot as bot


def test_relative_article_and_rss_video_urls_become_absolute():
    assert bot._find_video_in_html(
        '<video src="/media/trailer.mp4"></video>',
        'https://news.example/anime/story',
    ) == 'https://news.example/media/trailer.mp4'

    entry = SimpleNamespace(
        link='https://news.example/anime/story',
        enclosures=[{'type': 'video/mp4', 'href': '../clips/pv.mp4'}],
        media_content=[],
    )
    assert bot.extract_video_url(entry) == 'https://news.example/clips/pv.mp4'


def test_relative_og_image_becomes_absolute(monkeypatch):
    response = MagicMock(
        status_code=200,
        text='<meta property="og:image" content="../images/cover.jpg">',
        headers={'Content-Type': 'text/html'},
    )
    monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: response)

    assert bot.fetch_og_image('https://news.example/anime/story') == \
        'https://news.example/images/cover.jpg'


def test_video_host_requires_real_domain_boundary():
    assert bot._is_video_host('https://www.youtube.com/watch?v=ok') is True
    assert bot._is_video_host('https://youtube.com.attacker.test/watch?v=no') is False
    assert bot._is_direct_video('/relative/trailer.mp4') is False


def test_article_video_discovery_does_not_depend_on_llm(monkeypatch):
    news = {
        'title': 'New anime trailer released',
        'summary': 'Watch it now',
        'link': 'https://news.example/story',
        'source': 'Example',
        'video': None,
    }
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(video_enabled=True))
    monkeypatch.setattr(bot, '_llm_active', lambda: False)
    fetch = MagicMock(return_value={
        'text': '',
        'video': 'https://www.youtube.com/watch?v=stage19',
    })
    monkeypatch.setattr(bot, 'fetch_article', fetch)

    asyncio.run(bot._discover_article_video(news))

    fetch.assert_called_once_with(news['link'])
    assert news['video'].endswith('stage19')
    assert news['_video_note'] == 'ролик найден в статье'


def test_pending_video_file_id_is_atomic_and_persistent(tmp_path):
    path = tmp_path / 'pending.json'
    store = bot.PendingPosts(path)
    key = store.add({'title': 'Trailer', 'link': 'https://example.test/1'})

    assert store.set_video_file_id(key, 'BAACAg-stage19') is True
    assert bot.PendingPosts(path).get(key)['_telegram_video_file_id'] == 'BAACAg-stage19'


def test_moderation_response_file_id_is_saved_for_publication(tmp_path, monkeypatch):
    path = tmp_path / 'pending.json'
    store = bot.PendingPosts(path)
    monkeypatch.setattr(bot, 'pending_posts', store)
    news = {'title': 'Trailer', 'link': 'https://example.test/2'}
    key = store.add(news)
    response = SimpleNamespace(video=SimpleNamespace(file_id='BAACAg-reuse'))

    bot._remember_video_file_id(key, response, news)

    assert news['_telegram_video_file_id'] == 'BAACAg-reuse'
    assert bot.PendingPosts(path).get(key)['_telegram_video_file_id'] == 'BAACAg-reuse'


def test_thread_video_send_captures_real_response_file_id(tmp_path, monkeypatch):
    store = bot.PendingPosts(tmp_path / 'pending.json')
    monkeypatch.setattr(bot, 'pending_posts', store)
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(
        video_enabled=True, require_image=False,
    ))
    monkeypatch.setattr(bot, 'format_news_text_long', lambda _news: 'Post')
    tg = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(
        video=SimpleNamespace(file_id='BAACAg-from-telegram'),
        message_id=1, chat_id=-100,
    )))
    news = {
        'title': 'Trailer', 'summary': '', 'source': 'Example',
        'link': 'https://example.test/story', 'images': [],
        'video': 'https://cdn.example/trailer.mp4',
    }

    assert asyncio.run(bot._send_post_thread_split(tg, news, None)) is True

    key = news['_pending_key']
    assert store.get(key)['_telegram_video_file_id'] == 'BAACAg-from-telegram'


def test_channel_send_prefers_saved_file_id(monkeypatch):
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(
        video_enabled=True, require_image=False,
    ))
    monkeypatch.setattr(bot, 'format_news_post', lambda _news: 'Post')
    resolve = AsyncMock(side_effect=AssertionError('URL must not be resolved again'))
    monkeypatch.setattr(bot, '_resolve_video', resolve)
    tg = SimpleNamespace(
        send_video=AsyncMock(return_value=SimpleNamespace()),
        send_message=AsyncMock(), send_photo=AsyncMock(), send_media_group=AsyncMock(),
    )
    news = {
        'title': 'Trailer', 'source': 'Example', 'images': [],
        'video': 'https://cdn.example/trailer.mp4',
        '_telegram_video_file_id': 'BAACAg-cached',
    }

    assert asyncio.run(bot._send_post(tg, news, -100, None)) is True

    assert tg.send_video.await_args.kwargs['video'] == 'BAACAg-cached'
    resolve.assert_not_awaited()


def test_manual_channel_helper_prepares_old_items_and_cleans_file(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'mp4')
    prepare = AsyncMock(return_value=video)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_prepare_video_file', prepare)
    monkeypatch.setattr(bot, '_send_channel_post', send)
    news = {'title': 'Old pending trailer', 'video': 'https://youtube.com/watch?v=x'}

    assert asyncio.run(bot._prepare_and_send_channel_post(object(), news)) is True

    prepare.assert_awaited_once_with(news)
    assert send.await_args.args[2] == video
    assert not video.exists()


def test_manual_channel_helper_skips_download_when_file_id_exists(monkeypatch):
    prepare = AsyncMock(side_effect=AssertionError('file_id must avoid download'))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_prepare_video_file', prepare)
    monkeypatch.setattr(bot, '_send_channel_post', send)
    news = {'title': 'New pending trailer', '_telegram_video_file_id': 'BAACAg-ready'}

    assert asyncio.run(bot._prepare_and_send_channel_post(object(), news)) is True

    prepare.assert_not_awaited()
    assert send.await_args.args[2] is None


def test_telegram_embed_budget_is_spent_on_newest_posts(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    html = ''.join(
        f'<div class="tgme_widget_message" data-post="ch/{i}">'
        f'<div class="tgme_widget_message_text">Trailer post number {i} has details</div>'
        '<div class="tgme_widget_message_video_player"></div>'
        f'<time datetime="{now}"></time></div>'
        for i in range(10)
    )
    monkeypatch.setattr(bot, 'settings', MagicMock(post_max_age_hours=24))
    monkeypatch.setattr(bot, 'http_get_public_with_retry',
                        lambda *a, **k: MagicMock(status_code=200, text=html))
    calls = []
    monkeypatch.setattr(bot, '_fetch_video_from_embed',
                        lambda post_id: calls.append(post_id) or (None, None, None))

    posts = bot.get_telegram_channel('ch', 'TG: Test')

    assert calls == ['ch/9', 'ch/8', 'ch/7']
    assert [post['link'] for post in posts] == [
        'https://t.me/ch/5', 'https://t.me/ch/6', 'https://t.me/ch/7',
        'https://t.me/ch/8', 'https://t.me/ch/9',
    ]


def test_media_failure_snapshot_is_bounded():
    with bot._media_failure_lock:
        bot._media_failure_counts.clear()
        bot._media_failures.clear()
    overflow = bot.MEDIA_FAILURES_KEEP + 5
    for idx in range(overflow):
        bot._record_media_failure(
            {'title': f'Trailer {idx}', 'source': 'Test'},
            'video_download_failed',
        )

    snapshot = bot.media_failure_snapshot()
    # Счётчик считает все сбои, а список последних ограничен потолком.
    assert snapshot['counts']['video_download_failed'] == overflow
    assert len(snapshot['recent']) == bot.MEDIA_FAILURES_KEEP
    assert snapshot['recent'][-1]['title'].endswith(str(overflow - 1))
    # Самые старые вытеснены: первым остался тот, что не влез в потолок.
    assert snapshot['recent'][0]['title'] == f'Trailer {overflow - bot.MEDIA_FAILURES_KEEP}'

    with bot._media_failure_lock:
        bot._media_failure_counts.clear()
        bot._media_failures.clear()
