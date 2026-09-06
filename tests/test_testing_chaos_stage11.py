import asyncio
import io
import json
import random
import stat
import warnings
import zipfile

import pytest

import anime_news_bot as bot


def _zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files:
            if isinstance(payload, str):
                payload = payload.encode('utf-8')
            zf.writestr(name, payload)
    return buf.getvalue()


def test_runtime_schema_migrates_legacy_core_files_and_is_idempotent(tmp_path):
    (tmp_path / 'sent_links.json').write_text(json.dumps(['https://a.test/1']), encoding='utf-8')
    (tmp_path / 'post_queue.json').write_text(json.dumps([]), encoding='utf-8')
    (tmp_path / 'bot_settings.json').write_text(json.dumps({'check_interval_min': 30}), encoding='utf-8')
    (tmp_path / 'scheduled_posts.json').write_text(json.dumps({'counter': 2, 'items': {}}), encoding='utf-8')
    (tmp_path / 'pending_posts.json').write_text(json.dumps({'counter': 3, 'items': {}}), encoding='utf-8')

    first = bot._migrate_runtime_schemas(tmp_path)
    second = bot._migrate_runtime_schemas(tmp_path)

    assert first['ok'] is True
    assert set(first['changed']) >= {
        'sent_links.json', 'post_queue.json', 'bot_settings.json',
        'scheduled_posts.json', 'pending_posts.json',
    }
    assert second['ok'] is True
    assert second['changed'] == []
    assert json.loads((tmp_path / 'sent_links.json').read_text())['urls'] == ['https://a.test/1']
    assert json.loads((tmp_path / 'post_queue.json').read_text())['schema_version'] == 1
    assert json.loads((tmp_path / 'bot_settings.json').read_text())['schema_version'] == 1
    manifest = json.loads((tmp_path / 'runtime_schema.json').read_text())
    assert manifest['schema_version'] == bot.RUNTIME_SCHEMA_VERSION


def test_runtime_schema_corrupt_known_file_is_not_overwritten(tmp_path):
    path = tmp_path / 'post_queue.json'
    raw = '{this is not json'
    path.write_text(raw, encoding='utf-8')
    report = bot._migrate_runtime_schemas(tmp_path)
    assert report['ok'] is False
    assert any('post_queue.json' in e for e in report['errors'])
    assert path.read_text(encoding='utf-8') == raw


def test_core_stores_persist_schema_version(tmp_path, monkeypatch):
    fake = type('S', (), {'require_image': False})()
    monkeypatch.setattr(bot, 'settings', fake)

    sent = bot.SentLinksStore(tmp_path / 'sent_links.json')
    asyncio.run(sent.claim('https://e.test/1', 'One'))
    queue = bot.PostQueue(tmp_path / 'post_queue.json')
    asyncio.run(queue.push_many([{'link': 'https://e.test/2', 'title': 'Two', 'images': []}]))
    settings = bot.BotSettings(tmp_path / 'bot_settings.json')
    settings.save()
    scheduled = bot.ScheduledPosts(tmp_path / 'scheduled_posts.json')
    scheduled._save()
    pending = bot.PendingPosts(tmp_path / 'pending_posts.json')
    pending._save()

    for name in ('sent_links.json', 'post_queue.json', 'bot_settings.json',
                 'scheduled_posts.json', 'pending_posts.json'):
        assert json.loads((tmp_path / name).read_text())['schema_version'] == 1


def test_backup_restore_materializes_and_loads_core_stores(tmp_path):
    data = _zip_bytes([
        ('sent_links.json', json.dumps({'schema_version': 1, 'urls': [], 'titles': [], 'recent': [], 'reservations': {}, 'rejected': {}})),
        ('post_queue.json', json.dumps({'schema_version': 1, 'items': [], 'inflight': None})),
        ('bot_settings.json', json.dumps({'schema_version': 1, 'check_interval_min': 30})),
        ('scheduled_posts.json', json.dumps({'schema_version': 1, 'counter': 0, 'items': {}})),
        ('pending_posts.json', json.dumps({'schema_version': 1, 'counter': 0, 'items': {}})),
        ('analytics_events.json', json.dumps({'schema_version': 1, 'events': []})),
    ])
    result = bot._restore_backup_archive(data, tmp_path / 'restore')
    assert result['ok'] is True
    assert result['restored'] == 6
    selftest = bot._backup_restore_selftest(data)
    assert selftest['ok'] is True
    assert set(selftest['stores']) == {'sent_links', 'queue', 'settings', 'scheduled', 'pending', 'analytics'}


def test_backup_restore_refuses_path_traversal_and_symlink(tmp_path):
    traversal = _zip_bytes([('../escape.json', '{}')])
    assert bot._restore_backup_archive(traversal, tmp_path / 'x')['ok'] is False
    assert not (tmp_path / 'escape.json').exists()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        info = zipfile.ZipInfo('link.json')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, 'target.json')
    check = bot._verify_backup_archive(buf.getvalue())
    assert check['ok'] is False
    assert any('symlink' in e for e in check['errors'])


def test_backup_verify_rejects_duplicate_member_names():
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('same.json', '{}')
            zf.writestr('same.json', '{}')
    result = bot._verify_backup_archive(buf.getvalue())
    assert result['ok'] is False
    assert any('duplicate path' in e for e in result['errors'])


@pytest.mark.asyncio
async def test_stage11_chaos_selftest_passes_without_external_network(monkeypatch):
    # Force the self-test to prove it does not require a pre-initialized settings singleton.
    monkeypatch.setattr(bot, 'settings', None)
    result = await bot._run_chaos_selftest(12, seed=123)
    assert result['ok'] is True, result
    assert all(row['ok'] for row in result['checks'].values())
    assert result['checks']['ledger_single_winner']['detail'] == 'winners=1'


def _deterministic_parser_fuzz(cases=200):
    rng = random.Random(123456)
    alphabet = '<>&"\'\\/?:#=%[](){}\x00\n\r\t abcXYZ0123456789Аниме日本語😀'
    for _ in range(cases):
        text = ''.join(rng.choice(alphabet) for _ in range(rng.randrange(0, 1200)))
        normalized = bot.normalize_url(text)
        cleaned = bot.clean_html(text)
        article = bot._extract_article_text(text)
        bot._find_video_in_html(text)
        bot._parse_rss_bytes(
            (f'<rss><channel><item><title>{text}</title><link>https://example.test/x</link>'
             f'<description>{text}</description></item></channel></rss>').encode('utf-8', errors='ignore'),
            'FuzzRSS', fetch_og=False)
        bot.parse_episode(text)
        bot._parse_duration(text)
        bot._normalize_image_url(text)
        assert isinstance(normalized, str)
        assert isinstance(cleaned, str)
        assert isinstance(article, str)


def test_deterministic_parser_fuzz_200_cases():
    _deterministic_parser_fuzz(200)


# In a normal dev/CI install requirements-dev includes Hypothesis and this test
# becomes a true generated property test. The execution sandbox used for this
# audit may not have PyPI access, so keep an equivalent deterministic fallback.
try:
    from hypothesis import given, settings as hypothesis_settings, strategies as st
except ImportError:  # pragma: no cover - exercised in minimal audit env
    st = None


if st is not None:
    @hypothesis_settings(max_examples=250, deadline=None)
    @given(st.text(max_size=1500))
    def test_hypothesis_text_helpers_never_crash(text):
        assert isinstance(bot.normalize_url(text), str)
        assert isinstance(bot.clean_html(text), str)
        assert isinstance(bot._extract_article_text(text), str)
        bot._find_video_in_html(text)
        bot._parse_rss_bytes(
            (f'<rss><channel><item><title>{text}</title><link>https://example.test/x</link>'
             f'<description>{text}</description></item></channel></rss>').encode('utf-8', errors='ignore'),
            'FuzzRSS', fetch_og=False)
        bot.parse_episode(text)
        bot._parse_duration(text)
        bot._normalize_image_url(text)
else:
    def test_hypothesis_text_helpers_never_crash():
        _deterministic_parser_fuzz(250)


def test_runtime_schema_refuses_future_version(tmp_path):
    path = tmp_path / 'bot_settings.json'
    original = {'schema_version': 99, 'check_interval_min': 45}
    path.write_text(json.dumps(original), encoding='utf-8')
    report = bot._migrate_runtime_schemas(tmp_path)
    assert report['ok'] is False
    assert any('future schema' in e for e in report['errors'])
    assert json.loads(path.read_text(encoding='utf-8')) == original


def test_atomic_json_write_preserves_old_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'old': True}), encoding='utf-8')

    def boom(_src, _dst):
        raise OSError('simulated disk/replace failure')

    monkeypatch.setattr(bot.os, 'replace', boom)
    with pytest.raises(OSError):
        bot._atomic_write_json(path, {'new': True})
    assert json.loads(path.read_text(encoding='utf-8')) == {'old': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_malformed_rss_entry_does_not_abort_later_valid_entry(monkeypatch):
    class Entry(dict):
        __getattr__ = dict.get

    class Feed:
        entries = [
            Entry(link='https://example.test/broken', summary='<p>no title</p>'),
            Entry(title='Valid anime news', link='https://example.test/valid', summary='<p>Body</p>'),
        ]

    monkeypatch.setattr(bot.feedparser, 'parse', lambda _data: Feed())
    monkeypatch.setattr(bot, 'sent_links', None)
    monkeypatch.setattr(bot, 'settings', None)
    rows = bot._parse_rss_bytes(b'<rss/>', 'FuzzRSS', fetch_og=False)
    assert [row['title'] for row in rows] == ['Valid anime news']
