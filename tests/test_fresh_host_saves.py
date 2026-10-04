"""Первая запись на только что поднятом контейнере не откладывается.

time.monotonic() отсчитывается от загрузки машины. Счётчик «последней записи»
начинался с 0, и при аптайме меньше минуты первая запись пропускалась: на CI
(свежая виртуалка) кеш AniList не переживал перезапуск. Хостинг тоже
поднимает контейнер заново при каждом деплое.
"""
import json

import anime_news_bot as bot

FRESH_UPTIME = 5.0     # секунд с загрузки машины


def test_anilist_first_answer_is_saved_on_fresh_host(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.time, 'monotonic', lambda: FRESH_UPTIME)
    monkeypatch.setattr(bot.AniListClient, '_query_api',
                        lambda self, search, manga=False: None)
    path = tmp_path / 'anilist.json'
    bot.AniListClient(path).lookup('Пример без ответа')
    assert path.exists() and json.loads(path.read_text(encoding='utf-8'))


def test_moderation_log_first_write_is_not_deferred_on_fresh_host(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.time, 'monotonic', lambda: FRESH_UPTIME)
    path = tmp_path / 'mod.json'
    store = bot.ChatModerationStore(path)
    path.unlink(missing_ok=True)
    store._save_soon()
    assert path.exists()
