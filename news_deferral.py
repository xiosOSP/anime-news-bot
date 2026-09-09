"""Durable, bounded LLM waiting: a restart must not restart the wait."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time


class NewsDeferralStore:
    MAX_ITEMS = 500
    TTL = 3 * 86400

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items = {}
        self.storage_error = False
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('items'), dict):
                raise ValueError('invalid deferral state')
            if len(data['items']) > self.MAX_ITEMS:
                raise ValueError('oversized deferral state')
            for key, row in data['items'].items():
                if (not isinstance(row, dict) or type(row.get('attempts')) is not int or row['attempts'] < 0
                        or type(row.get('first_at')) not in (int, float)
                        or not math.isfinite(row['first_at']) or row['first_at'] <= 0):
                    raise ValueError('invalid deferral entry')
                self._items[key] = row
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            # Deferral is optional enrichment, not permission to publish. A bad
            # file disables waiting, leaving the ordinary publication filters.
            self.storage_error = True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.news-defer-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'schema_version': 1, 'items': self._items}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reserve(self, identity, known_attempts, max_attempts, max_age):
        """Return (wait, attempts); never clear active waits to admit new ones."""
        if self.storage_error or max_attempts <= 0:
            return False, known_attempts
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if now - v['first_at'] <= self.TTL}
            row = self._items.get(key)
            if row is None and len(self._items) >= self.MAX_ITEMS:
                return False, known_attempts
            first_at = min(now, row['first_at']) if row else now
            seen = max(known_attempts, row['attempts'] if row else 0)
            if seen >= max_attempts or now - first_at >= max_age:
                return False, seen
            self._items[key] = {'attempts': seen + 1, 'first_at': first_at}
            try:
                self._save()
            except OSError:
                self.storage_error = True
                return False, seen
            return True, seen + 1
