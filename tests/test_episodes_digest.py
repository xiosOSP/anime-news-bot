"""Рубрика «Серии на сегодня»: расписание серий из календаря Shikimori.

Календарь — бесплатный API без ключа (https://shikimori.io/api/calendar):
у каждого тайтла следующая серия и время её показа в Японии. Строки ниже —
урезанный настоящий ответ API от 23 сентября 2026 года.
"""
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram.error import RetryAfter

import anime_news_bot as bot

MSK = ZoneInfo('Europe/Moscow')


def _row(at, number, russian, *, episodes=0, score='7.0', kind='tv', status='ongoing',
         aired_on=None, name='Romaji'):
    anime = {'id': 1, 'name': name, 'russian': russian, 'kind': kind, 'score': score,
             'status': status, 'episodes': episodes, 'aired_on': aired_on}
    return {'next_episode': number, 'next_episode_at': at, 'duration': 1440, 'anime': anime}


CALENDAR = [
    _row('2026-09-24T15:30:00.000+03:00', 13, 'Дара из Рэйвы', episodes=13, score='7.02'),
    _row('2026-09-24T17:56:00.000+03:00', 12, 'История о перекуре за супермаркетом',
         episodes=12, score='8.31'),
    _row('2026-09-24T01:30:00.000+03:00', 1180, 'Ван-Пис', score='8.7'),
    _row('2026-09-25T17:00:00.000+03:00', 24, 'О моём перерождении в слизь 4', episodes=24),
    # Анонс: время 09:00 у всех анонсов — заглушка Shikimori, известна только дата.
    _row('2026-09-24T09:00:00.000+03:00', 1, 'Шальной последний босс явился! 2',
         score='0.0', status='anons', aired_on='2026-09-24'),
    _row('2026-09-24T18:00:00.000+03:00', 3, 'Клип группы', kind='music'),
]


def _text(day='2026-09-24', tz=MSK, calendar=CALENDAR):
    return bot._episodes_digest_text(calendar, date.fromisoformat(day), tz)


def test_todays_post():
    assert _text() == (
        '📅 Серии на сегодня, 24 сентября\n'
        '\n'
        '01:30 — «Ван-Пис», 1180 серия\n'
        '15:30 — «Дара из Рэйвы», 13 серия (финал)\n'
        '17:56 — «История о перекуре за супермаркетом», 12 серия (финал)\n'
        '\n'
        '🆕 Премьеры:\n'
        '«Шальной последний босс явился! 2»\n'
        '\n'
        'Время показа в Японии, по Москве. Данные Shikimori.')


def test_time_and_day_follow_the_admin_time_zone():
    """01:30 по Москве — это ещё 23 сентября в Лос-Анджелесе."""
    la = ZoneInfo('America/Los_Angeles')
    assert '15:30 — «Ван-Пис»' in _text('2026-09-23', la)
    assert 'Ван-Пис' not in _text('2026-09-24', la)
    assert 'по времени America/Los_Angeles.' in _text('2026-09-23', la)


def test_premiere_keeps_its_date_and_gets_no_invented_time():
    """Западнее Москвы заглушка 09:00 уехала бы на вчерашний день."""
    la_day = _text('2026-09-24', ZoneInfo('America/Los_Angeles'))
    assert '«Шальной последний босс явился! 2»' in la_day
    assert '09:00' not in _text() and '23:00' not in la_day


def test_only_premieres_do_not_promise_a_broadcast_time():
    text = _text(calendar=[CALENDAR[4]])
    assert text.endswith('🆕 Премьеры:\n«Шальной последний босс явился! 2»\n\nДанные Shikimori.')


def test_music_clips_are_not_episodes():
    assert 'Клип группы' not in _text()


def test_day_without_episodes_is_empty():
    assert _text('2026-09-30') == ''


def test_broken_rows_are_skipped():
    broken = [None, {'anime': None}, {'next_episode': 2, 'anime': {'russian': 'Без даты'}},
              _row('вчера', 2, 'Плохая дата'),
              _row('2026-09-24T12:00:00+03:00', 0, 'Нет номера'),
              _row('2026-09-24T12:00:00+03:00', 'n', 'Номер словом'),
              _row('2026-09-24T12:00:00+03:00', 2, '', name='')]
    assert _text(calendar=broken + [CALENDAR[0]]) == (
        '📅 Серии на сегодня, 24 сентября\n\n15:30 — «Дара из Рэйвы», 13 серия (финал)\n\n'
        'Время показа в Японии, по Москве. Данные Shikimori.')


def test_romaji_name_when_there_is_no_russian_one():
    assert '«Kimi to Boku»' in _text(calendar=[_row('2026-09-24T12:00:00+03:00', 2, '', name='Kimi to Boku')])


def test_busy_day_keeps_the_best_rated_in_time_order(monkeypatch):
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_MAX', 3)
    calendar = [_row(f'2026-09-24T1{h}:00:00+03:00', 2, f'Тайтл {h}', score=str(score))
                for h, score in ((1, 6.0), (2, 8.0), (3, 5.0), (4, 9.0), (5, 7.0))]
    lines = _text(calendar=calendar).splitlines()
    assert lines[2:6] == ['12:00 — «Тайтл 2», 2 серия', '14:00 — «Тайтл 4», 2 серия',
                          '15:00 — «Тайтл 5», 2 серия', '…и ещё 2']


def test_season_start_premieres_are_capped(monkeypatch):
    monkeypatch.setattr(bot, 'EPISODES_PREMIERES_MAX', 2)
    calendar = [_row('2026-10-04T09:00:00+03:00', 1, name, status='anons', aired_on='2026-10-04')
                for name in ('Ао Аси 2', 'Голубая шкатулка 2', 'Тануки и лис')]
    assert _text('2026-10-04', calendar=calendar).splitlines()[2:6] == [
        '🆕 Премьеры:', '«Ао Аси 2»', '«Голубая шкатулка 2»', '…и ещё 1']


def test_long_name_is_cut_at_a_word():
    name = ('Изгнанный реинкарнированный тяжёлый рыцарь не имеет себе равных '
            'в прокачке характеристик')
    line = _text(calendar=[_row('2026-09-24T18:26:00+03:00', 13, name)]).splitlines()[2]
    assert line == '18:26 — «Изгнанный реинкарнированный тяжёлый рыцарь не имеет себе…», 13 серия'


def test_quotes_inside_the_name_become_inner_quotes():
    line = _text(calendar=[_row('2026-09-24T18:00:00+03:00', 13, 'Шоу для взрослых: Цирк «Подсолнух»')])
    assert '«Шоу для взрослых: Цирк „Подсолнух“»' in line


# ---------- когда и куда ----------

@pytest.mark.parametrize(('value', 'clock'), [
    ('10:00', (10, 0)), ('9:30', (9, 30)), ('25:00', (10, 0)), ('10:75', (10, 0)),
    ('утром', (10, 0)), ('9', (10, 0)),
])
def test_digest_time_setting(monkeypatch, value, clock):
    """Опечатка не выключает рубрику навсегда: «25:00» не наступило бы никогда."""
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_TIME', value)
    assert bot._episodes_digest_clock() == clock


def test_new_rubric_is_off_until_the_admin_turns_it_on(tmp_path):
    assert bot.BotSettings(tmp_path / 's.json').episodes_digest is False


def test_switch_is_in_the_posts_menu(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    buttons = [b.callback_data for row in bot._menu_posts().inline_keyboard for b in row]
    assert 'settings:toggle_episodes' in buttons
    assert bot._TOGGLE_SECTION['toggle_episodes'] == 'posts'


@pytest.fixture
def job(monkeypatch, tmp_path):
    state = NS(now=datetime(2026, 9, 24, 10, 5), calendar=CALENDAR, fetches=0,
               settings=NS(episodes_digest=True, auto_enabled=True, thread_mode=True,
                           channel_autopost=False),
               send=AsyncMock(return_value=NS(message_id=77)))

    def fetch():
        state.fetches += 1
        return state.calendar
    monkeypatch.setattr(bot, 'settings', state.settings)
    monkeypatch.setattr(bot, '_local_now', lambda: state.now)
    monkeypatch.setattr(bot, '_admin_tz', lambda: MSK)
    monkeypatch.setattr(bot, '_fetch_episode_calendar', fetch)
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_FILE', tmp_path / 'episodes_digest.json')
    monkeypatch.setattr(bot, 'EPISODES_DIGEST_TIME', '10:00')
    state.context = NS(bot=NS(send_message=state.send))
    state.run = lambda: bot.episodes_digest_job(state.context)
    return state


@pytest.mark.asyncio
async def test_posts_once_a_day_to_the_news_thread(job):
    await job.run()
    job.send.assert_awaited_once()
    args, kwargs = job.send.await_args
    assert args[0] == bot.DISCUSSION_CHAT_ID and args[1].startswith('📅 Серии на сегодня, 24 сентября')
    assert kwargs['message_thread_id'] == bot.DISCUSSION_THREAD_ID
    await job.run()                                   # следующий тик того же дня
    assert job.send.await_count == 1 and job.fetches == 1
    job.now = datetime(2026, 9, 25, 10, 0)            # назавтра — снова
    await job.run()
    assert job.send.await_count == 2 and '25 сентября' in job.send.await_args.args[1]


@pytest.mark.asyncio
async def test_waits_for_the_time(job):
    job.now = datetime(2026, 9, 24, 9, 55)
    await job.run()
    assert job.fetches == 0 and not job.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize('switch', ['episodes_digest', 'auto_enabled'])
async def test_nothing_without_the_rubric_or_autoposting(job, switch):
    setattr(job.settings, switch, False)
    await job.run()
    assert job.fetches == 0 and not job.send.await_count


@pytest.mark.asyncio
async def test_channel_mode_posts_to_the_channel(job):
    job.settings.thread_mode, job.settings.channel_autopost = False, True
    await job.run()
    args, kwargs = job.send.await_args
    assert args[0] == bot.CHANNEL_ID and 'message_thread_id' not in kwargs


@pytest.mark.asyncio
async def test_both_mode_posts_to_both(job):
    job.settings.channel_autopost = True
    await job.run()
    assert [call.args[0] for call in job.send.await_args_list] == [bot.DISCUSSION_CHAT_ID, bot.CHANNEL_ID]


@pytest.mark.asyncio
async def test_shikimori_down_is_retried_on_the_next_tick(job):
    job.calendar = None
    await job.run()
    assert not job.send.await_count and not bot.EPISODES_DIGEST_FILE.exists()
    job.calendar = CALENDAR
    await job.run()
    job.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_send_is_retried_on_the_next_tick(job):
    # Отказ, после которого сообщения точно нет (флуд-лимит), повторяется.
    # Раньше здесь был NetworkError, но он неоднозначен: Telegram мог принять
    # сообщение, и повтор давал дубль. Теперь такой день закрывается как
    # «неизвестно» — это проверяет tests/test_reliability_audit.py.
    job.send.side_effect = [RetryAfter(30), NS(message_id=78)]
    await job.run()
    assert not bot.EPISODES_DIGEST_FILE.exists()
    await job.run()
    assert job.send.await_count == 2 and bot.EPISODES_DIGEST_FILE.exists()


@pytest.mark.asyncio
async def test_empty_day_is_not_fetched_every_ten_minutes(job):
    job.now = datetime(2026, 9, 30, 10, 0)
    await job.run()
    await job.run()
    assert job.fetches == 1 and not job.send.await_count


# ---------- запрос к Shikimori ----------

@pytest.mark.parametrize(('response', 'expected'), [
    (NS(status_code=200, text='[{"next_episode": 1}]', close=lambda: None), [{'next_episode': 1}]),
    (NS(status_code=200, text='{"error": "rate limit"}', close=lambda: None), None),
    (NS(status_code=200, text='<html>', close=lambda: None), None),
    (NS(status_code=503, text='[]', close=lambda: None), None),
    (None, None),
])
def test_calendar_request(monkeypatch, response, expected):
    seen = []

    def get(url, **kwargs):
        seen.append(url)
        return response
    monkeypatch.setattr(bot, 'http_get_with_retry', get)
    assert bot._fetch_episode_calendar() == expected
    assert seen == [bot.EPISODES_CALENDAR_URL]


def test_calendar_hides_adult_titles_explicitly():
    """Пост уходит в канал: 18+ скрываем явно, а не полагаемся на умолчание API."""
    assert 'censored=true' in bot.EPISODES_CALENDAR_URL


@pytest.mark.asyncio
async def test_job_is_registered_at_start(monkeypatch, tmp_path):
    settings = bot.BotSettings(tmp_path / 's.json')
    monkeypatch.setattr(bot, 'settings', settings)
    monkeypatch.setattr(bot, '_start_event_loop_lag_monitor', lambda: None)
    monkeypatch.setattr(bot, '_check_channel_access', AsyncMock(return_value=(True, '')))
    monkeypatch.setattr(bot, 'send_startup_report', AsyncMock())
    calls = []
    queue = NS(run_repeating=lambda callback, **kwargs: calls.append((callback, kwargs)))
    app = NS(job_queue=queue, bot=MagicMock(set_my_commands=AsyncMock()))
    await bot.setup_bot_commands(app)
    jobs = {kwargs['name']: callback for callback, kwargs in calls}
    assert jobs['episodes_digest'] is bot.episodes_digest_job
