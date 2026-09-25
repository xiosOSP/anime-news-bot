"""HTTP for untrusted article URLs: validate and connect to the same addresses."""
import ipaddress
import socket
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


# Well-known префикс NAT64 (RFC 6052): шлюз провайдера достраивает пакет до
# IPv4 из последних 32 бит. Python считает весь префикс «глобальным», поэтому
# 64:ff9b::a9fe:a9fe проходил проверку и вёл на 169.254.169.254 (metadata).
_NAT64_WKP = ipaddress.ip_network('64:ff9b::/96')


def _embedded_ipv4(ip):
    """IPv4, до которого на самом деле дойдёт соединение, или None.

    IPv4-mapped (::ffff:a.b.c.d) ядро отправляет прямо по IPv4, NAT64 и 6to4
    разворачивает шлюз или relay, устаревшие IPv4-compatible (::a.b.c.d) —
    туннель. Проверять по флагам самого IPv6 мало: решает вложенный адрес.
    """
    if ip.version != 6:
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64_WKP or int(ip) >> 32 == 0:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip) -> bool:
    """Можно ли ходить на этот адрес с недоверенным URL."""
    inner = _embedded_ipv4(ip)
    if inner is not None and not is_public_ip(inner):
        return False
    # Для IPv4-mapped и NAT64 решает вложенный адрес: префикс-обёртка у них
    # формально «reserved», но за ним обычный публичный IPv4 — на хосте с
    # DNS64 так выглядит любой сайт без IPv6, и запрещать его нельзя.
    if inner is not None and (ip.ipv4_mapped is not None or ip in _NAT64_WKP):
        return True
    # is_global мало: в reserved (::/8, 240.0.0.0/4) лежат и устаревшие
    # IPv4-compatible адреса, и то, что сеть может однажды начать маршрутизировать.
    return not (not ip.is_global or ip.is_multicast or ip.is_private
                or ip.is_reserved or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified)


def public_addresses(url: str, max_chars: int = 4096) -> tuple:
    if not isinstance(url, str) or not url or len(url) > max_chars:
        raise ValueError('Invalid URL length')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Only HTTP(S) URLs are allowed')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('URL credentials are not allowed')
    host = parsed.hostname.rstrip('.')
    if '%' in host:
        raise ValueError('Scoped addresses are not allowed')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for family, socktype, proto, _, address in rows:
        ip = ipaddress.ip_address(address[0])
        if not is_public_ip(ip):
            raise ValueError('Non-public destination')
        entry = (family, socktype, proto, address)
        if entry not in addresses:
            addresses.append(entry)
    if not addresses:
        raise ValueError('No public addresses')
    return tuple(addresses)


def _pinned_adapter(addresses):
    class PinnedConnection:
        def _new_conn(self):
            last_error = None
            for family, socktype, proto, address in addresses:
                sock = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    sock.settimeout(self.timeout)
                    for option in self.socket_options or ():
                        sock.setsockopt(*option)
                    if self.source_address:
                        sock.bind(self.source_address)
                    # No second DNS lookup. HTTPSConnection still uses the original
                    # hostname for SNI and certificate validation.
                    sock.connect(address)
                    return sock
                except OSError as exc:
                    last_error = exc
                    if sock is not None:
                        sock.close()
            if isinstance(last_error, socket.timeout):
                raise ConnectTimeoutError(self, 'Public destination timed out') from last_error
            raise NewConnectionError(self, str(last_error)) from last_error

    class PinnedHTTP(PinnedConnection, HTTPConnection):
        pass

    class PinnedHTTPS(PinnedConnection, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTP

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPS

    adapter = HTTPAdapter(max_retries=0)
    adapter.poolmanager.pool_classes_by_scheme = {'http': HTTPPool, 'https': HTTPSPool}
    return adapter


def public_get(url, *, headers=None, timeout=15, proxies=None,
               allow_redirects=False, stream=False):
    if allow_redirects or proxies:
        raise requests.exceptions.InvalidURL('Public requests require validated redirects and direct connections')
    try:
        addresses = public_addresses(url)
    except (ValueError, OSError) as exc:
        raise requests.exceptions.InvalidURL('Non-public or invalid destination') from exc
    with requests.Session() as session:
        # A proxy would resolve the hostname itself and bypass the pinned socket.
        session.trust_env = False
        adapter = _pinned_adapter(addresses)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session.get(url, headers=headers, timeout=timeout,
                           allow_redirects=False, stream=stream)
