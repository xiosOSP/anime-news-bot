"""Подготовка к публикации сразу в канал вместо ветки.

Переключатель режима — одна кнопка, но последствия несимметричны: пропадает
ручное одобрение, а темп публикации становится жёстким (один пост за интервал).
Тесты фиксируют то, что должно быть видно ДО переключения, и то, что при нём
не должно измениться.
"""
import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True, scope='module')
def _globals():
    """Хранилища бота создаются лениво в _init_globals, а тестам они нужны.

    DATA_DIR в прогоне указывает во временную папку (так же настроен CI),
    поэтому реальные данные бота не затрагиваются.
    """
    bot._init_globals()
    return True


# ---------- содержимое поста не зависит от режима ----------

def test_post_text_fits_channel_caption_with_llm():
    """В канале пост с картинкой — одно сообщение, подпись до 1024 символов.

    Ветка при длинном тексте умеет разделить медиа и текст, канал — нет.
    Поэтому максимальный ответ модели обязан влезать в подпись целиком.
    """
    news = {'title': 'X', 'summary': '', 'link': 'https://x/1', 'source': 'S',
            '_llm_text': 'А' * bot.LLM_SUMMARY_MAX, '_llm_tags': '#аниме #новости'}
    assert len(bot.format_news_short(news)) <= bot.TG_CAPTION_LIMIT


def test_post_text_fits_channel_caption_without_llm():
    """Модель может быть выключена или в паузе — пост всё равно должен влезать."""
    news = {'title': 'Очень длинный заголовок ' * 6,
            'summary': 'Предложение. ' * 60,
            'link': 'https://x/1', 'source': 'ANN', 'lang': 'ru'}
    assert len(bot.format_news_short(news)) <= bot.TG_CAPTION_LIMIT


def test_removed_length_constants_stay_removed():
    """SUMMARY_MAX_CHARS и родня не использовались, но выглядели как настройка
    длины поста для режима. При переезде в канал это ровно та деталь, из-за
    которой правят не тот параметр."""
    for dead in ('SUMMARY_MAX_CHARS', 'SUMMARY_MAX_CHARS_THREAD',
                 'TRANSLATION_INPUT_LIMIT_THREAD'):
        assert not hasattr(bot, dead), f'{dead} вернулась, ничего не ограничивая'


# ---------- потеря новостей должна быть видимой ----------

@pytest.mark.asyncio
async def test_queue_overflow_is_counted(tmp_path, monkeypatch):
    """Выброшенное из очереди — это потерянные новости, а не отложенные.

    В режиме канала публикуется один пост за интервал, поэтому переполнение
    становится нормой. Раньше оно уходило только в лог.
    """
    monkeypatch.setattr(bot.settings, '_data', dict(bot.settings._data), raising=False)
    monkeypatch.setattr(bot.settings, 'require_image', False, raising=False)
    queue = bot.PostQueue(tmp_path / 'q.json')
    rows = [{'link': f'https://example.com/{i}', 'title': f'T{i}',
             'images': ['https://img/1.jpg'], '_priority_score': i}
            for i in range(bot.QUEUE_MAX_SIZE + 15)]
    await queue.push_many(rows)
    assert queue.dropped_last_push == 15
    assert queue.dropped_total == 15


@pytest.mark.asyncio
async def test_queue_without_overflow_reports_zero(tmp_path, monkeypatch):
    """Ложная тревога о потере хуже молчания: она обесценивает настоящую."""
    monkeypatch.setattr(bot.settings, 'require_image', False, raising=False)
    queue = bot.PostQueue(tmp_path / 'q2.json')
    rows = [{'link': f'https://example.com/{i}', 'title': f'T{i}',
             'images': ['https://img/1.jpg']} for i in range(5)]
    await queue.push_many(rows)
    assert queue.dropped_last_push == 0
    assert queue.dropped_total == 0


# ---------- готовность видна до переключения ----------

def test_readiness_warns_when_channel_cannot_keep_up(monkeypatch):
    """Если источники дают больше, чем канал успевает, это надо знать заранее."""
    bot.settings.check_interval_min = 30          # 1800 с -> 48 постов в сутки
    monkeypatch.setattr(bot.stats, 'count_events_since', lambda *a, **k: 500)
    text = bot._channel_mode_readiness()
    assert 'не успеет' in text
    assert '48' in text                      # темп назван числом, а не словами


def test_readiness_confirms_when_pace_is_enough(monkeypatch):
    bot.settings.check_interval_min = 30
    monkeypatch.setattr(bot.stats, 'count_events_since', lambda *a, **k: 10)
    assert 'Темпа хватает' in bot._channel_mode_readiness()


def test_readiness_flags_missing_review_thread(monkeypatch):
    """Без настроенной ветки в канал уходит всё, включая спорное."""
    monkeypatch.setattr(bot, 'DISCUSSION_CHAT_FROM_ENV', False)
    monkeypatch.setattr(bot, 'DISCUSSION_THREAD_FROM_ENV', False)
    assert 'без ручной проверки' in bot._channel_mode_readiness()


def test_readiness_survives_broken_stats(monkeypatch):
    """Статистика не должна ронять /status: блок вспомогательный."""
    def boom(*a, **k):
        raise RuntimeError('stats недоступны')
    monkeypatch.setattr(bot.stats, 'count_events_since', boom)
    assert 'Готовность' in bot._channel_mode_readiness()
