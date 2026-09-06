import asyncio
import io

import pytest
from PIL import Image, ImageDraw

import anime_news_bot as bot


def _jpeg(size=(600, 1000)):
    im = Image.new('RGB', size, (20, 30, 50))
    d = ImageDraw.Draw(im)
    w, h = size
    # Put detail around the centre so the entropy-aware crop has a useful target.
    d.rectangle((w * .15, h * .3, w * .85, h * .7), fill=(230, 180, 50))
    d.ellipse((w * .3, h * .38, w * .7, h * .62), fill=(40, 140, 230))
    out = io.BytesIO(); im.save(out, 'JPEG', quality=90)
    return out.getvalue()


@pytest.fixture(autouse=True)
def restore_flags():
    old = dict(bot.FEATURE_FLAGS)
    yield
    bot.FEATURE_FLAGS.clear(); bot.FEATURE_FLAGS.update(old)
    bot._image_bytes_cache.clear()


def test_entity_evidence_is_weaker_than_official_reference(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'source_reputation', False)
    news = {'title': 'Blue Lock gets trailer', 'source': 'Blog', 'link': 'https://example.com/x',
            'images': ['https://img/x.jpg'], '_story_sources': ['Blog']}
    base = bot._confidence_score(news)
    entity = dict(news, _verification_evidence=[{'type': 'entity'}])
    official = dict(news, _verification_evidence=[{'type': 'official_reference'}])
    assert base < bot._confidence_score(entity) < bot._confidence_score(official)


def test_verification_entity_query_cuts_announcement_tail():
    q = bot._verification_entity_query({'title': 'Blue Lock Season 2 gets new official trailer'})
    assert q == 'Blue Lock Season 2'


def test_active_verification_only_spends_budget_on_weak_single_source(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'active_verification', True)
    calls = []
    def fake(news):
        calls.append(news['title'])
        return [{'type': 'entity', 'provider': 'AniList'}]
    monkeypatch.setattr(bot, '_verify_story_evidence_blocking', fake)
    weak = {'title': 'Weak story', 'source': 'Blog', 'link': 'https://example.com/weak',
            '_story_cluster_size': 1, '_story_sources': ['Blog'], '_confidence_score': 0.3}
    multi = {'title': 'Confirmed story', 'source': 'A', 'link': 'https://example.com/multi',
             '_story_cluster_size': 2, '_story_sources': ['A', 'B'], '_confidence_score': 0.5}
    official = {'title': 'Official story', 'source': 'Kadokawa Official', 'link': 'https://example.com/o',
                '_story_cluster_size': 1, '_story_sources': ['Kadokawa Official'], '_confidence_score': 0.4}
    out = asyncio.run(bot._apply_active_verification([weak, multi, official]))
    assert out[0]['_verification_checked'] is True
    assert calls == ['Weak story']
    assert '_verification_checked' not in multi
    assert '_verification_checked' not in official


def test_crop_plan_for_recoverable_portrait():
    plan = bot._media_crop_plan({'width': 600, 'height': 1000, 'aspect': 0.6})
    assert plan and plan['axis'] == 'vertical'
    assert plan['loss'] <= bot.MEDIA_CROP_MAX_LOSS


def test_crop_plan_refuses_destructive_extreme_portrait():
    assert bot._media_crop_plan({'width': 600, 'height': 1800, 'aspect': 1/3}) is None


def test_smart_crop_outputs_targetish_jpeg():
    data = _jpeg((600, 1000))
    plan = bot._media_crop_plan({'width': 600, 'height': 1000, 'aspect': 0.6})
    cropped = bot._smart_crop_image_bytes(data, plan)
    assert cropped and cropped.startswith(b'\xff\xd8')
    with Image.open(io.BytesIO(cropped)) as im:
        assert abs((im.width / im.height) - 0.8) < 0.02
        assert max(im.size) <= bot.MEDIA_CROP_MAX_DIM


def test_optimize_records_json_safe_crop_plan(monkeypatch):
    data = _jpeg((600, 1000))
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda _url: data)
    news = {'title': 'Poster', 'link': '', 'images': ['https://img.example/poster.jpg']}
    asyncio.run(bot._optimize_news_media(news))
    assert news['_media_preview']['state'] == 'crop-planned'
    assert isinstance(news['_media_crop_plan'], dict)
    # Plan must remain persistable; no bytes are stored in the news object.
    import json
    json.dumps(news)


def test_resolver_applies_primary_crop_at_send_time(monkeypatch):
    data = _jpeg((600, 1000))
    monkeypatch.setattr(bot, '_cached_image_bytes', lambda _url: data)
    plan = bot._media_crop_plan({'width': 600, 'height': 1000, 'aspect': 0.6})
    resolved = asyncio.run(bot._resolve_photos_for_album(['https://img.example/a.jpg'], plan))
    assert isinstance(resolved[0], bytes)
    with Image.open(io.BytesIO(resolved[0])) as im:
        assert abs((im.width / im.height) - 0.8) < 0.02


def test_resolver_accepts_existing_bytes_without_urlparse():
    raw = _jpeg((640, 360))
    resolved = asyncio.run(bot._resolve_photos_for_album([raw]))
    assert resolved == [raw]
    assert bot._download_needed_host(raw) is False


def test_official_reference_candidates_ignore_nonofficial_links(monkeypatch):
    # Avoid DNS in _is_public_http_url while still exercising official filtering.
    monkeypatch.setattr(bot, '_is_public_http_url', lambda url: True)
    html = b'''<html><body>
      <a href="https://random.example/story">random</a>
      <a href="https://www.crunchyroll.com/news/blue-lock">Blue Lock new trailer</a>
    </body></html>'''
    rows = bot._official_reference_candidates(html, 'https://news.example/a')
    assert len(rows) == 1
    assert 'crunchyroll.com' in rows[0][0]
