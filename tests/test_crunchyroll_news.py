"""Crunchyroll: новости из их API вместо ленты выхода серий в дубляжах.

Старая лента /rss/news на живой проверке 23.09.2026 отдала 50 записей — и все
были выходом серий в тайском и польском дубляже. Ответ API ниже — настоящий,
урезанный до нужных полей.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import anime_news_bot as bot


def _story(headline='From Old Country Bumpkin to Master Swordsman Season 3 Anime Announced',
           slug='latest/2026/9/23/country-bumpkin-season-3', hours_ago=2, **content):
    created = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    body = {'headline': headline, 'lead': 'Visual showcased alongside the reveal',
            'created_at': created, 'article_date': created[:16].replace('T', ' '),
            'thumbnail': {'filename': 'https://a.storyblok.com/f/178900/1200x675/ee700958d3/bumpkin.jpeg'}}
    body.update(content)
    return {'uuid': 'u', 'slug': slug, 'content': body}


@pytest.fixture
def api(monkeypatch):
    def serve(payload, status=200):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        response = MagicMock(status_code=status, text=text)
        monkeypatch.setattr(bot, 'http_get_with_retry', lambda *a, **k: response)
        monkeypatch.setattr(bot, '_read_limited_text', lambda r: r.text)
        monkeypatch.setattr(bot, 'settings', MagicMock(post_max_age_hours=48))
    return serve


def test_the_story_becomes_a_news_item(api):
    api({'total': 1, 'stories': [_story()]})
    news = bot.get_crunchyroll_news()[0]
    assert news['title'] == 'From Old Country Bumpkin to Master Swordsman Season 3 Anime Announced'
    assert news['link'] == 'https://www.crunchyroll.com/news/latest/2026/9/23/country-bumpkin-season-3'
    assert news['summary'] == 'Visual showcased alongside the reveal'
    assert news['images'] == ['https://a.storyblok.com/f/178900/1200x675/ee700958d3/bumpkin.jpeg']
    assert news['published_parsed'] is not None and news['source'] == 'Crunchyroll'


def test_old_stories_stay_out(api):
    api({'stories': [_story(slug='latest/new', hours_ago=2), _story(slug='latest/old', hours_ago=24 * 30)]})
    assert [n['link'].rsplit('/', 1)[-1] for n in bot.get_crunchyroll_news()] == ['new']


@pytest.mark.parametrize('story', [
    _story(headline=''),                        # без заголовка
    _story(slug=''),                            # без адреса
    _story(slug='latest/../../evil?x=1'),       # адрес не из API
    'not a dict',
])
def test_broken_stories_are_skipped(api, story):
    api({'stories': [story, _story(slug='latest/ok')]})
    assert [n['link'].rsplit('/', 1)[-1] for n in bot.get_crunchyroll_news()] == ['ok']


@pytest.mark.parametrize(('payload', 'status'), [
    ({'stories': []}, 403),
    ('<html>not json</html>', 200),
    ({'total': 0}, 200),
])
def test_api_failure_is_reported_to_source_health(api, payload, status):
    api(payload, status)
    result = bot.get_crunchyroll_news()
    assert isinstance(result, bot.SourceFetchFailure) and result == [] and result.reason


def test_crunchyroll_uses_the_news_api_not_the_episode_feed():
    assert 'Latest%20News' in bot.CRUNCHYROLL_NEWS_API
    assert 'rss/news' not in bot.CRUNCHYROLL_NEWS_API
