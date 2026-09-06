import base64
import json
import threading

import requests

import anime_news_bot as bot


def _basic(user, password):
    raw = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': 'Basic ' + raw}


def test_dashboard_requires_dedicated_token(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'admin_dashboard', True)
    monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', '')
    assert bot._dashboard_authorized(_basic('admin', 'anything')) is False


def test_dashboard_basic_auth_is_constant_path_and_separate(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'admin_dashboard', True)
    monkeypatch.setattr(bot, 'DASHBOARD_USER', 'operator')
    monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', 'dash-secret')
    assert bot._dashboard_authorized(_basic('operator', 'dash-secret')) is True
    assert bot._dashboard_authorized(_basic('operator', 'wrong')) is False
    assert bot._dashboard_authorized({'Authorization': 'Bearer dash-secret'}) is False


def test_dashboard_snapshot_does_not_expose_tokens(monkeypatch):
    monkeypatch.setattr(bot, 'TOKEN', 'BOT-TOKEN-DO-NOT-LEAK')
    monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', 'DASH-TOKEN-DO-NOT-LEAK')
    snap = bot._dashboard_snapshot()
    raw = json.dumps(snap, ensure_ascii=False)
    assert 'BOT-TOKEN-DO-NOT-LEAK' not in raw
    assert 'DASH-TOKEN-DO-NOT-LEAK' not in raw


def test_dashboard_html_escapes_untrusted_titles():
    snap = {
        'generated_at': 'now', 'ready': True,
        'queue': {'size': 1, 'inflight': None, 'items': [
            {'title': '<script>alert(1)</script>', 'source': 'x&y', 'priority': 1, 'queued_at': 'now'}]},
        'scheduled': {'count': 0, 'items': []},
        'uncertain': {'ledger': 0, 'scheduled': 0, 'pending': 0},
        'delivery_30d': {'attempts': 1, 'sent': 1, 'failed': 0, 'uncertain': 0},
        'sources': [], 'errors': [], 'lifecycle': {},
    }
    page = bot._dashboard_html(snap).decode()
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert 'x&amp;y' in page


def test_dashboard_http_requires_auth_and_serves_json(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'admin_dashboard', True)
    monkeypatch.setattr(bot, 'DASHBOARD_USER', 'admin')
    monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', 'secret123')
    server = bot.ThreadingHTTPServer(('127.0.0.1', 0), bot._HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_address[1]}'
        denied = requests.get(base + '/admin', timeout=2)
        assert denied.status_code == 401
        assert denied.headers.get('WWW-Authenticate', '').startswith('Basic ')
        ok = requests.get(base + '/admin/data.json', auth=('admin', 'secret123'), timeout=2)
        assert ok.status_code == 200
        assert ok.headers['Cache-Control'] == 'no-store'
        payload = ok.json()
        assert 'queue' in payload and 'sources' in payload and 'uncertain' in payload
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
