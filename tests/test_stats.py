"""Тесты метрик BotStats."""
from datetime import datetime, timedelta

import pytest

from anime_news_bot import BotStats


@pytest.fixture
def s(tmp_json):
    return BotStats(tmp_json)


class TestRecord:
    async def test_collected(self, s):
        await s.record_collected('ANN', 3)
        await s.record_collected('CBR', 2)
        totals = s.get_totals()
        assert totals['collected'] == 5
        by_src = s.get_by_source()
        assert by_src['ANN']['collected'] == 3
        assert by_src['CBR']['collected'] == 2

    async def test_published(self, s):
        await s.record_published('ANN')
        await s.record_published('ANN')
        await s.record_published('CBR')
        assert s.get_totals()['published'] == 3
        assert s.get_by_source()['ANN']['published'] == 2

    async def test_skipped(self, s):
        await s.record_skipped('no_image', 'Reddit')
        await s.record_skipped('duplicate')
        totals = s.get_totals()
        assert totals['skipped_no_image'] == 1
        assert totals['skipped_duplicate'] == 1

    async def test_failed_send(self, s):
        await s.record_failed_send('ANN')
        assert s.get_totals()['failed_send'] == 1

    async def test_source_error(self, s):
        await s.record_source_error('Crunchyroll')
        assert s.get_by_source()['Crunchyroll']['errors'] == 1


class TestCountEvents:
    async def test_counts_recent(self, s):
        await s.record_published('ANN')
        await s.record_published('ANN')
        now = datetime.now()
        past = now - timedelta(hours=1)
        count = s.count_events_since(past, 'published')
        assert count == 2

    async def test_filter_by_type(self, s):
        await s.record_published('ANN')
        await s.record_failed_send('ANN')
        past = datetime.now() - timedelta(hours=1)
        assert s.count_events_since(past, 'published') == 1
        assert s.count_events_since(past, 'failed_send') == 1


class TestPersistence:
    async def test_survives_restart(self, tmp_json):
        s1 = BotStats(tmp_json)
        await s1.record_published('ANN')
        await s1.record_published('CBR')

        s2 = BotStats(tmp_json)
        assert s2.get_totals()['published'] == 2
        assert s2.get_by_source()['ANN']['published'] == 1
