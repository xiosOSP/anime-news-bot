"""Регрессии на две проблемы, найденные при ревью Stage 9.

1. Health-сервер после Stage 8 автоматически уходит на 0.0.0.0, когда PaaS
   задал PORT. При этом он поднимал поток на каждое соединение без таймаута
   и без потолка, а /metrics отдавался кому угодно без авторизации.
2. `_prioritize_news` пересчитывала score, ключ франшизы и адаптивный
   множитель внутри вложенного цикла — больше секунды заморозки event loop
   на батче из 60 новостей.
"""
import base64
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def server():
    """Поднимает health-сервер на свободном порту и гасит его после теста."""
    srv = bot._BoundedHealthServer(('127.0.0.1', 0), bot._HealthHandler,
                                   max_concurrent=8, max_per_ip=3)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _raw_get(port, path='/healthz', headers=b'', timeout=5, method='GET'):
    s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        s.sendall(f'{method} {path} HTTP/1.0\r\n'.encode() + headers + b'\r\n')
        chunks = []
        while True:                      # дочитываем до закрытия, иначе рвём сервер
            part = s.recv(4096)
            if not part:
                break
            chunks.append(part)
        return b''.join(chunks).decode('utf-8', errors='replace')
    finally:
        s.close()


def _wait_drained(srv, timeout=20.0):
    """Обработчики завершаются в своих потоках, дадим им дойти до конца."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with srv._lock:
            if not srv._owner and not srv._per_ip:
                return True
        time.sleep(0.02)
    return False


class TestHealthServerLimits:
    def test_handler_has_socket_timeout(self):
        """Без таймаута полуоткрытое соединение держит поток вечно."""
        assert bot._HealthHandler.timeout is not None
        assert 0 < bot._HealthHandler.timeout <= 60

    def test_concurrent_connections_are_capped(self, server):
        srv, port = server
        hung = []
        try:
            for _ in range(30):
                s = socket.create_connection(('127.0.0.1', port), timeout=5)
                s.sendall(b'GET /healthz HTTP/1.1\r\n')   # заголовки не закрыты
                hung.append(s)
            time.sleep(0.5)
            # 30 соединений с одного адреса, потолок на адрес 3
            assert threading.active_count() < 30
        finally:
            for s in hung:
                s.close()

    def test_slots_are_returned_after_completed_requests(self, server):
        """Слоты не должны утекать: сервер обязан пережить много запросов."""
        srv, port = server
        for _ in range(40):
            assert '200' in _raw_get(port)
        assert _wait_drained(srv), f'слоты утекли: {srv._owner} / {srv._per_ip}'

    def test_rejected_connection_does_not_leak_a_slot(self, server):
        srv, port = server
        hung = []
        try:
            for _ in range(10):
                s = socket.create_connection(('127.0.0.1', port), timeout=5)
                s.sendall(b'GET /healthz HTTP/1.1\r\n')
                hung.append(s)
            time.sleep(0.3)
            with srv._lock:
                assert srv._per_ip.get('127.0.0.1', 0) <= 3
        finally:
            for s in hung:
                s.close()


class TestMetricsExposure:
    def test_metrics_open_on_loopback(self, server, monkeypatch):
        monkeypatch.setattr(bot, 'HEALTH_BIND_IS_PUBLIC', False)
        srv, port = server
        assert '200' in _raw_get(port, '/metrics')

    def test_metrics_closed_on_public_bind_without_token(self, server, monkeypatch):
        monkeypatch.setattr(bot, 'HEALTH_BIND_IS_PUBLIC', True)
        monkeypatch.setattr(bot, 'HEALTH_METRICS_TOKEN', '')
        srv, port = server
        assert '404' in _raw_get(port, '/metrics')

    def test_metrics_need_correct_token_on_public_bind(self, server, monkeypatch):
        monkeypatch.setattr(bot, 'HEALTH_BIND_IS_PUBLIC', True)
        monkeypatch.setattr(bot, 'HEALTH_METRICS_TOKEN', 'sekret')
        srv, port = server
        assert '404' in _raw_get(port, '/metrics')
        assert '404' in _raw_get(port, '/metrics', b'Authorization: Bearer nope\r\n')
        assert '200' in _raw_get(port, '/metrics', b'Authorization: Bearer sekret\r\n')

    def test_liveness_stays_open_on_public_bind(self, server, monkeypatch):
        """Health-check платформы не должен требовать токен."""
        monkeypatch.setattr(bot, 'HEALTH_BIND_IS_PUBLIC', True)
        monkeypatch.setattr(bot, 'HEALTH_METRICS_TOKEN', '')
        srv, port = server
        for path in ('/', '/livez', '/healthz', '/readyz'):
            assert '200' in _raw_get(port, path)


class TestPrioritizeNewsIsCheap:
    @staticmethod
    def _history(now, count=400):
        rows = [{'at': (now - timedelta(minutes=3 * (count - i))).isoformat(),
                 'subject': 'Naruto' if i % 4 else f'Series {i % 30}',
                 'title': f'history {i}'} for i in range(count)]
        return MagicMock(_items=rows)

    def test_adaptive_multiplier_computed_once_per_call(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(bot, 'story_history', self._history(now))
        calls = []
        original = bot._adaptive_diversity_multiplier
        monkeypatch.setattr(bot, '_adaptive_diversity_multiplier',
                            lambda *a, **k: (calls.append(1), original(*a, **k))[1])
        items = [{'title': f'Series {i % 8} new season', 'link': f'https://x/{i}',
                  'summary': 's', 'source': 'S', 'images': ['i']} for i in range(40)]
        bot._prioritize_news(items)
        assert len(calls) == 1, f'множитель пересчитан {len(calls)} раз'

    def test_score_computed_once_per_item(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(bot, 'story_history', self._history(now))
        calls = []
        original = bot._news_priority_score
        monkeypatch.setattr(bot, '_news_priority_score',
                            lambda n: (calls.append(1), original(n))[1])
        items = [{'title': f'Series {i % 8} trailer', 'link': f'https://x/{i}',
                  'summary': 's', 'source': 'S', 'images': ['i']} for i in range(40)]
        bot._prioritize_news(items)
        assert len(calls) == 40, f'score посчитан {len(calls)} раз вместо 40'

    def test_nothing_is_dropped_or_duplicated(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(bot, 'story_history', self._history(now))
        items = [{'title': f'Naruto chapter {i}', 'link': f'https://x/{i}',
                  'summary': 's', 'source': 'S', 'images': ['i']} for i in range(25)]
        out = bot._prioritize_news(list(items))
        assert len(out) == 25
        assert {x['link'] for x in out} == {x['link'] for x in items}

    def test_breaking_news_ignores_diversity_penalty(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(bot, 'story_history', self._history(now))
        items = [{'title': 'Naruto side story', 'link': 'https://x/plain',
                  'summary': 's', 'source': 'S', 'images': ['i'], '_llm_subject': 'Naruto'}
                 for _ in range(1)]
        items += [{'title': f'Naruto filler {i}', 'link': f'https://x/f{i}', 'summary': 's',
                   'source': 'S', 'images': ['i'], '_llm_subject': 'Naruto'} for i in range(6)]
        items.append({'title': 'Naruto movie announced', 'link': 'https://x/breaking',
                      'summary': 's', 'source': 'S', 'images': ['i'],
                      '_llm_subject': 'Naruto', '_breaking_news': True})
        out = bot._prioritize_news(items)
        breaking_pos = [i for i, x in enumerate(out) if x['link'] == 'https://x/breaking'][0]
        assert breaking_pos < len(out) - 1


class TestDashboardBruteForce:
    """Basic auth без тормозов перебирается тысячами попыток в секунду."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        bot._dashboard_failures.clear()
        monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', 'correct-horse-battery-staple')
        monkeypatch.setattr(bot, 'DASHBOARD_USER', 'admin')
        monkeypatch.setattr(bot, 'DASHBOARD_FAIL_DELAY_SEC', 0.0)
        monkeypatch.setattr(bot, 'DASHBOARD_FAIL_LIMIT', 5)
        monkeypatch.setattr(bot, 'DASHBOARD_FAIL_WINDOW_SEC', 60)
        yield
        bot._dashboard_failures.clear()

    def test_address_is_blocked_after_repeated_failures(self):
        ip = '203.0.113.9'
        for _ in range(4):
            bot._dashboard_note_failure(ip)
        assert not bot._dashboard_blocked(ip)
        bot._dashboard_note_failure(ip)
        assert bot._dashboard_blocked(ip)

    def test_block_is_per_address(self):
        for _ in range(6):
            bot._dashboard_note_failure('203.0.113.9')
        assert bot._dashboard_blocked('203.0.113.9')
        assert not bot._dashboard_blocked('203.0.113.10')

    def test_block_expires_after_window(self):
        ip = '203.0.113.9'
        for _ in range(6):
            bot._dashboard_note_failure(ip, now=1000.0)
        assert bot._dashboard_blocked(ip, now=1000.0)
        assert not bot._dashboard_blocked(ip, now=1000.0 + 61)

    def test_success_clears_the_counter(self):
        ip = '203.0.113.9'
        for _ in range(4):
            bot._dashboard_note_failure(ip)
        bot._dashboard_note_success(ip)
        assert bot._dashboard_failures.get(ip) is None

    def test_failure_table_does_not_grow_without_bound(self):
        for i in range(700):
            bot._dashboard_note_failure(f'198.51.100.{i % 256}.{i}', now=1000.0)
        bot._dashboard_note_failure('203.0.113.1', now=1000.0 + 600)
        assert len(bot._dashboard_failures) < 700

    def test_dashboard_returns_429_when_blocked(self, server, monkeypatch):
        srv, port = server
        monkeypatch.setattr(bot, 'DASHBOARD_FAIL_LIMIT', 3)
        for _ in range(3):
            assert '401' in _raw_get(port, '/admin',
                                     b'Authorization: Basic YWRtaW46bm9wZQ==\r\n')
        # Дальше credentials даже не проверяются, включая верные.
        good = base64.b64encode(b'admin:correct-horse-battery-staple').decode()
        resp = _raw_get(port, '/admin', f'Authorization: Basic {good}\r\n'.encode())
        assert '429' in resp

    def test_correct_password_works_below_the_limit(self, server):
        srv, port = server
        assert '401' in _raw_get(port, '/admin',
                                 b'Authorization: Basic YWRtaW46bm9wZQ==\r\n')
        good = base64.b64encode(b'admin:correct-horse-battery-staple').decode()
        assert '200' in _raw_get(port, '/admin', f'Authorization: Basic {good}\r\n'.encode())


class TestPlatformProbesAlwaysSucceed:
    """Провалившаяся health-проба = SIGTERM от платформы = перезапуск.

    Так и вышло: контейнер жил по 15 секунд, `run_polling` возвращался
    штатно (PTB так реагирует на SIGTERM), в lifecycle писалось
    `polling_returned` — и ни одной ошибки в логе.
    """

    def test_head_requests_are_supported(self, server):
        srv, port = server
        assert '200' in _raw_get(port, '/healthz', method='HEAD')
        assert '200' in _raw_get(port, '/', method='HEAD')

    def test_options_requests_are_supported(self, server):
        # Некоторые PaaS/прокси могут предварительно проверять HTTP endpoint
        # методом OPTIONS. Он не должен превращать живой процесс в 501.
        srv, port = server
        assert '200' in _raw_get(port, '/healthz', method='OPTIONS')
        assert '200' in _raw_get(port, '/', method='OPTIONS')

    def test_server_can_rebind_after_restart(self):
        assert bot._BoundedHealthServer.allow_reuse_address is True

    def test_unknown_liveness_paths_answer_200(self, server):
        srv, port = server
        for path in ('/health', '/ping', '/status', '/up', '/api/health'):
            assert '200' in _raw_get(port, path), f'{path} должен отвечать 200'

    def test_head_sends_no_body(self, server):
        srv, port = server
        resp = _raw_get(port, '/healthz', method='HEAD')
        head, _, body = resp.partition('\r\n\r\n')
        assert '200' in head
        assert body == '', 'на HEAD тело слать нельзя'

    def test_protected_paths_stay_protected(self, server, monkeypatch):
        """«Отвечаем на всё» не должно открыть метрики и дашборд."""
        monkeypatch.setattr(bot, 'HEALTH_BIND_IS_PUBLIC', True)
        monkeypatch.setattr(bot, 'HEALTH_METRICS_TOKEN', '')
        monkeypatch.setattr(bot, 'DASHBOARD_TOKEN', '')
        srv, port = server
        for path in ('/metrics', '/admin', '/admin/data.json'):
            assert '404' in _raw_get(port, path), f'{path} обязан оставаться закрытым'

    def test_probe_is_recorded_for_diagnostics(self, server, monkeypatch):
        bot._health_probe_seen.clear()
        srv, port = server
        _raw_get(port, '/some-platform-probe')
        assert any('/some-platform-probe' in k for k in bot._health_probe_seen)

    def test_liveness_never_touches_persistent_storage(self, server, monkeypatch):
        def storage_must_not_be_called():
            raise AssertionError('liveness must not probe persistent storage')

        monkeypatch.setattr(bot, '_storage_ready', storage_must_not_be_called)
        srv, port = server
        for path in ('/', '/healthz', '/livez', '/ping', '/some-platform-probe'):
            assert '200' in _raw_get(port, path)

    def test_readyz_keeps_its_own_semantics(self, server):
        srv, port = server
        resp = _raw_get(port, '/readyz')
        assert '200' in resp
        assert 'ready' in resp or 'not_ready' in resp
