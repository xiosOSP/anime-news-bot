"""Разные интервалы для ветки и канала.

Смысл режима «и в ветку, и в канал» в том, что у площадок разный темп: в
ветку модераторам можно часто, в канал подписчикам — редко. Тесты сторожат
именно независимость: отправка в одно место не должна двигать расписание
другого.
"""
from datetime import datetime, timedelta, timezone

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True, scope='module')
def _globals():
    bot._init_globals()
    return True


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    store = bot.BotSettings(tmp_path / 's.json')
    store.publish_mode = 'both'
    store.check_interval_min = 30        # ветка
    store.channel_interval_min = 120     # канал
    monkeypatch.setattr(bot, 'settings', store)
    return store


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.mark.asyncio
async def test_thread_post_does_not_delay_the_channel(_settings):
    """Пост в ветку не сдвигает расписание канала.

    Обе отметки времени жили в одном поле last_publish_at. Ветка при интервале
    30 мин обновляла его чаще, чем наступал двухчасовой срок канала, — и канал
    не получал ни одного поста вообще. Ровно та конфигурация, ради которой
    режим и существует.
    """
    _settings.last_channel_post_at = _ago(180)   # канал молчит три часа
    bot._mark_published()                        # только что ушло в ветку
    assert await bot._channel_autopost_due() is True


@pytest.mark.asyncio
async def test_channel_waits_out_its_own_interval(_settings):
    """Свой интервал канал всё-таки выдерживает."""
    _settings.last_channel_post_at = _ago(60)
    assert await bot._channel_autopost_due() is False
    _settings.last_channel_post_at = _ago(121)
    assert await bot._channel_autopost_due() is True


@pytest.mark.asyncio
async def test_first_channel_post_needs_no_history(_settings):
    """Без отметки публикуем сразу, а не ждём интервал впустую."""
    _settings.last_channel_post_at = ''
    assert await bot._channel_autopost_due() is True


@pytest.mark.asyncio
async def test_broken_timestamp_does_not_freeze_the_channel(_settings):
    """Мусор в настройках не должен останавливать публикации навсегда."""
    _settings.last_channel_post_at = 'не дата'
    assert await bot._channel_autopost_due() is True


def test_intervals_are_independent_settings(_settings):
    """Два разных числа, а не одно на двоих."""
    assert _settings.check_interval_min == 30
    assert _settings.channel_interval_min == 120
    _settings.check_interval_min = 45
    assert _settings.channel_interval_min == 120


def test_channel_interval_falls_back_to_collection(_settings):
    """Ноль означает «как интервал сбора» — старое поведение сохраняем."""
    _settings.channel_interval_min = 0
    assert _settings.channel_interval_min == _settings.check_interval_min


@pytest.mark.asyncio
async def test_manual_channel_post_also_starts_the_clock(_settings, monkeypatch):
    """Ручная кнопка «📢 В канал» тоже сдвигает расписание.

    Отметка ставится в одном месте — там, где пост реально ушёл в канал.
    Иначе автопостинг мог добавить второй пост через минуту после ручного,
    хотя интервал канала обещает обратное.
    """
    _settings.last_channel_post_at = _ago(300)

    async def _sent(*a, **k):
        return True

    async def _no_video(*a, **k):
        return None

    monkeypatch.setattr(bot, '_send_channel_post', _sent)
    monkeypatch.setattr(bot, '_prepare_video_file', _no_video)
    assert await bot._prepare_and_send_channel_post(None, {'title': 'x'}) is True
    assert await bot._channel_autopost_due() is False


@pytest.mark.asyncio
async def test_failed_channel_send_does_not_start_the_clock(_settings, monkeypatch):
    """Неудачная отправка не считается публикацией: канал не должен молчать 2 часа зря."""
    _settings.last_channel_post_at = _ago(300)

    async def _failed(*a, **k):
        return False

    async def _no_video(*a, **k):
        return None

    monkeypatch.setattr(bot, '_send_channel_post', _failed)
    monkeypatch.setattr(bot, '_prepare_video_file', _no_video)
    assert await bot._prepare_and_send_channel_post(None, {'title': 'x'}) is False
    assert await bot._channel_autopost_due() is True


# ---------- как это выглядит в меню ----------

def test_menu_shows_both_intervals_side_by_side(_settings):
    """Смысл режима в разном темпе — значит оба числа нужны на одном экране."""
    labels = [b.text for row in bot._menu_posts().inline_keyboard for b in row]
    assert any('Ветка: 30 мин' in x for x in labels)
    assert any('Канал: 2 ч' in x for x in labels)


def test_menu_hides_channel_interval_when_it_does_nothing(_settings):
    """В режиме «только в ветку» кнопка интервала канала ничего не меняет."""
    _settings.publish_mode = 'thread'
    labels = [b.text for row in bot._menu_posts().inline_keyboard for b in row]
    assert not any('Канал' in x for x in labels)
    assert any('В ветку: раз в 30 мин' in x for x in labels)


def test_interval_button_names_the_place_not_the_mechanics(_settings):
    """«Интервал сбора» не отвечал на вопрос, куда пойдут посты."""
    _settings.publish_mode = 'channel'
    labels = [b.text for row in bot._menu_posts().inline_keyboard for b in row]
    assert any('В канал: раз в' in x for x in labels)


def test_overview_answers_what_the_bot_is_doing(_settings):
    """Главный экран отвечает на вопрос, с которым в настройки и заходят."""
    text = bot._settings_overview()
    assert 'в ветку раз в 30 мин' in text
    assert 'в канал раз в 2 ч' in text


@pytest.mark.parametrize('minutes,expected', [
    (30, '30 мин'), (60, '1 ч'), (90, '1 ч 30 мин'), (120, '2 ч'), (360, '6 ч'),
])
def test_time_is_formatted_the_same_everywhere(minutes, expected):
    """Одна настройка не должна выглядеть двумя разными: было «120 мин» и «2 ч»."""
    assert bot._fmt_minutes(minutes) == expected


def test_channel_menu_offers_the_two_hour_step(_settings):
    """Два часа — ровно тот интервал, ради которого всё затевалось."""
    values = [b.callback_data for row in bot.build_channel_interval_menu().inline_keyboard
              for b in row]
    assert 'chint:120' in values
