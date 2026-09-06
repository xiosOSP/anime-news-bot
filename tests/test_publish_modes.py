"""Три режима публикации, включая «в ветку + в канал по интервалу».

Главный риск нового режима — дубль в канале: пост из ветки может уйти туда
и по кнопке 📢, и сам по интервалу. Защищает состояние channel_state у
pending-поста, и именно оно проверяется здесь в первую очередь.
"""
import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True, scope='module')
def _globals():
    bot._init_globals()
    return True


@pytest.fixture
def mode(request):
    """Возвращает режим на место после теста: настройка глобальная."""
    before = bot.settings.publish_mode
    yield bot.settings
    bot.settings.publish_mode = before


# ---------- сама настройка ----------

@pytest.mark.parametrize('value,thread,channel', [
    ('thread', True, False),
    ('channel', False, True),
    ('both', True, True),
])
def test_mode_drives_both_flags(mode, value, thread, channel):
    """thread_mode остался вычисляемым: на него смотрят два десятка мест."""
    mode.publish_mode = value
    assert mode.thread_mode is thread
    assert mode.channel_autopost is channel


def test_old_settings_file_still_works(mode):
    """Файл настроек, записанный до появления режимов, не должен ломаться."""
    mode._data.pop('publish_mode', None)
    mode._data['thread_mode'] = True
    assert mode.publish_mode == 'thread'
    mode._data['thread_mode'] = False
    assert mode.publish_mode == 'channel'


def test_unknown_mode_is_rejected(mode):
    """Молча проглотить опечатку значило бы получить неизвестное поведение."""
    with pytest.raises(ValueError):
        mode.publish_mode = 'куда-нибудь'


def test_channel_interval_falls_back_to_collection_interval(mode):
    mode.channel_interval_min = 0
    assert mode.channel_interval_min == mode.check_interval_min
    mode.channel_interval_min = 15
    assert mode.channel_interval_min == 15
    assert mode.channel_interval_sec == 900


# ---------- выбор поста для автопубликации ----------

def _pending(tmp_path):
    return bot.PendingPosts(tmp_path / 'pending.json')


def test_autopost_takes_oldest_first(tmp_path):
    """Канал должен повторять ленту ветки, а не выдавать её вперемешку."""
    store = _pending(tmp_path)
    first = store.add({'title': 'Первый', 'link': 'https://x/1'})
    store.add({'title': 'Второй', 'link': 'https://x/2'})
    key, news = store.next_for_autopost()
    assert key == first and news['title'] == 'Первый'


def test_published_post_is_not_offered_again(tmp_path):
    """Пост, который уже публикуется, повторно выдавать нельзя — это дубль."""
    store = _pending(tmp_path)
    key = store.add({'title': 'Т', 'link': 'https://x/1'})
    assert store.mark_channel_sending(key) is True
    assert store.next_for_autopost() is None


def test_manual_button_and_autopost_cannot_both_win(tmp_path):
    """Кнопка 📢 и автопубликация занимают одно состояние.

    Кто первым перевёл pending в sending, тот и публикует; второй получает
    False и не отправляет ничего.
    """
    store = _pending(tmp_path)
    key = store.add({'title': 'Т', 'link': 'https://x/1'})
    assert store.mark_channel_sending(key) is True     # нажали кнопку
    assert store.mark_channel_sending(key) is False    # подоспел автопостинг


def test_uncertain_post_is_skipped(tmp_path):
    """Результат прошлой отправки неизвестен — трогать нельзя до проверки."""
    store = _pending(tmp_path)
    key = store.add({'title': 'Т', 'link': 'https://x/1'})
    store.mark_channel_uncertain(key)
    assert store.next_for_autopost() is None


def test_hidden_post_never_reaches_channel(tmp_path):
    """Скрытый кнопкой ✖ пост удаляется — автопостинг его не увидит."""
    store = _pending(tmp_path)
    key = store.add({'title': 'Т', 'link': 'https://x/1'})
    store.pop(key)
    assert store.next_for_autopost() is None


def test_failed_send_returns_post_to_queue(tmp_path):
    """После неудачи пост обязан снова стать кандидатом, а не потеряться."""
    store = _pending(tmp_path)
    key = store.add({'title': 'Т', 'link': 'https://x/1'})
    store.mark_channel_sending(key)
    store.mark_channel_pending(key)
    assert store.next_for_autopost()[0] == key


def test_empty_store_offers_nothing(tmp_path):
    assert _pending(tmp_path).next_for_autopost() is None
