from datetime import datetime, timedelta, timezone


import anime_news_bot as bot


def _news(title='Test story', summary='', source='Source', **extra):
    row = {
        'title': title,
        'summary': summary,
        'source': source,
        'link': 'https://example.com/story',
        'images': ['https://example.com/image.jpg'],
        '_confidence_score': 0.75,
    }
    row.update(extra)
    return row


def test_stage5_feature_flags_exist():
    for name in ('editorial_learning', 'editorial_rules', 'diversity_scheduler',
                 'breaking_news', 'confidence_moderation'):
        assert name in bot.FEATURE_FLAGS


def test_editorial_rules_roundtrip_and_evaluate(tmp_path):
    path = tmp_path / 'rules.json'
    rules = bot.EditorialRulesStore(path)
    assert rules.add('block', 'leak rumor')
    assert rules.add('downrank', 'ranking list')
    assert rules.add('boost', 'official announcement')
    assert rules.add('breaking', 'world premiere')
    again = bot.EditorialRulesStore(path)
    blocked = again.evaluate(_news('Leak rumor about a ranking list'))
    assert blocked['blocked'] is True
    assert blocked['adjustment'] < 0
    boosted = again.evaluate(_news('Official announcement: world premiere'))
    assert boosted['blocked'] is False
    assert boosted['adjustment'] > 0
    assert boosted['breaking'] == ['world premiere']


def test_editorial_rules_are_case_insensitive(tmp_path):
    rules = bot.EditorialRulesStore(tmp_path / 'rules.json')
    rules.add('boost', 'MAPPA')
    assert rules.evaluate(_news('mappa reveals visual'))['boost'] == ['mappa']


def test_editorial_rules_remove(tmp_path):
    rules = bot.EditorialRulesStore(tmp_path / 'rules.json')
    assert rules.add('block', 'spoiler dump')
    assert rules.remove('block', 'SPOILER DUMP')
    assert not rules.evaluate(_news('Spoiler dump'))['blocked']


def test_moderation_learning_requires_enough_samples(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_learning', True)
    monkeypatch.setattr(bot, 'EDITORIAL_LEARNING_MIN_SAMPLES', 5)
    fb = bot.ModerationFeedback(tmp_path / 'feedback.json')
    for _ in range(4):
        fb.record('hidden', _news('Clickbait rumor roundup'))
    assert 'clickbait' not in fb.learned_term_scores()
    fb.record('hidden', _news('Clickbait rumor roundup'))
    assert fb.learned_term_scores()['clickbait'] < 0


def test_moderation_learning_is_soft_not_blacklist(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_learning', True)
    monkeypatch.setattr(bot, 'EDITORIAL_LEARNING_MIN_SAMPLES', 5)
    fb = bot.ModerationFeedback(tmp_path / 'feedback.json')
    for _ in range(8):
        fb.record('hidden', _news('Clickbait rumor roundup'))
    value = fb.learning_adjustment(_news('Clickbait project update'))
    assert -2.5 <= value < 0


def test_moderation_learning_can_boost_consistently_approved_term(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_learning', True)
    monkeypatch.setattr(bot, 'EDITORIAL_LEARNING_MIN_SAMPLES', 5)
    fb = bot.ModerationFeedback(tmp_path / 'feedback.json')
    for _ in range(8):
        fb.record('published', _news('sakuga production notes'))
    assert fb.learning_adjustment(_news('sakuga feature announced')) > 0


def test_breaking_requires_confidence(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'breaking_news', True)
    monkeypatch.setattr(bot, 'BREAKING_MIN_CONFIDENCE', 0.70)
    low = _news('Anime gets official trailer and release date', _confidence_score=0.50)
    high = _news('Anime gets official trailer and release date', _confidence_score=0.90)
    bot._annotate_editorial_automation([low, high])
    assert not low.get('_breaking_news')
    assert high.get('_breaking_news') is True


def test_custom_breaking_rule_participates(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'breaking_news', True)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_rules', True)
    rules = bot.EditorialRulesStore(tmp_path / 'rules.json')
    rules.add('breaking', 'studio closure')
    monkeypatch.setattr(bot, 'editorial_rules', rules)
    news = _news('Studio closure confirmed', _confidence_score=0.95,
                 _story_cluster_size=2)
    bot._annotate_editorial_automation([news])
    assert news.get('_breaking_news') is True


def test_confidence_moderation_annotation(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'confidence_moderation', True)
    monkeypatch.setattr(bot, 'CONFIDENCE_AUTO_MIN', 0.60)
    low = _news('Low confidence story', _confidence_score=0.50)
    high = _news('High confidence story', _confidence_score=0.80)
    bot._annotate_editorial_automation([low, high])
    assert low.get('_needs_review') is True
    assert not high.get('_needs_review')


def test_breaking_bypasses_confidence_review(monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'confidence_moderation', True)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'breaking_news', True)
    monkeypatch.setattr(bot, 'CONFIDENCE_AUTO_MIN', 0.90)
    monkeypatch.setattr(bot, 'BREAKING_MIN_CONFIDENCE', 0.60)
    row = _news('Official trailer and release date announced', _confidence_score=0.80,
                _story_cluster_size=2)
    bot._annotate_editorial_automation([row])
    assert row.get('_breaking_news') is True
    assert not row.get('_needs_review')


def test_recent_franchise_penalty_decays(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'diversity_scheduler', True)
    monkeypatch.setattr(bot, 'FRANCHISE_COOLDOWN_MIN', 180)
    monkeypatch.setattr(bot, 'FRANCHISE_COOLDOWN_PENALTY', 9.0)
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    store._items = [{
        'title': 'Frieren gets a trailer', 'subject': 'Frieren',
        'at': (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
    }]
    monkeypatch.setattr(bot, 'story_history', store)
    row = _news('Frieren gets a new visual', _llm_subject='Frieren')
    penalty = bot._recent_franchise_penalty(row)
    assert -9.0 < penalty < 0


def test_breaking_bypasses_franchise_cooldown(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'diversity_scheduler', True)
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    store._items = [{'title': 'Frieren news', 'subject': 'Frieren',
                     'at': datetime.now(timezone.utc).isoformat()}]
    monkeypatch.setattr(bot, 'story_history', store)
    row = _news('Frieren release date', _llm_subject='Frieren', _breaking_news=True)
    assert bot._recent_franchise_penalty(row) == 0


def test_priority_uses_editorial_signals(monkeypatch):
    monkeypatch.setattr(bot, 'story_history', None)
    base = _news('Ordinary item')
    boosted = dict(base, _editorial_rule_adjustment=4.0, _learned_editorial_adjustment=1.0)
    assert bot._news_priority_score(boosted) >= bot._news_priority_score(base) + 4.9


def test_editorial_block_marker_is_respected(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'editorial_rules', True)
    rules = bot.EditorialRulesStore(tmp_path / 'rules.json')
    rules.add('block', 'fake giveaway')
    monkeypatch.setattr(bot, 'editorial_rules', rules)
    row = _news('Fake giveaway announced')
    bot._annotate_editorial_automation([row])
    assert row['_editorial_blocked'] is True
    assert bot._editorial_allowed(row) is False


def test_prioritize_diversifies_same_franchise(monkeypatch):
    monkeypatch.setattr(bot, 'story_history', None)
    a1 = _news('Alpha story one', _llm_subject='Alpha', _priority_score=0)
    a2 = _news('Alpha story two', _llm_subject='Alpha', _priority_score=0)
    b = _news('Beta story', _llm_subject='Beta', _priority_score=0)
    # Equalize intrinsic scoring so only in-batch diversity matters.
    monkeypatch.setattr(bot, '_news_priority_score', lambda _n: 10.0)
    rows = bot._prioritize_news([a1, a2, b])
    assert bot._franchise_key(rows[0]) != bot._franchise_key(rows[1])
