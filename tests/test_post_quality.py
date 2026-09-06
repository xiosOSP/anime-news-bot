"""Тесты качества постов: чтение статей, отсев наполнителя,
дедуп по предмету новости, лимит повторов."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot
from anime_news_bot import RecentSubjects, _extract_article_text, _looks_thin

ARTICLE_PAGE = '''<html><body>
<nav><a href="/">Home</a><a href="/anime">Anime</a></nav>
<aside><p>Related: 10 best anime you should watch right now, click here today</p></aside>
<article>
  <p>The final cour of Bleach: Thousand-Year Blood War premieres on October 4,
     and the opening theme is performed by jo0ji, known for his anime work.</p>
  <p>Studio Pierrot returns to handle the animation, with Tomohisa Taguchi
     directing the adaptation of Tite Kubo manga finale for the streaming service.</p>
  <p>Advertisement</p>
</article>
<footer><p>Subscribe to our newsletter for updates and follow us on social media</p></footer>
</body></html>'''


def _reply(payload):
    return MagicMock(status_code=200, text='',
                     json=lambda: {'choices': [{'message': {
                         'content': json.dumps(payload, ensure_ascii=False)}}]})


class TestArticleExtraction:
    def test_takes_body_drops_chrome(self):
        text = _extract_article_text(ARTICLE_PAGE)
        assert 'October 4' in text and 'Pierrot' in text
        assert 'Advertisement' not in text
        assert 'newsletter' not in text
        assert 'Home' not in text

    def test_empty_page(self):
        assert _extract_article_text('<html><body></body></html>') == ''

    def test_length_capped(self):
        page = '<article>' + '<p>' + 'слово ' * 3000 + '</p></article>'
        assert len(_extract_article_text(page)) <= anime_news_bot.ARTICLE_MAX_CHARS

    def test_broken_html_is_safe(self):
        assert isinstance(_extract_article_text('<div><p>текст'), str)

    @pytest.mark.parametrize('text,thin', [
        ('The anime will premiere in 2027. Read more...', True),
        ('', True),
        (' '.join(['слово'] * 30), False),
    ])
    def test_thin_detection(self, text, thin):
        assert _looks_thin(text) is thin


class TestFetchArticle:
    def _ok_response(self):
        return MagicMock(status_code=200, text=ARTICLE_PAGE,
                         headers={'Content-Type': 'text/html'})

    def test_fetches_and_caches(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        calls = []
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: calls.append(1) or self._ok_response())
        first = anime_news_bot.fetch_article_text('https://site.com/a')
        second = anime_news_bot.fetch_article_text('https://site.com/a')
        assert first == second and 'Pierrot' in first
        assert len(calls) == 1                    # второй раз из кэша

    def test_network_failure_safe(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: None)
        assert anime_news_bot.fetch_article_text('https://site.com/b') == ''

    def test_exception_safe(self, monkeypatch):
        anime_news_bot._article_cache.clear()

        def boom(*a, **k):
            raise OSError('down')
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', boom)
        assert anime_news_bot.fetch_article_text('https://site.com/c') == ''

    def test_non_html_skipped(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(
                                status_code=200, text='{}',
                                headers={'Content-Type': 'application/json'}))
        assert anime_news_bot.fetch_article_text('https://site.com/d') == ''

    @pytest.mark.parametrize('url', ['', 'не ссылка', 'ftp://x/y'])
    def test_bad_urls(self, url):
        assert anime_news_bot.fetch_article_text(url) == ''

    def test_cache_bounded(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: self._ok_response())
        for i in range(anime_news_bot.ARTICLE_CACHE_MAX + 10):
            anime_news_bot.fetch_article_text(f'https://site.com/{i}')
        assert len(anime_news_bot._article_cache) <= anime_news_bot.ARTICLE_CACHE_MAX + 1


class TestRecentSubjects:
    def test_key_normalises_variants(self, tmp_path):
        r = RecentSubjects(tmp_path / 's.json')
        a = r._key('Bleach: Thousand-Year Blood War')
        b = r._key('BLEACH Thousand Year Blood War')
        c = r._key('«Bleach» — thousand year blood war')
        assert a == b == c

    def test_same_news_detected(self, tmp_path):
        r = RecentSubjects(tmp_path / 's.json')
        r.add('Bleach: Thousand-Year Blood War', 'новость', 'Опенинг')
        assert r.seen_same_news('BLEACH Thousand Year Blood War', 'новость')

    def test_different_kind_not_duplicate(self, tmp_path):
        r = RecentSubjects(tmp_path / 's.json')
        r.add('Chainsaw Man', 'новость')
        assert not r.seen_same_news('Chainsaw Man', 'трейлер')

    def test_count_today(self, tmp_path):
        r = RecentSubjects(tmp_path / 's.json')
        for kind in ('новость', 'трейлер', 'анонс'):
            r.add('Chainsaw Man', kind)
        assert r.count_today('Chainsaw Man') == 3
        assert r.count_today('Bleach') == 0

    def test_old_entries_pruned(self, tmp_path):
        p = tmp_path / 's.json'
        old = (datetime.now(timezone.utc)
               - timedelta(hours=anime_news_bot.SUBJECT_MEMORY_HOURS + 5)).isoformat()
        p.write_text(json.dumps([{'key': 'bleach', 'kind': 'новость', 'at': old}]))
        assert len(RecentSubjects(p)) == 0

    def test_persists(self, tmp_path):
        p = tmp_path / 's.json'
        RecentSubjects(p).add('Bleach', 'новость')
        assert RecentSubjects(p).seen_same_news('Bleach', 'новость')

    def test_empty_subject_ignored(self, tmp_path):
        r = RecentSubjects(tmp_path / 's.json')
        r.add('', 'новость')
        assert len(r) == 0
        assert r.seen_same_news('', 'новость') is False

    def test_corrupt_file_safe(self, tmp_path):
        p = tmp_path / 's.json'
        p.write_text('не json')
        assert len(RecentSubjects(p)) == 0


class TestQualityPipeline:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'LLM_API_KEY', 'k')
        monkeypatch.setattr(anime_news_bot, 'LLM_BASE_URL', 'https://x/v1')
        monkeypatch.setattr(anime_news_bot, 'LLM_MODEL', 'm')
        monkeypatch.setattr(anime_news_bot, 'LLM_MIN_INTERVAL', 0)
        monkeypatch.setattr(anime_news_bot, '_llm_disabled_runtime', False)
        monkeypatch.setattr(anime_news_bot, '_llm_fail_streak', 0)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            anime_news_bot.BotSettings(tmp_path / 'cfg.json'))
        monkeypatch.setattr(anime_news_bot, 'recent_subjects',
                            RecentSubjects(tmp_path / 'subj.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        return anime_news_bot

    def _run(self, env, answer, news, article=''):
        env._llm_fail_streak = 0
        with patch.object(env.requests, 'post', return_value=_reply(answer)), \
             patch.object(env, 'fetch_article',
                          return_value={'text': article, 'video': None}):
            return asyncio.run(env._llm_enrich(news))

    def test_article_read_when_summary_thin(self, env):
        news = {'title': 'Bleach opening', 'summary': 'Read more...',
                'link': 'https://ann.com/1', 'source': 'ANN'}
        article = ' '.join(['факт'] * 60)
        self._run(env, {'topic': 'аниме', 'kind': 'новость', 'subject': 'Bleach',
                        'title': 'Заголовок', 'summary': 'Текст.'}, news, article)
        assert news.get('_article_used') is True

    def test_article_not_read_when_summary_rich(self, env):
        news = {'title': 'T', 'summary': ' '.join(['слово'] * 40),
                'link': 'https://x/1', 'source': 'X'}
        with patch.object(env.requests, 'post',
                          return_value=_reply({'topic': 'аниме', 'kind': 'новость',
                                               'title': 'З', 'summary': ''})), \
             patch.object(env, 'fetch_article',
                          side_effect=AssertionError('не должны читать')):
            asyncio.run(env._llm_enrich(news))

    def test_article_disabled_by_setting(self, env):
        env.settings.llm_read_article = False
        news = {'title': 'T', 'summary': 'мало', 'link': 'https://x/1', 'source': 'X'}
        with patch.object(env.requests, 'post',
                          return_value=_reply({'topic': 'аниме', 'kind': 'новость',
                                               'title': 'З', 'summary': ''})), \
             patch.object(env, 'fetch_article',
                          side_effect=AssertionError('не должны читать')):
            asyncio.run(env._llm_enrich(news))

    @pytest.mark.parametrize('kind', list(anime_news_bot.LLM_KINDS_FILLER))
    def test_filler_skipped(self, env, kind):
        news = {'title': '5 perfect anime shows', 'summary': 'x' * 300,
                'link': 'https://p/1', 'source': 'Polygon'}
        assert self._run(env, {'topic': 'аниме', 'kind': kind, 'subject': '',
                               'title': 'Подборка', 'summary': ''}, news) == 'skip'

    @pytest.mark.parametrize('kind', list(anime_news_bot.LLM_KINDS_NEWS))
    def test_real_news_passes(self, env, kind):
        news = {'title': 'T', 'summary': 'x' * 300, 'link': 'https://p/1', 'source': 'X'}
        assert self._run(env, {'topic': 'аниме', 'kind': kind, 'subject': '',
                               'title': 'Заголовок', 'summary': ''}, news) == 'ok'

    def test_filler_can_be_allowed(self, env):
        env.settings.llm_skip_filler = False
        news = {'title': 'T', 'summary': 'x' * 300, 'link': 'https://p/1', 'source': 'X'}
        assert self._run(env, {'topic': 'аниме', 'kind': 'подборка', 'subject': '',
                               'title': 'Подборка', 'summary': ''}, news) == 'ok'

    def test_same_news_from_two_sources(self, env):
        answer = {'topic': 'аниме', 'kind': 'новость',
                  'subject': 'Bleach: Thousand-Year Blood War',
                  'title': 'Опенинг финальной части', 'summary': ''}
        first = {'title': 'A', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'ANN'}
        assert self._run(env, answer, first) == 'ok'
        env._commit_image_fingerprint(first)
        second = {'title': 'B', 'summary': 'y' * 300, 'link': 'https://b/2',
                  'source': 'ComicBook'}
        assert self._run(env, dict(answer, subject='BLEACH Thousand Year Blood War'),
                         second) == 'skip'

    def test_dedup_can_be_disabled(self, env):
        env.settings.llm_dedup_subject = False
        answer = {'topic': 'аниме', 'kind': 'новость', 'subject': 'Bleach',
                  'title': 'З', 'summary': ''}
        first = {'title': 'A', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'X'}
        self._run(env, answer, first)
        env._commit_image_fingerprint(first)
        second = {'title': 'B', 'summary': 'y' * 300, 'link': 'https://b/2', 'source': 'Y'}
        assert self._run(env, answer, second) == 'ok'

    def test_daily_repeat_limit(self, env):
        results = []
        for i, kind in enumerate(['новость', 'трейлер', 'анонс', 'релиз'], 1):
            news = {'title': f'T{i}', 'summary': 'x' * 300,
                    'link': f'https://c/{i}', 'source': 'X'}
            res = self._run(env, {'topic': 'аниме', 'kind': kind,
                                  'subject': 'Chainsaw Man',
                                  'title': f'Пост {i}', 'summary': ''}, news)
            results.append(res)
            if res == 'ok':
                env._commit_image_fingerprint(news)
        assert results[:anime_news_bot.SUBJECT_MAX_PER_DAY] == ['ok'] * anime_news_bot.SUBJECT_MAX_PER_DAY
        assert results[-1] == 'skip'

    def test_subject_saved_only_after_publish(self, env):
        news = {'title': 'T', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'X'}
        self._run(env, {'topic': 'аниме', 'kind': 'новость', 'subject': 'Bleach',
                        'title': 'З', 'summary': ''}, news)
        assert len(env.recent_subjects) == 0          # публикация ещё не случилась
        env._commit_image_fingerprint(news)
        assert len(env.recent_subjects) == 1

    def test_unknown_kind_treated_as_news(self, env):
        news = {'title': 'T', 'summary': 'x' * 300, 'link': 'https://a/1', 'source': 'X'}
        assert self._run(env, {'topic': 'аниме', 'kind': 'непонятно', 'subject': '',
                               'title': 'З', 'summary': ''}, news) == 'ok'
        assert news['_llm_kind'] == 'новость'
