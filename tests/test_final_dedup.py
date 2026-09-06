"""Тесты: работоспособность меню настроек и дедуп по готовому тексту поста."""
import asyncio
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import anime_news_bot
from anime_news_bot import PublishedTexts


def _full_settings(**over):
    base = dict(require_image=True, post_max_age_hours=24, thread_mode=True,
                translator_engine='deepl', quiet_mode=False, video_enabled=True,
                open_moderation=True, image_dedup=True, dedup_final_text=True,
                auto_disable_sources=True, daily_backup=True, startup_report=True,
                llm_enabled=True, llm_rewrite=True, llm_filter=True, llm_tags=True,
                llm_read_article=True, llm_skip_filler=True, llm_dedup_subject=True,
                llm_limit_repeats=True, is_source_enabled=lambda n: True)
    base.update(over)
    return MagicMock(**base)


class TestSettingsMenuWorks:
    """Кнопка «Настройки» перестала работать после рефакторинга меню:
    массовая замена занесла в две функции переменную, которой там нет."""

    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings', _full_settings())
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('S1', None)])
        monkeypatch.setattr(anime_news_bot, 'is_admin', lambda u: True)

    def test_command_opens_menu(self):
        upd = MagicMock()
        upd.message.reply_text = AsyncMock()
        asyncio.run(anime_news_bot.settings_command(upd, MagicMock()))
        markup = upd.message.reply_text.await_args.kwargs['reply_markup']
        assert markup.inline_keyboard

    def test_reply_button_opens_menu(self):
        upd = MagicMock()
        upd.message = MagicMock(text='⚙️ Настройки')
        upd.message.reply_text = AsyncMock()
        upd.effective_chat = MagicMock(type='private')
        asyncio.run(anime_news_bot.reply_button_handler(upd, MagicMock()))
        assert upd.message.reply_text.await_args.kwargs.get('reply_markup')

    def test_no_undefined_data_variable(self):
        """_menu_for(data) должна вызываться только там, где data существует."""
        source = Path(anime_news_bot.__file__).read_text(encoding='utf-8')
        for match in re.finditer(r'_menu_for\(data\)', source):
            head = source[:match.start()]
            func_start = max(head.rfind('\ndef '), head.rfind('\nasync def '))
            body = source[func_start:match.start()]
            assert 'data = ' in body or 'data: str' in body, \
                'вызов _menu_for(data) в функции без переменной data'


class TestFinalTextDedup:
    """Одна новость с двух сайтов приходит разными формулировками и совпадает
    только после перевода — прежние дедупы работают до него."""

    def test_catches_real_case(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('Аниме по манге «FX Воин Куруми» выйдет в октябре.\n\n'
               'Премьера состоится в октябре этого года.')
        assert pt.find_similar('Аниме по манге FX Fighter Kurumi-chan выйдет в октябре.')

    def test_transliteration_matched(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('Вышел трейлер «Куруми-тян»')
        assert pt.find_similar('Опубликован трейлер Kurumi-chan')

    @pytest.mark.parametrize('other', [
        'Раскрыт актёрский состав «Атаки титанов»',
        'Chainsaw Man получит второй сезон весной 2027',
        'Опенинг финальной части Bleach записал jo0ji',
    ])
    def test_different_news_pass(self, tmp_path, other):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('Вышел трейлер второго сезона «Атаки титанов»')
        assert pt.find_similar(other) is None

    def test_short_text_ignored(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('Аниме вышло')
        assert len(pt) == 0
        assert pt.find_similar('Аниме вышло') is None

    def test_empty_safe(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('')
        assert len(pt) == 0 and pt.find_similar('') is None

    def test_persists(self, tmp_path):
        p = tmp_path / 'p.json'
        PublishedTexts(p).add('Аниме по манге «FX Воин Куруми» выйдет в октябре')
        assert PublishedTexts(p).find_similar(
            'Аниме по манге FX Fighter Kurumi-chan выйдет в октябре')

    def test_old_entries_pruned(self, tmp_path):
        p = tmp_path / 'p.json'
        p.write_text(json.dumps([{'w': ['kurumi'], 't': 'старое',
                                  'ts': time.time() - 500 * 3600}]))
        assert len(PublishedTexts(p)) == 0

    def test_capped(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        for i in range(anime_news_bot.PUBLISHED_TEXT_MAX + 30):
            pt.add(f'Новость номер {i} про какой-то интересный тайтл сегодня')
        assert len(pt) <= anime_news_bot.PUBLISHED_TEXT_MAX

    def test_corrupt_file_safe(self, tmp_path):
        p = tmp_path / 'p.json'
        p.write_text('не json')
        assert len(PublishedTexts(p)) == 0

    def test_only_first_line_compared(self, tmp_path):
        pt = PublishedTexts(tmp_path / 'p.json')
        pt.add('Заголовок про Kurumi-chan сегодня\n\nСовсем другой второй абзац')
        # различия во втором абзаце не должны мешать
        assert pt.find_similar('Заголовок про Kurumi-chan сегодня\n\nТретий текст')


class TestFinalDedupInPipeline:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        for name, val in (('LLM_API_KEY', 'k'), ('LLM_BASE_URL', 'u'),
                          ('LLM_MODEL', 'm'), ('LLM_MIN_INTERVAL', 0),
                          ('_llm_disabled_runtime', False), ('_llm_fail_streak', 0)):
            monkeypatch.setattr(anime_news_bot, name, val)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            anime_news_bot.BotSettings(tmp_path / 'c.json'))
        monkeypatch.setattr(anime_news_bot, 'recent_subjects',
                            anime_news_bot.RecentSubjects(tmp_path / 's.json'))
        monkeypatch.setattr(anime_news_bot, 'published_texts',
                            PublishedTexts(tmp_path / 'p.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        # Реестр историй и журнал ссылок общие на весь прогон и копят записи в
        # памяти: чужая история делала первую новость «обновлением» уже
        # известной, путь менялся, и дедуп второй новости не срабатывал.
        # В одиночку тест при этом проходил.
        monkeypatch.setattr(anime_news_bot, 'story_registry',
                            anime_news_bot.StoryRegistry(tmp_path / 'stories.json'))
        monkeypatch.setattr(anime_news_bot, 'story_history', None)
        monkeypatch.setattr(anime_news_bot, 'source_intelligence', None)
        monkeypatch.setattr(anime_news_bot, 'stats',
                            MagicMock(record_skipped=AsyncMock()))
        # Переводчик тоже общий на прогон, а первая новость резервирует свой
        # готовый текст именно через него. С чужим переводчиком текст первой
        # новости получался другим, и вторая с ним уже не совпадала — дедуп
        # «не срабатывал», хотя ломался не он. Тест задавал переводчик только
        # перед второй новостью, то есть слишком поздно.
        class _Identity:
            def translate(self, text, input_limit=None):
                return text

        monkeypatch.setattr(anime_news_bot, 'translator', _Identity())
        monkeypatch.setattr(anime_news_bot, 'anilist', MagicMock(lookup=lambda q: None))
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        return anime_news_bot

    def _run(self, env, answer, news):
        env._llm_fail_streak = 0
        reply = MagicMock(status_code=200, text='', json=lambda: {
            'choices': [{'message': {'content': json.dumps(answer, ensure_ascii=False)}}]})
        with patch.object(env.requests, 'post', return_value=reply), \
             patch.object(env, 'fetch_article', return_value={'text': '', 'video': None}):
            return asyncio.run(env._prepare_news_for_send(news, 'X'))

    def test_second_source_blocked(self, env):
        answer = {'topic': 'аниме', 'kind': 'новость', 'subject': 'FX Senshi Kurumi-chan',
                  'title': 'Аниме по манге «FX Воин Куруми» выйдет в октябре',
                  'summary': '', 'tags': []}
        first = {'title': 'FX Fighter Kurumi-chan anime', 'summary': 'x' * 300,
                 'link': 'https://a/1', 'source': 'ANN'}
        assert self._run(env, answer, first) is None
        env._commit_image_fingerprint(first)

        # второй пост идёт без модели — subject не заполнится
        env.settings.llm_enabled = False

        class FakeTr:
            def translate(self, t, input_limit=None):
                return t
        env.translator = FakeTr()
        env.anilist = MagicMock(lookup=lambda q: None)
        env._translation_cache = {}
        env.DEEPL_API_KEY = ''
        second = {'title': 'Аниме по манге FX Fighter Kurumi-chan выйдет в октябре',
                  'summary': '', 'link': 'https://b/2', 'source': 'CBR',
                  'published_parsed': None, 'lang': 'ru'}
        assert self._run(env, {}, second) == 'skipped_dup'

    def test_text_saved_only_after_publish(self, env):
        answer = {'topic': 'аниме', 'kind': 'новость', 'subject': 'X',
                  'title': 'Какой-то интересный заголовок новости', 'summary': '',
                  'tags': []}
        news = {'title': 'T', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'X'}
        self._run(env, answer, news)
        assert len(env.published_texts) == 0        # публикации ещё не было
        env._commit_image_fingerprint(news)
        assert len(env.published_texts) == 1

    def test_can_be_disabled(self, env):
        env.settings.dedup_final_text = False
        answer = {'topic': 'аниме', 'kind': 'новость', 'subject': '',
                  'title': 'Одинаковый заголовок про Kurumi-chan', 'summary': '',
                  'tags': []}
        first = {'title': 'A', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'X'}
        self._run(env, answer, first)
        env._commit_image_fingerprint(first)
        second = {'title': 'B', 'summary': 'y' * 300, 'link': 'https://b/2', 'source': 'Y'}
        assert self._run(env, answer, second) is None

    def test_toggle_in_menu(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings', _full_settings())
        monkeypatch.setattr(anime_news_bot, 'SOURCES', [('S', None)])
        cbs = [b.callback_data
               for row in anime_news_bot._menu_posts().inline_keyboard for b in row]
        assert 'settings:toggle_finaldedup' in cbs
