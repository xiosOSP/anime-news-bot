"""Optional chat-only LLM client; it never borrows news credentials or quota."""
import asyncio
from collections import OrderedDict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx


class ChatModelClient:
    def __init__(self, *, base_url, api_key, model, state_path, daily_limit=120,
                 token_budget=50000, timeout=12, min_interval=1.0, transport=None):
        self.base_url, self.api_key, self.model = base_url.rstrip('/'), api_key, model
        self.state_path = Path(state_path)
        self.daily_limit = max(0, min(5000, int(daily_limit)))
        self.token_budget = max(0, int(token_budget))
        self.timeout = max(3, min(60, int(timeout)))
        self.min_interval = max(0, min(60, float(min_interval)))
        self._transport = transport
        self._busy = False
        self._last_request = -3600.0
        self._cooldown = 0.0
        self._failures = 0
        self._json_mode = True
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self.last_error = ''
        self._storage_error = False
        self._state = {'day': '', 'requests': 0, 'tokens': 0}
        try:
            row = json.loads(self.state_path.read_text(encoding='utf-8'))
            if (not isinstance(row, dict) or not isinstance(row.get('day'), str)
                    or any(type(row.get(key)) is not int or row[key] < 0
                           for key in ('requests', 'tokens'))):
                raise ValueError('Invalid quota state')
            if date.fromisoformat(row['day']).isoformat() != row['day']:
                raise ValueError('Invalid quota date')
            self._state = dict(day=row['day'], requests=row['requests'], tokens=row['tokens'])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            # A corrupt quota file must not reset limits and start spending.
            self.last_error = 'storage'
            self._storage_error = True

    @property
    def configured(self):
        try:
            url = urlsplit(self.base_url)
            # Accessing port validates malformed/non-numeric and out-of-range ports.
            port = url.port
            return bool(self.api_key and self.model and url.scheme in ('https', 'http')
                        and url.hostname and not url.username and not url.password
                        and not url.query and not url.fragment and port != 0)
        except ValueError:
            return False

    def _reset_day(self):
        day = datetime.now(timezone.utc).date().isoformat()
        if self._state['day'] != day:
            self._state = dict(day=day, requests=0, tokens=0)

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.moderation-llm-', dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(self._state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _reserve(self, tokens):
        with self._lock:
            if self._storage_error:
                self.last_error = 'storage'
                return False
            self._reset_day()
            if self._state['requests'] >= self.daily_limit:
                self.last_error = 'quota'
                return False
            if self.token_budget and self._state['tokens'] + tokens > self.token_budget:
                self.last_error = 'token_budget'
                return False
            previous = dict(self._state)
            self._state['requests'] += 1
            self._state['tokens'] += tokens
            try:
                self._save()
            except OSError:
                self._state = previous
                self.last_error = 'storage'
                self._storage_error = True
                return False
            return True

    def _reconcile(self, reserved, used):
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            return
        with self._lock:
            previous = dict(self._state)
            self._state['tokens'] = max(0, self._state['tokens'] - reserved + used)
            try:
                self._save()
            except OSError:
                self._state = previous  # retain the conservative reservation
                self.last_error = 'storage'
                self._storage_error = True

    def snapshot(self):
        with self._lock:
            self._reset_day()
            return dict(self._state, model=self.model, configured=self.configured,
                        daily_limit=self.daily_limit, token_budget=self.token_budget,
                        cooldown_sec=max(0, math.ceil(self._cooldown - time.monotonic())),
                        error=self.last_error, busy=self._busy)

    @staticmethod
    def _retry_after(value):
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 60
        return max(1, min(86400, seconds)) if math.isfinite(seconds) else 60

    async def _http(self, messages, max_tokens):
        payload = dict(model=self.model, messages=messages, max_tokens=max_tokens,
                       temperature=0.1, stream=False)
        if self._json_mode:
            payload['response_format'] = {'type': 'json_object'}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=min(5, self.timeout)),
                                         transport=self._transport, follow_redirects=False) as client:
                async with client.stream('POST', self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.api_key}, json=payload) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        content.extend(chunk)
                        if len(content) > 128 * 1024:
                            return {'error': 'response_too_large'}
            if response.status_code == 429:
                return {'error': 'rate_limit', 'cooldown': self._retry_after(response.headers.get('Retry-After'))}
            if response.status_code in (401, 403):
                return {'error': 'auth', 'cooldown': 3600}
            if response.status_code in (400, 422) and self._json_mode and b'response_format' in content:
                return {'error': 'json_mode'}
            if response.status_code != 200:
                return {'error': f'http_{response.status_code}'}
            data = json.loads(content)
            text = data['choices'][0]['message']['content']
            if not isinstance(text, str) or not text.strip():
                return {'error': 'empty_response'}
            usage = data.get('usage', {})
            return {'text': text.strip(), 'tokens': usage.get('total_tokens') if isinstance(usage, dict) else None}
        except (httpx.HTTPError, OSError):
            return {'error': 'network'}
        except (ValueError, KeyError, TypeError, IndexError):
            return {'error': 'bad_response'}

    async def complete(self, messages, max_tokens=200):
        # One deadline includes durable reservation, the response body and the
        # optional JSON compatibility attempt. Cancellation reaches HTTPX itself.
        try:
            async with asyncio.timeout(self.timeout):
                return await self._complete(messages, max_tokens)
        except asyncio.TimeoutError:
            self.last_error = 'timeout'
            self._failures = min(5, self._failures + 1)
            self._cooldown = time.monotonic() + min(900, 30 * 2**self._failures)
            return None

    async def _complete(self, messages, max_tokens):
        if not self.configured:
            self.last_error = 'not_configured'
            return None
        if self._storage_error:
            self.last_error = 'storage'
            return None
        max_tokens = max(64, min(400, int(max_tokens)))
        cache_key = hashlib.sha256(json.dumps(
            [self.model, max_tokens, messages], ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(cache_key)
            return cached[1]
        if self._busy or time.monotonic() < self._cooldown:
            return None
        if time.monotonic() - self._last_request < self.min_interval:
            return None
        self._busy = True
        try:
            serialized = json.dumps(messages, ensure_ascii=False)
            if len(serialized) > 20000:
                self.last_error = 'input_too_large'
                return None
            reserved = len(serialized.encode('utf-8')) // 3 + max_tokens
            # Only an unsupported JSON option may cause a second attempt.
            for _ in range(2):
                if not await asyncio.to_thread(self._reserve, reserved):
                    return None
                self._last_request = time.monotonic()
                result = await self._http(messages, max_tokens)
                if result.get('text'):
                    await asyncio.to_thread(self._reconcile, reserved, result.get('tokens'))
                    self._failures = 0
                    if self.last_error != 'storage':
                        self.last_error = ''
                    self._cache[cache_key] = (time.monotonic() + 300, result['text'])
                    while len(self._cache) > 256:
                        self._cache.popitem(last=False)
                    return result['text']
                self.last_error = result.get('error', 'unknown')
                if self.last_error == 'json_mode' and self._json_mode:
                    self._json_mode = False
                    continue
                self._failures = min(5, self._failures + 1)
                self._cooldown = time.monotonic() + result.get('cooldown', min(900, 30 * 2**self._failures))
                return None
            return None
        finally:
            self._busy = False
