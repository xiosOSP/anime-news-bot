"""Тесты: динамические источники (/addsource) и мультиадминка."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


import anime_news_bot
from anime_news_bot import BotSettings, CustomSources, _parse_addsource_args


class TestParseAddsourceArgs:
    def test_rss_with_label(self):
        assert _parse_addsource_args(['https://s.com/feed/', 'My', 'Site']) == \
            ('rss', 'https://s.com/feed/', 'My Site')

    def test_rss_label_from_host(self):
        assert _parse_addsource_args(['https://www.site.com/feed/']) == \
            ('rss', 'https://www.site.com/feed/', 'site.com')

    def test_tg_at(self):
        assert _parse_addsource_args(['@nexvlsz']) == ('tg', 'nexvlsz', 'TG: nexvlsz')

    def test_tg_tme_variants(self):
        assert _parse_addsource_args(['t.me/ytkanews'])[1] == 'ytkanews'
        assert _parse_addsource_args(['https://t.me/s/advance_emp', 'Adv']) == \
            ('tg', 'advance_emp', 'TG: Adv')

    def test_garbage_none(self):
        assert _parse_addsource_args(['мусор']) is None
        assert _parse_addsource_args([]) is None


class TestCustomSources:
    def test_add_dedup_persist_remove(self, tmp_path):
        p = tmp_path / 'cs.json'
        cs = CustomSources(p)
        assert cs.add('rss', 'https://x.com/f', 'X') is True
        assert cs.add('rss', 'https://y.com/f', 'x') is False
        cs2 = CustomSources(p)
        assert len(cs2.all()) == 1
        assert cs2.remove('x')['value'] == 'https://x.com/f'
        assert cs2.remove('X') is None

    def test_attach_no_duplicates(self):
        before = len(anime_news_bot.SOURCES)
        item = {'type': 'rss', 'value': 'https://z.com/f', 'label': '__TesT_Attach__'}
        anime_news_bot._attach_custom_source(item)
        anime_news_bot._attach_custom_source(item)
        added = [n for n, _ in anime_news_bot.SOURCES if n == '__TesT_Attach__']
        assert len(added) == 1
        anime_news_bot.SOURCES[:] = [(n, f) for n, f in anime_news_bot.SOURCES
                                     if n != '__TesT_Attach__']
        assert len(anime_news_bot.SOURCES) == before


class TestMultiAdmin:
    def test_add_remove_persist(self, tmp_path):
        p = tmp_path / 's.json'
        s = BotSettings(p)
        assert s.add_admin(111) is True
        assert s.add_admin(111) is False
        assert s.add_admin(anime_news_bot.ADMIN_ID) is False
        assert BotSettings(p).extra_admins == [111]
        assert s.remove_admin(111) is True
        assert s.remove_admin(111) is False

    def test_instances_isolated(self, tmp_path):
        s1 = BotSettings(tmp_path / 'a.json')
        s1.add_admin(111)
        s2 = BotSettings(tmp_path / 'b.json')
        assert s2.extra_admins == []  # deepcopy DEFAULTS — нет утечки

    def test_is_admin_extra(self, tmp_path, monkeypatch):
        s = BotSettings(tmp_path / 's.json')
        s.add_admin(111)
        monkeypatch.setattr(anime_news_bot, 'settings', s)
        upd = MagicMock()
        upd.effective_user.id = 111
        assert anime_news_bot.is_admin(upd) is True
        upd.effective_user.id = 222
        assert anime_news_bot.is_admin(upd) is False

    def test_notify_all_admins(self, tmp_path, monkeypatch):
        s = BotSettings(tmp_path / 's.json')
        s.add_admin(999)
        monkeypatch.setattr(anime_news_bot, 'settings', s)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        asyncio.run(anime_news_bot.notify_admin(bot, 'hi'))
        ids = {c.kwargs['chat_id'] for c in bot.send_message.await_args_list}
        assert ids == {anime_news_bot.ADMIN_ID, 999}


class TestAddDelSourceCommands:
    def test_addsource_attaches_and_reports(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'custom_sources',
                            CustomSources(tmp_path / 'cs.json'))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        before = len(anime_news_bot.SOURCES)
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock(args=['@testchan', 'Тест'])
        with patch.object(anime_news_bot, 'get_telegram_channel',
                          return_value=[{'title': 'x'}] * 3):
            asyncio.run(anime_news_bot.addsource_command(upd, ctx))
        assert anime_news_bot.SOURCES[-1][0] == 'TG: Тест'
        assert 'отдаёт 3' in upd.message.reply_text.await_args.args[0]
        ctx2 = MagicMock(args=['TG:', 'Тест'])
        asyncio.run(anime_news_bot.delsource_command(upd, ctx2))
        assert len(anime_news_bot.SOURCES) == before

    def test_delsource_builtin_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'custom_sources',
                            CustomSources(tmp_path / 'cs.json'))
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock(args=['Filmix'])
        asyncio.run(anime_news_bot.delsource_command(upd, ctx))
        assert 'встроенный' in upd.message.reply_text.await_args.args[0]
