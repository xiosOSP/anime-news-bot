"""Находки аудита.

Каждый тест здесь закрывает конкретную ошибку, найденную разбором, а не
гипотезу. Формулировки описывают следствие: по ним видно, что именно
сломается, если проверку убрать.
"""
import threading
import time
from pathlib import Path


import anime_news_bot as bot


def _slow_read(monkeypatch):
    """Растягивает чтение внутри «прочитать, прибавить, записать».

    Без этого гонку не увидеть: операции слишком коротки, GIL успевает
    провести их целиком, и тест зеленел бы даже на коде без блокировки.
    """
    real = bot._safe_nonnegative_int

    def slow(*args, **kwargs):
        value = real(*args, **kwargs)
        time.sleep(0.001)
        return value

    monkeypatch.setattr(bot, '_safe_nonnegative_int', slow)


def _run_together(worker, count: int) -> None:
    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------- кеш статей: неудачи копились мимо потолка ----------

def test_failed_article_marks_respect_the_cache_cap(monkeypatch):
    """Пометки о неудачных статьях писались напрямую, минуя ограничитель.

    Неудач больше, чем удач — мёртвые ссылки, пейволы, таймауты, — и они
    копились до конца жизни процесса. Потолок при этом существовал и честно
    работал, но только для удачных разборов.
    """
    monkeypatch.setattr(bot, '_article_text_cache', {})
    monkeypatch.setattr(bot, 'ARTICLE_CACHE_MAX', 10)
    for i in range(50):
        bot._bounded_cache_put(bot._article_text_cache, f'http://x/{i}', '',
                               bot.ARTICLE_CACHE_MAX)
    assert len(bot._article_text_cache) <= 10


def test_no_direct_writes_to_the_article_cache():
    """Прямая запись в обход ограничителя — это и была вся ошибка."""
    source = Path(bot.__file__).read_text(encoding='utf-8')
    assert '_article_text_cache[url] =' not in source, \
        'запись мимо _bounded_cache_put вернулась'


# ---------- счётчики: чтение-изменение-запись из потока ----------

def test_deepl_counter_does_not_lose_updates(tmp_path, monkeypatch):
    """Счётчик символов DeepL правится из рабочего потока.

    format_news_short уходит в asyncio.to_thread и переводит по сети, то есть
    «прочитать, прибавить, записать» шло без блокировки. Потерянные обновления
    здесь означают заниженный расход — и молчаливый выход за лимит DeepL,
    ровно то, что этот счётчик обязан предотвращать.

    Окно гонки расширяем намеренно: без паузы между чтением и записью GIL
    прячет её почти всегда, и тест проходил бы и на сломанном коде — то есть
    не проверял бы ничего.
    """
    store = bot.BotSettings(tmp_path / 's.json')
    _slow_read(monkeypatch)

    def worker():
        for _ in range(25):
            store.add_deepl_chars('2026-09', 1)

    _run_together(worker, 4)
    assert store.deepl_chars == 100, f'потеряно обновлений: {100 - store.deepl_chars}'


def test_llm_counter_does_not_lose_updates(tmp_path, monkeypatch):
    """Тот же счётчик, по которому бот решает, не пора ли остановиться."""
    store = bot.BotSettings(tmp_path / 's.json')
    _slow_read(monkeypatch)

    def worker():
        for _ in range(25):
            store.increment_llm_call('2026-09-07')

    _run_together(worker, 4)
    assert store.llm_calls_today == 100


def test_admin_list_survives_concurrent_writes(tmp_path):
    """Список админов — тоже чтение-изменение-запись, но менее опасная.

    В отличие от счётчиков, здесь потерять нечего: append у списка атомарен,
    и без блокировки этот тест тоже зелёный — проверено мутацией. Замок в
    add_admin поставлен ради единообразия с остальными составными правками,
    а тест сторожит само свойство: параллельная запись не теряет записей и
    не оставляет список порванным.
    """
    store = bot.BotSettings(tmp_path / 's.json')

    def worker(base):
        for i in range(50):
            store.add_admin(base + i)

    threads = [threading.Thread(target=worker, args=(b,)) for b in (1000, 2000, 3000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.extra_admins) == 150


# ---------- перебор моделей у ненастроенного слота ----------

def test_alternates_are_not_offered_for_an_unconfigured_slot(monkeypatch, tmp_path):
    """Запрос ушёл бы на пустой адрес с пустым ключом.

    Это гарантированный отказ, который вдобавок засчитывается в серию неудач
    и открывает circuit — провайдеру, который вообще не был настроен.
    """
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'LLM_BASE_URL', '')
    monkeypatch.setattr(bot, 'LLM_API_KEY', '')
    monkeypatch.setattr(bot, 'LLM_MODEL', '')
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ('qwen/qwen3.8-27b-free',))
    monkeypatch.setattr(bot, 'LLM_FALLBACK_BASE_URL', 'https://two.test/v1')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_API_KEY', 'key-two-bbbbbbbb')
    monkeypatch.setattr(bot, 'LLM_FALLBACK_MODEL', 'mistral-small-latest')

    for slot, _model in bot._llm_candidates():
        base_url, api_key, _ = bot._llm_slot_env(slot)
        assert base_url and api_key, f'кандидат без адреса или ключа: {slot}'


def test_alternates_still_work_for_a_configured_slot(monkeypatch, tmp_path):
    """Проверка не должна заодно выключить саму возможность."""
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://one.test/v1')
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'key-one-aaaaaaaa')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'model-one')
    monkeypatch.setattr(bot, 'LLM_MODEL_ALTERNATES', ('qwen/qwen3.8-27b-free',))
    assert ('primary', 'qwen/qwen3.8-27b-free') in bot._llm_candidates()


# ---------- хранилище рекомендаций читал поток дашборда ----------

def test_adaptive_store_is_safe_across_threads(tmp_path):
    """Единственное хранилище, где два потока встречались без защиты.

    Пишет event loop, читает поток дашборда: список рекомендаций уходит в
    /health-payload. Крах здесь маловероятен — списки под GIL не рвутся, как
    словари, — поэтому тест проверяет не падение, а что параллельное чтение не
    видит порванного состояния и потолок держится. Само наличие замка сторожит
    отдельный инвариант: правило «есть данные и запись на диск — есть замок»
    не зависит от того, удалось ли воспроизвести гонку.
    """
    store = bot.AdaptivePublishingStore(tmp_path / 'a.json')
    errors = []

    def writer():
        for i in range(300):
            store.record({'recommended_interval_min': i})

    def reader():
        try:
            for _ in range(300):
                store.latest()
                store.history(20)
        except Exception as e:                     # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(store.history(1000)) <= store.MAX_HISTORY


# ---------- переменные, без которых бот не настроить ----------

def test_operator_variables_are_documented():
    """Настройка провайдера — то, на чём человек застревает первым делом.

    Список нарочно короткий: это не все переменные, а те, без которых бот не
    заводится или остаётся без модели.
    """
    doc = Path(bot.__file__).with_name('.env.example').read_text(encoding='utf-8')
    for name in ('LLM_PROVIDER', 'LLM_API_KEY', 'LLM_FALLBACK_PROVIDER',
                 'LLM_FAST_PROVIDER', 'LLM_MODEL_ALTERNATES', 'DEEPL_API_KEY',
                 'FEATURE_CHAT_MODERATION'):
        assert name in doc, f'{name} не описан в .env.example'
