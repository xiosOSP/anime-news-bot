"""Отсев непрофильного, когда модели нет.

Шесть лент общей тематики (Collider, /Film, Variety, Polygon, ComingSoon,
Filmix) дают аниме вперемешку с кино, сериалами и играми. Отличала одно от
другого только модель, и стоило ей замолчать — в канал уходили Zelda, Том Круз
и «Ходячие мертвецы». Здесь проверяется грубый локальный отсев по словам в
заголовке: он должен ловить именно этот случай и не мешать всему остальному.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


def _news(source, title, summary=''):
    return {'source': source, 'title': title, 'summary': summary,
            'link': f'https://example.test/{abs(hash(title))}'}


class TestMarkersInTitle:
    def test_general_source_without_marker_is_off_topic(self):
        """Ровно то, из-за чего всё затевалось."""
        assert bot.off_topic_without_llm(
            _news('Polygon', 'The Legend of Zelda movie casts its Link and Zelda'))
        assert bot.off_topic_without_llm(
            _news('Collider', 'The Walking Dead spinoff gets a season 3 renewal'))
        assert bot.off_topic_without_llm(
            _news('Variety', 'Tom Cruise says Mission: Impossible is not over'))

    def test_general_source_with_marker_passes(self):
        for title in ('Chainsaw Man: Reze Arc trailer drops',
                      'Shonen Jump announces a new series',
                      'Studio Ghibli museum opens a new exhibit',
                      'One-Piece live action season 2 gets a date'):
            assert not bot.off_topic_without_llm(_news('Collider', title)), title

    def test_specialised_sources_are_never_touched(self):
        """У профильных лент отсутствие слова «аниме» ничего не значит.

        Заголовок «Zelda movie casts Link» на ANN — это новость про экранизацию
        в аниме-издании, а не про игры: судить о ней по словарю нельзя.
        """
        assert not bot.off_topic_without_llm(
            _news('AnimeNewsNetwork', 'Zelda movie casts Link'))
        assert not bot.off_topic_without_llm(
            _news('Anime Trending', 'Live action casting announced'))

    def test_latin_marker_matches_whole_words_only(self):
        """«manga» внутри «manganese» — не манга.

        Поиск подстрокой выглядит работающим ровно до первого такого слова, и
        тогда фильтр молча пропускает всё подряд.
        """
        assert bot.off_topic_without_llm(
            _news('Variety', 'Manganese mining documentary lands at Sundance'))
        assert bot.off_topic_without_llm(
            _news('Variety', 'Animated Disney feature sets release date'))

    def test_russian_marker_survives_declension(self):
        """Filmix пишет по-русски, и «манга» там почти всегда в падеже.

        В заголовках нарочно нет других зацепок: маркер должен находиться
        именно в склонённом слове, иначе проверка ничего не доказывает.
        """
        for title in ('Экранизация манги выйдет следующей осенью',
                      'Автор манхвы подписал контракт со студией',
                      'Сборы аниме-новинки превысили прошлогодние',
                      'Мангака впервые показал финальный разворот'):
            assert not bot.off_topic_without_llm(_news('Filmix', title)), title

    def test_russian_source_without_marker_is_off_topic(self):
        assert bot.off_topic_without_llm(
            _news('Filmix', 'Вышел трейлер боевика с Джейсоном Стэйтемом'))


class TestMarkersInLead:
    """Границы лида заданы числами, а не константой из кода.

    Сверяться с самой константой бессмысленно: она подстроится под любое
    её изменение, и тест продолжит проходить с лидом хоть во всю статью.
    """

    def test_marker_in_lead_saves_the_post(self):
        """Кино-издание часто выносит в заголовок только название тайтла."""
        news = _news('Variety', 'Netflix orders a new series',
                     'x' * 120 + ' The anime adaptation streams in 2027.')
        assert not bot.off_topic_without_llm(news)

    def test_marker_deep_in_the_text_does_not_count(self):
        """Дальше по тексту «аниме» — это уже блок «похожие материалы»."""
        news = _news('Variety', 'Netflix orders a new series',
                     'x' * 1000 + ' anime')
        assert bot.off_topic_without_llm(news)


class TestPipeline:
    """Отсев обязан стоять в общем конвейере — и канала, и ветки."""

    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(local_topic_filter=True, llm_enabled=False))
        monkeypatch.setattr(bot, '_improve_thumb', AsyncMock(return_value=None))
        monkeypatch.setattr(bot, '_discover_article_video', AsyncMock(return_value=None))
        monkeypatch.setattr(bot, '_optimize_news_media', AsyncMock(return_value=None))
        monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
        monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
        return bot

    def _prepare(self, env, news):
        return asyncio.run(env._prepare_news_for_send(news, news['source'],
                                                      apply_dedup=False))

    def test_without_model_off_topic_is_dropped(self, env, monkeypatch):
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='off'))
        env.settings.local_topic_filter = True
        news = _news('Polygon', 'The Legend of Zelda movie casts its Link')
        assert self._prepare(env, news) == 'skipped_filter'
        env.stats.record_skipped.assert_awaited_with('filtered', 'Polygon')

    def test_without_model_anime_news_still_goes_out(self, env, monkeypatch):
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='off'))
        env.settings.local_topic_filter = True
        news = _news('Polygon', 'Frieren season 2 gets a premiere date')
        assert self._prepare(env, news) is None

    def test_live_model_decides_alone(self, env, monkeypatch):
        """Разобранную моделью новость локальный словарь трогать не смеет.

        Он грубее: у профильной новости может не быть ни одного приметного
        слова в заголовке, и поверх живой модели такой отсев начал бы выбрасывать
        как раз то, ради чего за модель платят.
        """
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='ok'))
        env.settings.local_topic_filter = True
        news = _news('Polygon', 'Netflix casts its lead for the live action film')
        assert self._prepare(env, news) is None

    def test_earlier_model_verdict_wins(self, env, monkeypatch):
        """Пост, разобранный при живой модели, кнопкой в канал уйдёт всё равно.

        В ветку он ушёл с вердиктом модели, а кнопку «📢 В канал» нажимают
        часом позже, когда провайдер уже молчит. Судить такую новость словарём
        значит отменять решение, которое принято точнее.
        """
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='off'))
        env.settings.local_topic_filter = True
        news = _news('Polygon', 'Netflix casts its lead for the live action film')
        news['_llm_topic'] = 'аниме'
        assert self._prepare(env, news) is None

    def test_deferred_news_is_not_dropped(self, env, monkeypatch):
        """Отложенная новость ждёт модель, а не приговор словаря."""
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='defer'))
        env.settings.local_topic_filter = True
        news = _news('Polygon', 'The Legend of Zelda movie casts its Link')
        assert self._prepare(env, news) == 'deferred'

    def test_switch_off_returns_the_old_behaviour(self, env, monkeypatch):
        monkeypatch.setattr(env, '_llm_enrich', AsyncMock(return_value='off'))
        env.settings.local_topic_filter = False
        news = _news('Polygon', 'The Legend of Zelda movie casts its Link')
        assert self._prepare(env, news) is None


class TestSetting:
    def test_enabled_by_default(self, tmp_path):
        fresh = bot.BotSettings(tmp_path / 'settings.json')
        assert fresh.local_topic_filter is True

    def test_toggle_survives_restart(self, tmp_path):
        path = tmp_path / 'settings.json'
        first = bot.BotSettings(path)
        first.local_topic_filter = False
        assert bot.BotSettings(path).local_topic_filter is False

    def test_menu_shows_the_switch_without_a_model(self, monkeypatch):
        """Кнопка нужна именно тогда, когда модель не настроена.

        В меню модели всё остальное в этот момент скрыто, и раньше выключатель
        уехал бы вместе с ним — то есть был бы недоступен ровно там, где он
        единственный, что работает.
        """
        monkeypatch.setattr(bot, '_llm_configured', lambda: False)
        monkeypatch.setattr(bot, 'settings',
                            MagicMock(local_topic_filter=True, translator_engine='deepl'))
        buttons = [b.callback_data for row in bot._menu_llm().inline_keyboard for b in row]
        assert 'settings:toggle_localtopic' in buttons
        assert bot._menu_for('settings:toggle_localtopic') is not None
