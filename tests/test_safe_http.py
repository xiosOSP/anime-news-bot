"""Connection pinning must hold at the socket boundary, including HTTPS."""
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
import urllib3.connection

import anime_news_bot as bot
import safe_http


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
                                    '100.64.0.1', '::1', 'fc00::1', '224.0.0.1'])
def test_dns_private_and_mixed_answers_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))])
    with pytest.raises(ValueError):
        safe_http.public_addresses('http://news.invalid/feed')


def test_pinned_socket_does_not_resolve_again(monkeypatch):
    dns = MagicMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))])
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    addresses = safe_http.public_addresses('http://news.invalid/feed')
    dns.side_effect = AssertionError('second DNS lookup could rebind to localhost')
    sock = MagicMock()
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: sock)
    adapter = safe_http._pinned_adapter(addresses)
    conn = adapter.poolmanager.connection_from_url('http://news.invalid/feed')._new_conn()
    try:
        conn.connect()
        sock.connect.assert_called_once_with(('8.8.8.8', 80))
        assert dns.call_count == 1
    finally:
        conn.close()
        adapter.close()


def test_https_keeps_hostname_and_certificate_verification(monkeypatch):
    adapter = safe_http._pinned_adapter(((socket.AF_INET, socket.SOCK_STREAM, 6, ('8.8.8.8', 443)),))
    sock = MagicMock()
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: sock)
    wrap = MagicMock(return_value=SimpleNamespace(socket=sock, is_verified=True))
    monkeypatch.setattr(urllib3.connection, '_ssl_wrap_socket_and_match_hostname', wrap)
    conn = adapter.poolmanager.connection_from_url('https://news.invalid/feed')._new_conn()
    try:
        conn.connect()
        sock.connect.assert_called_once_with(('8.8.8.8', 443))
        assert wrap.call_args.kwargs['server_hostname'] == 'news.invalid'
        assert wrap.call_args.kwargs['cert_reqs'] == ssl.CERT_REQUIRED
        assert wrap.call_args.kwargs['assert_hostname'] is not False
    finally:
        conn.close()
        adapter.close()


def test_streaming_response_keeps_host_header_and_body(monkeypatch):
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hosts.append(self.headers['Host'])
            self.send_response(200)
            self.send_header('Content-Length', '7')
            self.end_headers()
            self.wfile.write(b'payload')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Only this integration fixture permits loopback, to exercise real HTTP.
        monkeypatch.setattr(safe_http, 'public_addresses', lambda url: (
            (socket.AF_INET, socket.SOCK_STREAM, 6, server.server_address),))
        monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
        with safe_http.public_get('http://news.invalid/feed', stream=True, timeout=2) as response:
            assert b''.join(response.iter_content(2)) == b'payload'
        assert hosts == ['news.invalid']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_redirect_cannot_connect_to_private_destination(monkeypatch):
    response = MagicMock(status_code=302, headers={'Location': 'http://127.0.0.1/private'})
    get = MagicMock(return_value=response)
    monkeypatch.setattr(bot, 'public_get', get)
    assert bot.http_get_public_with_retry('https://8.8.8.8/feed') is None
    assert get.call_count == 1
    response.close.assert_called_once()


def test_unvalidated_proxy_is_rejected():
    with pytest.raises(requests.exceptions.InvalidURL):
        safe_http.public_get('https://news.invalid/feed', proxies={'https': 'http://proxy.invalid'})
