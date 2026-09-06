"""Тесты видео-конвейера для RSS-источников: поиск ролика на странице статьи,
формат под наличие ffmpeg, внятная причина каждого провала."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot

ARTICLE_WITH_VIDEO = '''<html><head>
<meta property="og:video" content="https://www.youtube.com/watch?v=abc123">
</head><body><article>
<p>The final trailer for THE RIBBON HERO anime film was released on Thursday,
   revealing additional cast members ahead of the August 8 premiere date.</p>
<p>The film is the first feature-length project from studio Outline animation.</p>
</article></body></html>'''


class TestFindVideoInHtml:
    def test_og_video(self):
        assert anime_news_bot._find_video_in_html(ARTICLE_WITH_VIDEO) == \
            'https://www.youtube.com/watch?v=abc123'

    def test_iframe_embed(self):
        html = '<iframe src="https://www.youtube.com/embed/xyz"></iframe>'
        assert 'youtube.com/embed/xyz' in anime_news_bot._find_video_in_html(html)

    def test_protocol_relative_iframe(self):
        html = '<iframe src="//www.youtube.com/embed/xyz"></iframe>'
        assert anime_news_bot._find_video_in_html(html).startswith('https://')

    def test_video_tag(self):
        html = '<video src="https://cdn.site.com/clip.mp4"></video>'
        assert anime_news_bot._find_video_in_html(html) == 'https://cdn.site.com/clip.mp4'

    def test_link_to_video_host(self):
        html = '<a href="https://youtu.be/abc">Watch the trailer</a>'
        assert 'youtu.be/abc' in anime_news_bot._find_video_in_html(html)

    def test_meta_wins_over_link(self):
        html = ('<meta property="og:video" content="https://youtube.com/watch?v=meta">'
                '<a href="https://youtube.com/watch?v=link">x</a>')
        assert 'v=meta' in anime_news_bot._find_video_in_html(html)

    def test_no_video(self):
        assert anime_news_bot._find_video_in_html(
            '<article><p>обычный текст без роликов</p></article>') is None

    def test_ignores_non_video_links(self):
        html = '<a href="https://example.com/page">не видео</a>'
        assert anime_news_bot._find_video_in_html(html) is None

    def test_broken_html_safe(self):
        assert anime_news_bot._find_video_in_html('<div><iframe src=') is None


class TestVideoHint:
    @pytest.mark.parametrize('title,expected', [
        ("The Ribbon Hero Anime Film's Promo Video Highlights Cast", True),
        ('Финальный трейлер фильма THE RIBBON HERO', True),
        ('Опубликован опенинг к 3 сезону', True),
        ('New Teaser Revealed', True),
        ('Manga Ends Serialization Next Month', False),
        ('New Chapter Released Today', False),
    ])
    def test_detection(self, title, expected):
        assert anime_news_bot._probably_has_video(
            {'title': title, 'summary': ''}) is expected


class TestFetchArticleVideo:
    def test_returns_text_and_video(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(
                                status_code=200, text=ARTICLE_WITH_VIDEO,
                                headers={'Content-Type': 'text/html'}))
        out = anime_news_bot.fetch_article('https://ann.com/x')
        assert 'August 8' in out['text']
        assert out['video'] == 'https://www.youtube.com/watch?v=abc123'

    def test_text_wrapper_still_works(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(
                                status_code=200, text=ARTICLE_WITH_VIDEO,
                                headers={'Content-Type': 'text/html'}))
        assert 'August 8' in anime_news_bot.fetch_article_text('https://ann.com/y')

    def test_failure_returns_empty_dict(self, monkeypatch):
        anime_news_bot._article_cache.clear()
        monkeypatch.setattr(anime_news_bot, 'http_get_public_with_retry', lambda *a, **k: None)
        assert anime_news_bot.fetch_article('https://x/1') == {'text': '', 'video': None}


class TestVideoFormat:
    def test_progressive_without_ffmpeg(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot.shutil, 'which', lambda name: None)
        fmt = anime_news_bot._video_format()
        assert 'acodec!=none' in fmt and '+ba' not in fmt   # склейка не нужна

    def test_merge_allowed_with_ffmpeg(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot.shutil, 'which', lambda name: '/usr/bin/ffmpeg')
        assert '+ba' in anime_news_bot._video_format()


class TestVideoFailureReasons:
    """Каждая причина провала должна называться словами."""

    def _run(self, video, ytdlp=True, enabled=True, title='Trailer released'):
        anime_news_bot.settings = MagicMock(video_enabled=enabled)
        anime_news_bot.YT_DLP_AVAILABLE = ytdlp
        news = {'title': title, 'summary': '', 'video': video}
        asyncio.run(anime_news_bot._prepare_video_file(news))
        return news.get('_video_note')

    def test_setting_off(self):
        assert 'выключена' in self._run('https://youtube.com/watch?v=x', enabled=False)

    def test_no_ytdlp(self):
        assert 'yt-dlp не установлен' in self._run('https://youtube.com/watch?v=x',
                                                   ytdlp=False)

    def test_unsupported_host(self):
        assert 'не поддерживается' in self._run('https://vk.com/video1')

    def test_video_expected_but_missing(self):
        assert 'ссылки на него нет' in self._run(None)

    def test_text_news_no_note(self):
        assert self._run(None, title='Manga chapter released') is None

    def test_direct_mp4_no_note(self):
        assert self._run('https://site.com/clip.mp4') is None


class TestDownloadVideoNotes:
    def test_reports_missing_ytdlp(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', False)
        note = []
        assert anime_news_bot.download_video('https://youtube.com/x', note) is None
        assert 'yt-dlp не установлен' in note[0]

    def test_reports_exception(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)

        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, *a, **k):
                raise RuntimeError('видео закрыто')
        monkeypatch.setattr(anime_news_bot, 'yt_dlp',
                            MagicMock(YoutubeDL=lambda o: Boom()))
        note = []
        assert anime_news_bot.download_video('https://youtube.com/x', note) is None
        assert 'видео закрыто' in note[0]

    def test_reports_too_long(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', True)
        monkeypatch.setattr(anime_news_bot, 'VIDEO_MAX_DURATION_SEC', 60)

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {'duration': 600}
        monkeypatch.setattr(anime_news_bot, 'yt_dlp',
                            MagicMock(YoutubeDL=lambda o: Fake()))
        note = []
        assert anime_news_bot.download_video('https://youtube.com/x', note) is None
        assert 'длиннее лимита' in note[0]

    def test_works_without_note_arg(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'YT_DLP_AVAILABLE', False)
        assert anime_news_bot.download_video('https://youtube.com/x') is None


class TestArticleVideoInPipeline:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        for name, val in (('LLM_API_KEY', 'k'), ('LLM_BASE_URL', 'https://x/v1'),
                          ('LLM_MODEL', 'm'), ('LLM_MIN_INTERVAL', 0),
                          ('_llm_disabled_runtime', False), ('_llm_fail_streak', 0)):
            monkeypatch.setattr(anime_news_bot, name, val)
        monkeypatch.setattr(anime_news_bot, 'settings',
                            anime_news_bot.BotSettings(tmp_path / 'c.json'))
        monkeypatch.setattr(anime_news_bot, 'recent_subjects',
                            anime_news_bot.RecentSubjects(tmp_path / 's.json'))
        monkeypatch.setattr(anime_news_bot, 'image_hashes', None)
        anime_news_bot._article_cache.clear()
        return anime_news_bot

    def _reply(self):
        import json
        answer = {'topic': 'аниме', 'kind': 'трейлер', 'subject': 'The Ribbon Hero',
                  'title': 'Вышел финальный трейлер фильма «Герой ленты»',
                  'summary': 'Премьера 8 августа.', 'tags': ['#аниме']}
        return MagicMock(status_code=200, text='', json=lambda: {
            'choices': [{'message': {'content': json.dumps(answer, ensure_ascii=False)}}]})

    def test_video_taken_from_article(self, env, monkeypatch):
        news = {'title': "Ribbon Hero Anime Film's Promo Video Highlights Cast",
                'summary': 'Read more...', 'link': 'https://ann.com/x',
                'source': 'ANN', 'video': None}
        monkeypatch.setattr(env, 'http_get_public_with_retry',
                            lambda *a, **k: MagicMock(
                                status_code=200, text=ARTICLE_WITH_VIDEO,
                                headers={'Content-Type': 'text/html'}))
        with patch.object(env.requests, 'post', return_value=self._reply()):
            asyncio.run(env._llm_enrich(news))
        assert news['video'] == 'https://www.youtube.com/watch?v=abc123'
        assert 'найден в статье' in news['_video_note']

    def test_existing_video_not_overwritten(self, env, monkeypatch):
        news = {'title': 'Trailer released', 'summary': 'x' * 300,
                'link': 'https://ann.com/y', 'source': 'ANN',
                'video': 'https://youtube.com/watch?v=original'}
        def boom(*a, **k):
            raise AssertionError('статью читать не должны')
        monkeypatch.setattr(env, 'http_get_public_with_retry', boom)
        with patch.object(env.requests, 'post', return_value=self._reply()):
            asyncio.run(env._llm_enrich(news))
        assert news['video'] == 'https://youtube.com/watch?v=original'

    def test_text_news_does_not_fetch(self, env, monkeypatch):
        news = {'title': 'Manga ends serialization', 'summary': 'x' * 300,
                'link': 'https://ann.com/z', 'source': 'ANN', 'video': None}
        def boom(*a, **k):
            raise AssertionError('лишний запрос')
        monkeypatch.setattr(env, 'http_get_public_with_retry', boom)
        with patch.object(env.requests, 'post', return_value=self._reply()):
            asyncio.run(env._llm_enrich(news))
