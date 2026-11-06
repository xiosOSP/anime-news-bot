"""Аудит ноября 2026: надёжность и скорость.

Пересказанные случаи: пост в ветке, не доставленный до рестарта; проверка
повтора тайтла, которая с полной памятью тормозила цикл событий; кнопка
видео, которая в экономном режиме всегда писала «включено»; рестарты без
единой строки в логе.
"""
import json
import time

import anime_news_bot as bot


# ---------- память тайтлов: быстрый проход даёт тот же ответ ----------

def _works(tmp_path):
    return bot.WorkMemory(tmp_path / 'works.json')


def test_main_part_meets_full_name_both_ways(tmp_path):
    store = _works(tmp_path)
    store.remember(['name:ведьма', 'main:ведьма'], 'Ведьма: Рассвет', 'bot')
    assert store.seen(['main:ведьма'], 36)                      # main ↔ name
    store2 = bot.WorkMemory(tmp_path / 'w2.json')
    store2.remember(['main:ведьма'], 'Ведьма: Рассвет', 'bot')
    assert store2.seen(['name:ведьма'], 36)                     # name ↔ main


def test_two_main_parts_alone_do_not_meet(tmp_path):
    """Общая главная часть («Ведьма: …» и «Ведьма: …») — ещё не один тайтл."""
    store = _works(tmp_path)
    store.remember(['main:ведьма'], 'Ведьма: Рассвет', 'bot')
    assert store.seen(['main:ведьма'], 36) is None


def test_old_row_stops_the_search_but_fresh_one_is_found(tmp_path):
    store = _works(tmp_path)
    store.remember(['ru:старый'], 'Старый', 'bot')
    store._items[-1]['at'] = time.time() - 100 * 3600
    store.remember(['ru:новый'], 'Новый', 'bot')
    assert store.seen(['ru:новый'], 36)
    assert store.seen(['ru:старый'], 36) is None


def test_unsorted_file_is_sorted_on_load(tmp_path):
    """Записи в файле не по времени (часы хостинга шагнули назад): свежая
    запись после старой не должна теряться из-за раннего выхода."""
    now = time.time()
    rows = [{'keys': ['ru:свежий'], 'title': 'Свежий', 'by': 'bot', 'at': now - 60},
            {'keys': ['ru:старый'], 'title': 'Старый', 'by': 'bot', 'at': now - 100 * 3600}]
    (tmp_path / 'works.json').write_text(json.dumps(rows), encoding='utf-8')
    assert _works(tmp_path).seen(['ru:свежий'], 36)


# ---------- ветка: пост, не доставленный до рестарта ----------

def _pending_file(tmp_path, items):
    path = tmp_path / 'pending.json'
    path.write_text(json.dumps({'items': items}), encoding='utf-8')
    return path


def test_staging_without_thread_message_is_dropped_on_start(tmp_path):
    """Процесс умер до отправки в ветку: модераторы пост не видели — в канал
    он уйти не должен."""
    path = _pending_file(tmp_path, {
        'a': {'news': {'title': 'Не дошёл', 'link': 'https://e/1'}, 'ts': time.time(),
              'channel_state': 'staging'},
        'b': {'news': {'title': 'Дошёл', 'link': 'https://e/2'}, 'ts': time.time(),
              'channel_state': 'staging', 'preview': {'message_id': 5}},
    })
    store = bot.PendingPosts(path)
    assert store.get('a') is None
    assert store.get('b') is not None
    assert store._items['b']['channel_state'] == 'pending'
    # Снятие записано на диск: после второго рестарта пост не воскресает.
    assert 'a' not in json.loads(path.read_text(encoding='utf-8'))['items']


# ---------- кнопка видео в экономном режиме ----------

def test_video_button_shows_the_admin_setting_not_the_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_low_memory_mode', lambda: True)
    s = bot.BotSettings(tmp_path / 'settings.json')
    s.video_enabled = True
    assert s.video_enabled is False          # итог: в экономном режиме выкл
    assert s.video_setting is True           # а настройка админа — вкл
    s.video_enabled = not s.video_setting    # то, что делает кнопка
    assert s.video_setting is False


# ---------- пульс памяти и трасса падения ----------

def test_heartbeat_of_unclean_process_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    beat = {'at': '2026-11-06T03:04:05+00:00', 'rss_mb': 410, 'container_mb': 980,
            'container_max_mb': 1024, 'cycle': 'llm'}
    path.write_text(json.dumps({'state': 'running', 'heartbeat': beat}), encoding='utf-8')
    data = bot._mark_lifecycle_start()
    assert data['previous_heartbeat'] == beat
    assert 'heartbeat' not in data
    line = bot._heartbeat_line(data['previous_heartbeat'])
    assert '03:04:05' in line and 'RSS 410' in line and '980/1024' in line and 'llm' in line


def test_heartbeat_after_clean_stop_is_not_reported(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    path.write_text(json.dumps({'state': 'stopped', 'heartbeat': {'at': 'x', 'rss_mb': 1}}),
                    encoding='utf-8')
    assert bot._mark_lifecycle_start()['previous_heartbeat'] is None


def test_heartbeat_is_written_only_by_the_owner(tmp_path, monkeypatch):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    bot._mark_lifecycle_start()
    monkeypatch.setattr(bot, '_cgroup_memory_mb', lambda: {'current': 300, 'max': 1024})
    bot._write_heartbeat()
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['heartbeat']['container_mb'] == 300
    # Чужой pid (второй процесс на том же томе) пульс не перетирает.
    data['pid'] = 1
    data.pop('heartbeat')
    path.write_text(json.dumps(data), encoding='utf-8')
    bot._write_heartbeat()
    assert 'heartbeat' not in json.loads(path.read_text(encoding='utf-8'))


def test_crash_trace_returns_the_previous_crash_and_starts_clean(tmp_path, monkeypatch):
    trace = tmp_path / 'crash_trace.log'
    trace.write_text('Fatal Python error: Segmentation fault\n  File "x.py", line 1', encoding='utf-8')
    monkeypatch.setattr(bot, 'CRASH_TRACE_FILE', trace)
    monkeypatch.setattr(bot.faulthandler, 'enable', lambda *a, **k: None)
    previous = bot._enable_crash_trace()
    assert 'Segmentation fault' in previous
    assert trace.read_text(encoding='utf-8') == ''           # новый запуск — с чистого листа
    assert bot._enable_crash_trace() == ''                   # пустой файл — не падение


def test_lifecycle_shows_heartbeat_line():
    import inspect
    assert '_heartbeat_line(' in inspect.getsource(bot.lifecycle_command)
