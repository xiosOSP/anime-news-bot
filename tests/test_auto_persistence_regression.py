import asyncio
from types import SimpleNamespace

import anime_news_bot as bot


class FakeJob:
    def __init__(self, name):
        self.name = name
        self.removed = False
    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []
        self.calls = []
    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]
    def run_repeating(self, callback, *, interval, first, name, job_kwargs=None):
        self.calls.append({
            'callback': callback, 'interval': interval, 'first': first,
            'name': name, 'job_kwargs': job_kwargs,
        })
        job = FakeJob(name)
        self.jobs.append(job)
        return job


def test_auto_enabled_persists_across_settings_reload(tmp_path):
    path = tmp_path / 'bot_settings.json'
    s = bot.BotSettings(path)
    assert s.auto_enabled is False
    s.auto_enabled = True
    reloaded = bot.BotSettings(path)
    assert reloaded.auto_enabled is True


def test_ensure_auto_job_is_idempotent(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.check_interval_min = 17
    monkeypatch.setattr(bot, 'settings', settings)
    jq = FakeJobQueue()
    assert bot._ensure_auto_news_job(jq, first=5) is True
    assert len(jq.get_jobs_by_name('anime_news_check')) == 1
    assert jq.calls[0]['interval'] == 17 * 60
    assert bot._ensure_auto_news_job(jq, first=5) is False
    assert len(jq.get_jobs_by_name('anime_news_check')) == 1


def test_duplicate_auto_jobs_are_trimmed(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    monkeypatch.setattr(bot, 'settings', settings)
    jq = FakeJobQueue()
    jq.jobs = [FakeJob('anime_news_check'), FakeJob('anime_news_check')]
    assert bot._ensure_auto_news_job(jq) is False
    assert len(jq.get_jobs_by_name('anime_news_check')) == 1


def test_health_watchdog_restores_missing_auto_job(tmp_path, monkeypatch):
    settings = bot.BotSettings(tmp_path / 'settings.json')
    settings.auto_enabled = True
    monkeypatch.setattr(bot, 'settings', settings)
    jq = FakeJobQueue()

    class FakeBot:
        pass

    ctx = SimpleNamespace(application=SimpleNamespace(job_queue=jq), bot=FakeBot())
    monkeypatch.setattr(bot, '_check_channel_access', lambda _b: asyncio.sleep(0, result=(True, 'ok')))
    monkeypatch.setattr(bot, '_storage_ready', lambda: True)
    monkeypatch.setattr(bot, 'notify_admin', lambda *_a, **_k: asyncio.sleep(0, result=1))

    asyncio.run(bot.health_probe_job(ctx))
    assert len(jq.get_jobs_by_name('anime_news_check')) == 1
