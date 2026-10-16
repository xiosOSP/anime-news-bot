"""Круг перезапусков на одном посте (октябрь 2026).

Пересказ случая: бот готовил пост с роликом из Telegram-канала, скачивал его,
и через секунды контейнер убивали без строки в логе. После рестарта сборщик
находил ту же новость, снова её готовил — и так 40 запусков подряд. Бот
знал, какой пост готовил в момент смерти (незакрытый резерв), но только
освобождал его.
"""
import asyncio
import json

import pytest

import anime_news_bot as bot

URL = 'https://t.me/s/somechannel/123'
TITLE = 'Трейлер второго сезона'


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, '_queue_admin_alert', sent.append)
    return sent


def _die_while_preparing(path, url=URL, title=TITLE):
    """Процесс берёт резерв и умирает; новый процесс читает тот же файл."""
    store = bot.SentLinksStore(path)
    assert asyncio.run(store.claim(url, title))
    return bot.SentLinksStore(path)


def test_two_deaths_on_one_post_quarantine_it(tmp_path, alerts):
    path = tmp_path / 'sent.json'
    after_first = _die_while_preparing(path)
    assert not after_first.is_rejected(URL)           # один раз — может быть передеплой
    after_second = _die_while_preparing(path)
    assert after_second.is_rejected(URL)
    assert not asyncio.run(after_second.claim(URL, TITLE))
    # Тот же текст под другой ссылкой (репост) тоже не берётся.
    assert not asyncio.run(after_second.claim('https://t.me/s/other/9', TITLE))
    assert alerts and TITLE in alerts[-1]


def test_release_means_preparation_survived(tmp_path, alerts):
    path = tmp_path / 'sent.json'
    store = _die_while_preparing(path)
    assert asyncio.run(store.claim(URL, TITLE))
    asyncio.run(store.release(URL, TITLE))           # сбой, но процесс жив
    after = _die_while_preparing(path)
    assert not after.is_rejected(URL)
    assert not alerts


def test_commit_clears_the_count(tmp_path, alerts):
    path = tmp_path / 'sent.json'
    store = _die_while_preparing(path)
    assert asyncio.run(store.claim(URL, TITLE))
    asyncio.run(store.commit(URL, TITLE))
    assert URL not in json.loads(path.read_text(encoding='utf-8'))['prep_crashes']


def test_old_death_does_not_count(tmp_path, alerts):
    """Смерть суточной давности и новая смерть на том же посте — не «подряд»."""
    path = tmp_path / 'sent.json'
    store = _die_while_preparing(path)
    assert asyncio.run(store.claim(URL, TITLE))      # второй процесс тоже умирает
    data = json.loads(path.read_text(encoding='utf-8'))
    for entry in data['prep_crashes'].values():
        entry['at'] -= bot.PREP_CRASH_MEMORY_SEC + 60
    path.write_text(json.dumps(data), encoding='utf-8')
    after = bot.SentLinksStore(path)
    assert not after.is_rejected(URL)


def test_other_posts_are_not_blamed(tmp_path, alerts):
    path = tmp_path / 'sent.json'
    _die_while_preparing(path, url='https://t.me/s/a/1', title='Первая новость')
    after = _die_while_preparing(path, url='https://t.me/s/b/2', title='Вторая новость')
    assert not after.is_rejected('https://t.me/s/a/1')
    assert not after.is_rejected('https://t.me/s/b/2')


def test_cgroup_counters_v2(tmp_path):
    (tmp_path / 'memory.events').write_text('low 0\nhigh 0\nmax 4\noom 2\noom_kill 3\n')
    (tmp_path / 'memory.current').write_text(str(300 * 1024 * 1024))
    (tmp_path / 'memory.peak').write_text(str(990 * 1024 * 1024))
    (tmp_path / 'memory.max').write_text(str(1024 * 1024 * 1024))
    assert bot._cgroup_oom_kills(tmp_path) == 3
    assert bot._cgroup_memory_mb(tmp_path) == {'current': 300, 'peak': 990, 'max': 1024}


def test_cgroup_counters_v1_and_no_limit(tmp_path):
    (tmp_path / 'memory').mkdir()
    (tmp_path / 'memory' / 'memory.oom_control').write_text(
        'oom_kill_disable 0\nunder_oom 0\noom_kill 1\n')
    (tmp_path / 'memory' / 'memory.limit_in_bytes').write_text('9223372036854771712')
    assert bot._cgroup_oom_kills(tmp_path) == 1
    assert bot._cgroup_memory_mb(tmp_path) == {}       # «без лимита» — не цифра лимита


@pytest.mark.parametrize('before_state, oom_before, oom_now, expected', [
    ('running', 2, 3, True),        # умер грязно, и ядро убило процесс за память
    ('running', 3, 3, False),       # умер грязно, но не за память
    ('stopped', 2, 3, False),       # штатный выход: OOM был у дочернего процесса
])
def test_lifecycle_start_names_oom(tmp_path, monkeypatch, before_state, oom_before, oom_now, expected):
    path = tmp_path / 'runtime_lifecycle.json'
    monkeypatch.setattr(bot, 'LIFECYCLE_FILE', path)
    path.write_text(json.dumps({'state': before_state, 'cgroup_oom_kills': oom_before}),
                    encoding='utf-8')
    monkeypatch.setattr(bot, '_cgroup_oom_kills', lambda *a: oom_now)
    data = bot._mark_lifecycle_start()
    assert data['oom_kill_on_last_exit'] is expected
    assert data['cgroup_oom_kills'] == oom_now
    assert ('Прошлый процесс убит' in bot._lifecycle_container_memory_line(data)) is expected


def test_main_enables_faulthandler():
    import inspect
    assert 'faulthandler.enable()' in inspect.getsource(bot.main)
