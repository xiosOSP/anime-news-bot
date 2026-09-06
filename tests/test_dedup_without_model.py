"""Диагностика и дедуп не должны зависеть от того, что происходит снаружи.

Обе проблемы найдены по живому боту:

1. `/health` показывал «медиасбоев не зафиксировано», хотя видео стабильно не
   доходило. Счётчики жили только в памяти процесса, а процесс перезапускается
   платформой каждые ~18 минут — статистика до отчёта не доживала.
2. Одна и та же новость приходила в ветку по нескольку раз. Дедуп доставленных
   сюжетов опирался на предмет новости от модели, а модель постоянно упиралась
   в лимит запросов бесплатного провайдера: предмет пустой — дедуп молчит.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _clean_counters():
    with bot._media_failure_lock:
        bot._media_failure_counts.clear()
        bot._media_failures.clear()
        bot._media_failure_period_start = datetime.now(timezone.utc).isoformat()
        bot._media_failure_generation = 0
        bot._media_failure_save_task = None
    bot._media_failure_persisted_generation = -1
    yield
    with bot._media_failure_lock:
        bot._media_failure_counts.clear()
        bot._media_failures.clear()
        bot._media_failure_save_task = None
    bot._media_failure_persisted_generation = -1


class TestMediaFailuresSurviveRestart:
    def test_counters_are_written_to_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', tmp_path / 'media.json')
        bot._record_media_failure({'title': 'Трейлер', 'source': 'ANN'},
                                  'video_download_failed', 'yt-dlp упал')
        data = json.loads((tmp_path / 'media.json').read_text(encoding='utf-8'))
        assert data['counts']['video_download_failed'] == 1
        assert data['recent'][-1]['source'] == 'ANN'
        assert datetime.fromisoformat(data['period_start']).tzinfo is not None

    def test_counters_are_restored_on_next_start(self, monkeypatch, tmp_path):
        path = tmp_path / 'media.json'
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', path)
        bot._record_media_failure({'title': 'A', 'source': 'S'}, 'video_download_failed')
        bot._record_media_failure({'title': 'B', 'source': 'S'}, 'cover_fallback')

        # Имитируем перезапуск процесса: память чистая, файл на месте.
        with bot._media_failure_lock:
            bot._media_failure_counts.clear()
            bot._media_failures.clear()
        bot._load_media_failures()

        counts = bot.media_failure_snapshot()['counts']
        assert counts['video_download_failed'] == 1
        assert counts['cover_fallback'] == 1

    def test_missing_file_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', tmp_path / 'нет.json')
        bot._load_media_failures()
        assert bot.media_failure_snapshot()['counts'] == {}

    def test_broken_file_is_survived(self, monkeypatch, tmp_path):
        path = tmp_path / 'media.json'
        path.write_text('{не json', encoding='utf-8')
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', path)
        bot._load_media_failures()
        assert bot.media_failure_snapshot()['counts'] == {}

    def test_unknown_codes_are_ignored_on_load(self, monkeypatch, tmp_path):
        """Чужие коды не должны раздувать метки Prometheus."""
        path = tmp_path / 'media.json'
        path.write_text(json.dumps({
            'counts': {'video_download_failed': 3, 'какой_то_мусор': 99},
            'recent': [],
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', path)
        bot._load_media_failures()
        counts = bot.media_failure_snapshot()['counts']
        assert counts.get('video_download_failed') == 3
        assert 'какой_то_мусор' not in counts

    def test_disk_failure_does_not_break_recording(self, monkeypatch):
        """Отказ диска не должен ронять публикацию из-за статистики."""
        def boom(*args, **kwargs):
            raise OSError('диск полон')

        monkeypatch.setattr(bot, '_atomic_write_json', boom)
        bot._record_media_failure({'title': 'A', 'source': 'S'}, 'cover_fallback')
        assert bot.media_failure_snapshot()['counts']['cover_fallback'] == 1

    def test_loading_twice_does_not_duplicate_recent_rows(self, monkeypatch, tmp_path):
        path = tmp_path / 'media.json'
        path.write_text(json.dumps({
            'period_start': datetime.now(timezone.utc).isoformat(),
            'counts': {'cover_fallback': 1},
            'recent': [{'ts': '2026-08-26T00:00:00+00:00',
                        'code': 'cover_fallback', 'source': 'ANN', 'title': 'A'}],
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', path)
        bot._load_media_failures()
        bot._load_media_failures()
        assert len(bot.media_failure_snapshot()['recent']) == 1

    def test_expired_window_is_not_reported_forever(self, monkeypatch, tmp_path):
        path = tmp_path / 'media.json'
        path.write_text(json.dumps({
            'period_start': (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
            'counts': {'video_download_failed': 99},
            'recent': [{'code': 'video_download_failed'}],
        }), encoding='utf-8')
        monkeypatch.setattr(bot, 'MEDIA_FAILURES_FILE', path)
        bot._load_media_failures()
        assert bot.media_failure_snapshot()['counts'] == {}

    def test_async_runtime_coalesces_disk_writes(self, monkeypatch):
        saved = []
        monkeypatch.setattr(bot, '_save_media_failures', lambda snapshot: saved.append(snapshot))

        async def exercise():
            bot._record_media_failure({'title': 'A', 'source': 'S'}, 'cover_fallback')
            bot._record_media_failure({'title': 'B', 'source': 'S'}, 'video_download_failed')
            pending = bot._media_failure_save_task
            assert pending is not None
            await pending

        asyncio.run(exercise())
        assert len(saved) == 1
        assert saved[0]['counts'] == {'cover_fallback': 1, 'video_download_failed': 1}

    def test_late_old_snapshot_cannot_overwrite_newer_one(self, monkeypatch):
        written = []
        monkeypatch.setattr(bot, '_atomic_write_json',
                            lambda _path, payload, **_kwargs: written.append(payload))
        bot._save_media_failures({'_generation': 2, 'counts': {'cover_fallback': 2}})
        bot._save_media_failures({'_generation': 1, 'counts': {'cover_fallback': 1}})
        assert [row['counts']['cover_fallback'] for row in written] == [2]


class TestOrdinalNumbersAreUnderstood:
    @pytest.mark.parametrize('title, expected', [
        ('Вышел трейлер второго сезона «Голубой Шкатулки»', {'2'}),
        ('Выдали трейлер ко 2 сезону «Голубой Шкатулки»', {'2'}),
        ('Blue Box Season 2 trailer released', {'2'}),
        ('Third season announced', {'3'}),
        ('Анонсирован первый сезон', {'1'}),
    ])
    def test_word_numbers_become_digits(self, title, expected):
        assert bot._story_numbers(title) == expected

    @pytest.mark.parametrize('title', [
        'Шестерёнка судьбы: новый трейлер',
        'Перчатки героя',
        'Третьесортный фильм',
        'Секретная лаборатория',
        'Пятно на солнце получило экранизацию',
        'Четверть века спустя вышел трейлер',
        'Десятина — новый исторический фильм',
    ])
    def test_no_false_positives(self, title):
        assert bot._story_numbers(title) == set()


class TestDedupWorksWithoutTheModel:
    """Предмет новости проставляет модель. Она часто в лимите — дедуп обязан
    справляться и без неё, иначе одна новость приходит в ветку по нескольку раз.
    """

    @staticmethod
    def _delivered(title, subject=''):
        news = {'title': title}
        return {'delivered_title': title,
                'delivered_subject': subject,
                'delivered_markers': sorted(bot._story_event_markers(news)),
                'delivered_numbers': sorted(bot._story_numbers(news))}

    @pytest.mark.parametrize('old, new', [
        ('Вышел трейлер второго сезона «Голубой Шкатулки»',
         'Выдали трейлер ко 2 сезону «Голубой Шкатулки» (Ao no Hako)'),
        ('Blue Box Season 2 trailer released', 'Blue Box Season 2 trailer is out'),
    ])
    def test_same_event_is_caught_without_subject(self, old, new):
        assert bot.StoryRegistry._delivery_match({'title': new}, self._delivered(old))

    @pytest.mark.parametrize('old, new', [
        # Трейлер и постер одной франшизы — разные редакционные события.
        ('Вышел трейлер второго сезона «Голубой Шкатулки»',
         'Новый постер 2 сезона аниме «Голубая шкатулка»'),
        # Разные сезоны.
        ('Вышел трейлер второго сезона «Голубой Шкатулки»',
         'Вышел трейлер третьего сезона «Голубой Шкатулки»'),
        # Разные франшизы с одинаковым типом события.
        ('Вышел трейлер второго сезона «Голубой Шкатулки»',
         'Вышел трейлер второго сезона «Атаки титанов»'),
        # Ничего общего.
        ('Вышел трейлер второго сезона «Голубой Шкатулки»',
         'Вышел трейлер игры Elden Ring'),
    ])
    def test_different_events_stay_separate(self, old, new):
        assert not bot.StoryRegistry._delivery_match({'title': new}, self._delivered(old))

    @pytest.mark.parametrize('old, new', [
        ('Dragon Ball Season 2 trailer released',
         'Dragon Ball Daima Season 2 trailer released'),
        ('Solo Leveling Season 2 trailer released',
         'Solo Leveling Ragnarok Season 2 trailer released'),
        ('Love Live Season 2 trailer released',
         'Love Live Superstar Season 2 trailer released'),
        ('My Hero Academia Season 2 trailer released',
         'My Hero Academia Vigilantes Season 2 trailer released'),
    ])
    def test_spinoffs_are_not_swallowed_by_base_franchise(self, old, new):
        assert not bot.StoryRegistry._delivery_match({'title': new}, self._delivered(old))

    @pytest.mark.parametrize('old, new', [
        ('Blue Box Season 2 trailer released',
         'Blue Box Season 2 final trailer released'),
        ('Голубая Шкатулка: вышел трейлер второго сезона',
         'Голубая Шкатулка: вышел финальный трейлер второго сезона'),
        ('Naruto Season 2 trailer released',
         'Naruto Season 2 trailer released (Remake)'),
    ])
    def test_meaningful_title_extension_is_not_a_duplicate(self, old, new):
        assert not bot.StoryRegistry._delivery_match({'title': new}, self._delivered(old))

    def test_subject_still_wins_when_the_model_worked(self):
        old = 'Вышел трейлер второго сезона «Голубой Шкатулки»'
        new = 'Совсем другой заголовок про трейлер 2 сезона'
        row = self._delivered(old, subject='Голубая шкатулка')
        assert bot.StoryRegistry._delivery_match(
            {'title': new, '_llm_subject': 'Голубая шкатулка'}, row)

    def test_marker_is_required(self):
        """Без типа события две новости об одной франшизе не склеиваются."""
        old = 'Голубая шкатулка получила награду'
        new = 'Голубая шкатулка вышла в Японии'
        assert not bot.StoryRegistry._delivery_match({'title': new}, self._delivered(old))

    def test_inflected_generic_words_do_not_count_as_evidence(self):
        """«сезона» и «сезону» — одно служебное слово, а не общий якорь."""
        assert bot._is_generic_anchor('сезона')
        assert bot._is_generic_anchor('сезону')
        assert not bot._is_generic_anchor('шкатулки')
