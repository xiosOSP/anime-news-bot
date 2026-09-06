"""Тесты меню по разделам и автопаузы источников по времени."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import anime_news_bot
from anime_news_bot import SourceHealth


def _full_settings(**over):
    base = dict(require_image=True, post_max_age_hours=24, thread_mode=True,
                translator_engine='deepl', quiet_mode=False, video_enabled=True,
                open_moderation=True, image_dedup=True, auto_disable_sources=True,
                daily_backup=True, startup_report=True, llm_enabled=True,
                llm_rewrite=True, llm_filter=True, llm_tags=True,
                llm_read_article=True, llm_skip_filler=True,
                llm_dedup_subject=True, llm_limit_repeats=True,
                is_source_enabled=lambda n: True)
    base.update(over)
    return MagicMock(**base)


@pytest.fixture
def menu(monkeypatch):
    monkeypatch.setattr(anime_news_bot, 'settings', _full_settings())
    monkeypatch.setattr(anime_news_bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(anime_news_bot, 'LLM_BASE_URL', 'u')
    monkeypatch.setattr(anime_news_bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(anime_news_bot, 'SOURCES', [('S1', None), ('S2', None)])
    return anime_news_bot


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestRootMenu:
    def test_is_compact(self, menu):
        markup = menu.build_settings_menu()
        assert len(_labels(markup)) <= 8       # было 23 в один столбец

    def test_has_all_sections(self, menu):
        cbs = _callbacks(menu.build_settings_menu())
        for name in menu.SETTINGS_SECTIONS:
            assert f'settings:sec:{name}' in cbs

    def test_has_close(self, menu):
        assert 'settings:close' in _callbacks(menu.build_settings_menu())


class TestSections:
    @pytest.mark.parametrize('name', list(anime_news_bot.SETTINGS_SECTIONS))
    def test_every_section_opens(self, menu, name):
        text, markup = menu._section_view(name)
        assert menu.SETTINGS_SECTIONS[name] in text
        assert markup.inline_keyboard

    @pytest.mark.parametrize('name', list(anime_news_bot.SETTINGS_SECTIONS))
    def test_every_section_has_back(self, menu, name):
        _text, markup = menu._section_view(name)
        assert 'settings:back' in _callbacks(markup)

    def test_unknown_section_falls_back(self, menu):
        text, markup = menu._section_view('нет такого')
        assert 'Настройки' in text
        assert 'settings:sec:posts' in _callbacks(markup)

    def test_no_duplicate_buttons_across_sections(self, menu):
        seen = {}
        for name in menu.SETTINGS_SECTIONS:
            for cb in _callbacks(menu._section_view(name)[1]):
                if cb == 'settings:back':
                    continue
                assert cb not in seen, f'{cb} есть и в {seen.get(cb)}, и в {name}'
                seen[cb] = name

    def test_llm_section_hides_details_when_off(self, menu):
        menu.settings.llm_enabled = False
        cbs = _callbacks(menu._section_view('llm')[1])
        assert 'settings:toggle_llm' in cbs
        assert 'settings:toggle_llm_tags' not in cbs   # подпункты скрыты

    def test_llm_section_without_key(self, menu, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'LLM_API_KEY', '')
        cbs = _callbacks(menu._section_view('llm')[1])
        assert 'settings:llm_help' in cbs

    def test_sources_section_shows_count(self, menu):
        text = ' '.join(_labels(menu._section_view('sources')[1]))
        assert 'из 2' in text


class TestStayInSection:
    """После переключения тумблера остаёмся в своём разделе, а не в корне."""

    @pytest.mark.parametrize('toggle,section', [
        ('settings:toggle_thread', 'posts'),
        ('settings:toggle_dedup', 'media'),
        ('settings:toggle_llm_tags', 'llm'),
        ('settings:toggle_autodis', 'sources'),
        ('settings:toggle_backup', 'system'),
    ])
    def test_menu_for_returns_section(self, menu, toggle, section):
        expected = _callbacks(menu._SECTION_BUILDERS[section]())
        assert _callbacks(menu._menu_for(toggle)) == expected

    def test_unknown_toggle_falls_back_to_root(self, menu):
        assert _callbacks(menu._menu_for('settings:что-то')) == \
            _callbacks(menu.build_settings_menu())

    def test_every_toggle_mapped(self, menu):
        """Каждая кнопка раздела должна возвращать в свой раздел.

        Проверяем сам _menu_for, а не наличие ключа в _TOGGLE_SECTION: у него
        два способа найти раздел — по хвосту callback_data и по префиксу. Ключ
        в словаре был лишь одним из них, поэтому кнопка вида «llmslot:show»
        считалась непривязанной, хотя по префиксу раздел находился.
        """
        for name in menu.SETTINGS_SECTIONS:
            expected = _callbacks(menu._section_view(name)[1])
            for cb in expected:
                key = cb.split(':', 1)[-1]
                if key in ('back', 'close') or cb.startswith('settings:sec:'):
                    continue
                if key in ('llm_help', 'queue', 'history'):
                    continue
                assert _callbacks(menu._menu_for(cb)) == expected, (
                    f'{cb} возвращает не в свой раздел')


class TestCommandMenu:
    def test_trimmed(self):
        src = Path(anime_news_bot.__file__).read_text(encoding='utf-8')
        block = src[src.index('    commands = ['):]
        block = block[:block.index(']') + 1]
        assert 5 <= block.count('BotCommand(') <= 12

    def test_all_menu_commands_have_handlers(self):
        src = Path(anime_news_bot.__file__).read_text(encoding='utf-8')
        menu = set(re.findall(r'BotCommand\("([a-z_]+)"', src))
        handlers = set(re.findall(r'CommandHandler\("([a-z_]+)"', src))
        assert not (menu - handlers)

    def test_hidden_commands_still_work(self):
        src = Path(anime_news_bot.__file__).read_text(encoding='utf-8')
        handlers = set(re.findall(r'CommandHandler\("([a-z_]+)"', src))
        for cmd in ('tz', 'preview', 'backup', 'addsource', 'videocheck', 'cancel'):
            assert cmd in handlers


class TestAutoPauseByTime:
    """Пауза по времени тишины, а не по числу проверок: живой источник
    может законно молчать ночью или в выходной."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'source_health',
                            SourceHealth(tmp_path / 'sh.json'))
        anime_news_bot._auto_disabled_pending.clear()
        disabled = []
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(auto_disable_sources=True,
                                      is_source_enabled=lambda n: n not in disabled,
                                      toggle_source=lambda n: disabled.append(n)))
        return anime_news_bot, disabled

    def _silence(self, env, name, hours):
        env.source_health._entry(name)['silent_since'] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def test_quiet_night_not_punished(self, env):
        bot, disabled = env
        for hours in (2, 8, 16, 23):
            bot._note_source_failure('Quiet', 'нет новостей')
            self._silence(bot, 'Quiet', hours)
        assert disabled == []

    def test_disabled_after_day_of_silence(self, env):
        bot, disabled = env
        for _ in range(bot.AUTO_DISABLE_MIN_CHECKS):
            bot._note_source_failure('Dead', 'нет новостей')
            self._silence(bot, 'Dead', bot.AUTO_DISABLE_AFTER_HOURS + 1)
        bot._note_source_failure('Dead', '403 Forbidden')
        assert disabled == ['Dead']
        assert 'ч без новостей' in bot._auto_disabled_pending[0][1]

    def test_min_checks_guard(self, env):
        bot, disabled = env
        bot._note_source_failure('Once', 'сбой')
        self._silence(bot, 'Once', 100)
        assert disabled == []          # одной проверки мало

    def test_success_resets_silence(self, env):
        bot, _ = env
        bot._note_source_failure('S', 'сбой')
        self._silence(bot, 'S', 50)
        assert bot.source_health.silent_hours('S') > 49
        bot.source_health.record_ok('S', 3)
        assert bot.source_health.silent_hours('S') is None

    def test_manual_enable_resets(self, env, tmp_path):
        bot, _ = env
        bot._note_source_failure('S', 'сбой')
        self._silence(bot, 'S', 50)
        bot.source_health.reset('S')
        assert bot.source_health.silent_hours('S') is None

    def test_silence_survives_restart(self, tmp_path):
        p = tmp_path / 'sh.json'
        health = SourceHealth(p)
        health.record_fail('S', 'сбой')
        health._entry('S')['silent_since'] = (
            datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        health._save()
        assert SourceHealth(p).silent_hours('S') > 29

    def test_healthy_source_has_no_silence(self, tmp_path):
        health = SourceHealth(tmp_path / 'sh.json')
        health.record_ok('Good', 5)
        assert health.silent_hours('Good') is None

    def test_unknown_source(self, tmp_path):
        assert SourceHealth(tmp_path / 'sh.json').silent_hours('Нет') is None

    def test_broken_timestamp_safe(self, tmp_path):
        health = SourceHealth(tmp_path / 'sh.json')
        health.record_fail('S', 'сбой')
        health._entry('S')['silent_since'] = 'не дата'
        assert health.silent_hours('S') is None

    def test_setting_off_disables_feature(self, env, monkeypatch):
        bot, disabled = env
        bot.settings.auto_disable_sources = False
        for _ in range(6):
            bot._note_source_failure('Dead', 'сбой')
            self._silence(bot, 'Dead', 100)
        assert disabled == []
