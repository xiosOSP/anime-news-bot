from types import SimpleNamespace
from pathlib import Path

import pytest

import anime_news_bot as bot


def _candidate(domain='newsource.example', evidence=0.8, feed_url=''):
    return {
        'domain': domain,
        'homepage': f'https://{domain}/',
        'discovered_url': f'https://{domain}/news/example',
        'feed_url': feed_url,
        'evidence': evidence,
        'context': 'Official source announcement',
    }


def test_extract_discovery_links_filters_same_site_and_social_noise():
    html = '''
      <html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>
      <body>
        <p>Source: <a href="https://studio.example/news/anime-x">Official studio announcement</a></p>
        <a href="https://twitter.com/studio/status/1">Twitter</a>
        <a href="https://origin.example/other">same site</a>
        <a href="https://cdn.example/image.jpg">image</a>
      </body></html>'''
    rows = bot._extract_source_discovery_links(html, 'https://origin.example/news/story')
    assert [r['domain'] for r in rows] == ['studio.example']
    assert rows[0]['evidence'] >= 0.7


def test_extract_external_feed_link_marks_direct_feed():
    html = '<p>Official feed: <a href="https://publisher.example/rss.xml">RSS</a></p>'
    rows = bot._extract_source_discovery_links(html, 'https://blog.example/article')
    assert len(rows) == 1
    assert rows[0]['feed_url'] == 'https://publisher.example/rss.xml'
    assert rows[0]['evidence'] >= 0.9


def test_source_discovery_candidate_id_is_stable():
    assert bot.SourceDiscoveryStore.candidate_id('Example.COM') == bot.SourceDiscoveryStore.candidate_id('example.com')


def test_configured_host_is_not_rediscovered(tmp_path):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    store.note_configured_source('Known', 'https://news.example/feed.xml')
    assert store.observe(_candidate('news.example'), found_by_source='Other', article_url='https://other.example/a') is None
    assert store.observe(_candidate('sub.news.example'), found_by_source='Other', article_url='https://other.example/a') is None


def test_candidate_stays_shadow_until_multiple_mentions(tmp_path, monkeypatch):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_MIN_MENTIONS', 2)
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_SUGGEST_SCORE', 0.60)
    cid = store.observe(_candidate(), found_by_source='A', article_url='https://a.example/x')
    store.record_probe(cid, {
        'ok': True, 'feed_url': 'https://newsource.example/feed.xml',
        'feed_items': 12, 'recent_items': 10, 'anime_relevance': 1.0,
        'label': 'New Source',
    })
    assert store.get(cid)['status'] == 'shadow'
    store.observe(_candidate(), found_by_source='B', article_url='https://b.example/y')
    assert store.get(cid)['status'] == 'suggested'


def test_dismissed_candidate_does_not_return_to_suggested(tmp_path, monkeypatch):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_MIN_MENTIONS', 1)
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_SUGGEST_SCORE', 0.3)
    cid = store.observe(_candidate(), found_by_source='A', article_url='https://a.example/x')
    store.record_probe(cid, {'ok': True, 'feed_url': 'https://newsource.example/feed.xml',
                             'feed_items': 10, 'recent_items': 10, 'anime_relevance': 1.0})
    assert store.dismiss(cid)
    store.observe(_candidate(), found_by_source='B', article_url='https://b.example/y')
    assert store.get(cid)['status'] == 'dismissed'


def test_feed_probe_stats_uses_valid_entries_and_relevance(monkeypatch):
    entries = [
        SimpleNamespace(title='Anime X Season 2 Trailer', link='https://x.example/1',
                        published_parsed=None, updated_parsed=None),
        SimpleNamespace(title='Manga Y adaptation announced', link='https://x.example/2',
                        published_parsed=None, updated_parsed=None),
        SimpleNamespace(title='General company post', link='https://x.example/3',
                        published_parsed=None, updated_parsed=None),
    ]
    fake_feed = SimpleNamespace(entries=entries, feed={'title': 'Publisher News'})
    monkeypatch.setattr(bot.feedparser, 'parse', lambda data: fake_feed)
    row = bot._feed_probe_stats(b'<rss/>')
    assert row['ok'] is True
    assert row['feed_items'] == 3
    assert row['anime_relevance'] > 0.5


def test_probe_candidate_discovers_feed_from_html(monkeypatch):
    class Resp:
        status_code = 200
        encoding = 'utf-8'
        def __init__(self, content):
            self.content = content
        def close(self):
            pass

    html = b'<head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>'
    monkeypatch.setattr(bot, '_is_public_http_url', lambda url: True)
    monkeypatch.setattr(bot, 'http_get_public_with_retry', lambda *a, **k: Resp(html))
    monkeypatch.setattr(bot, '_read_limited_response', lambda r, limit: r.content)
    monkeypatch.setattr(bot, '_probe_feed_url', lambda url: ({'ok': True, 'feed_url': url,
                                                              'feed_items': 8, 'recent_items': 5,
                                                              'anime_relevance': 0.8}
                                                             if url.endswith('/feed.xml') else {'ok': False, 'error': 'no'}))
    row = bot._probe_source_discovery_candidate(_candidate())
    assert row['ok'] is True
    assert row['feed_url'] == 'https://newsource.example/feed.xml'


@pytest.mark.asyncio
async def test_discovery_skips_under_backpressure(tmp_path, monkeypatch):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    flags = dict(bot.FEATURE_FLAGS)
    flags['source_discovery'] = True
    flags['backpressure'] = True
    monkeypatch.setattr(bot, 'FEATURE_FLAGS', flags)
    monkeypatch.setattr(bot, 'source_discovery', store)

    # Заглушка обязана повторять API настоящей PostQueue: размер отдаёт только
    # async peek_size(). Прежний вариант с __len__ был единственной причиной,
    # по которой тест проходил, пока автопоиск падал в проде TypeError-ом.
    queue = bot.PostQueue(tmp_path / 'queue.json')
    monkeypatch.setattr(bot, 'settings', SimpleNamespace(require_image=False))
    await queue.push_many([
        {'title': f'News {i}', 'link': f'https://example.test/{i}', 'source': 'S'}
        for i in range(bot.BACKPRESSURE_SOFT_QUEUE)])
    monkeypatch.setattr(bot, 'post_queue', queue)
    assert await queue.peek_size() >= bot.BACKPRESSURE_SOFT_QUEUE
    result = await bot._run_source_discovery([{'link': 'https://known.example/a', 'source': 'Known'}])
    assert result['skipped'] == 'backpressure'
    assert store.rows() == []


def test_manual_promotion_is_explicit_and_enters_custom_sources(tmp_path, monkeypatch):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    custom = bot.CustomSources(tmp_path / 'custom.json')
    cid = store.observe(_candidate(), found_by_source='A', article_url='https://a.example/x')
    store.record_probe(cid, {'ok': True, 'feed_url': 'https://newsource.example/feed.xml',
                             'feed_items': 8, 'recent_items': 8, 'anime_relevance': 1.0,
                             'label': 'New Anime Source'})
    monkeypatch.setattr(bot, 'source_discovery', store)
    monkeypatch.setattr(bot, 'custom_sources', custom)
    monkeypatch.setattr(bot, 'SOURCES', [('Existing', lambda: [])])
    monkeypatch.setattr(bot, '_is_public_http_url', lambda url: True)
    ok, label = bot._promote_discovered_source(cid)
    assert ok is True
    assert label == 'New Anime Source'
    assert custom.all()[0]['value'] == 'https://newsource.example/feed.xml'
    assert any(name == 'New Anime Source' for name, _ in bot.SOURCES)
    assert store.get(cid)['status'] == 'promoted'


def test_runtime_schema_knows_source_discovery_file(tmp_path):
    path = tmp_path / 'source_discovery.json'
    path.write_text('{"candidates":{},"scanned":{},"configured_hosts":{}}', encoding='utf-8')
    report = bot._migrate_runtime_schemas(tmp_path)
    assert report['ok'] is True
    raw = __import__('json').loads(path.read_text(encoding='utf-8'))
    assert raw['schema_version'] == 1


def test_source_discovery_runtime_file_is_ignored_by_git():
    """Рабочий файл не должен попадать в репозиторий.

    Проверяем через сам git, а не поиском имени в .gitignore: правила там
    свернули в «*.json» с исключениями, и точное имя больше не встречается —
    хотя файл по-прежнему игнорируется.
    """
    import subprocess
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(['git', 'check-ignore', '-q', 'source_discovery.json'],
                            cwd=root, capture_output=True)
    assert result.returncode == 0, 'source_discovery.json не игнорируется git'


def test_discovery_auto_probe_rejects_raw_ip_and_custom_port(monkeypatch):
    monkeypatch.setattr(bot, '_is_public_http_url', lambda url: True)
    assert bot._is_safe_discovery_url('https://203.0.113.10/feed.xml') is False
    assert bot._is_safe_discovery_url('https://news.example:8443/feed.xml') is False
    assert bot._is_safe_discovery_url('https://news.example/feed.xml') is True


def test_failed_reprobe_demotes_previous_suggestion(tmp_path, monkeypatch):
    store = bot.SourceDiscoveryStore(tmp_path / 'discovery.json')
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_MIN_MENTIONS', 1)
    monkeypatch.setattr(bot, 'SOURCE_DISCOVERY_SUGGEST_SCORE', 0.3)
    cid = store.observe(_candidate(), found_by_source='A', article_url='https://a.example/x')
    store.record_probe(cid, {'ok': True, 'feed_url': 'https://newsource.example/feed.xml',
                             'feed_items': 10, 'recent_items': 10, 'anime_relevance': 1.0})
    assert store.get(cid)['status'] == 'suggested'
    store.record_probe(cid, {'ok': False, 'error': 'temporary feed failure'})
    assert store.get(cid)['status'] == 'shadow'
