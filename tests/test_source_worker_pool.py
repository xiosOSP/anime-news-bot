"""Зависший источник не должен отравлять весь пул сбора.

Найдено экспериментом: `_source_worker_try_acquire` проверял только общий
счётчик, а имя клал в set. Зависший парсер держит слот до конца своей жизни
(поток в Python не убить), поэтому каждый цикл запускал ещё одну копию того же
источника. За пять циклов один сломанный сайт забивал пул целиком, и здоровые
источники переставали собираться вовсе — молча, без единой внятной ошибки.
"""
import asyncio
import threading

import pytest

import anime_news_bot as bot


def _reset_pool():
    # getattr, чтобы тест проверял поведение, а не наличие новых полей:
    # на сборке без защиты он обязан падать на assert, а не на setup.
    with bot._source_worker_lock:
        bot._source_worker_active = 0
        bot._source_worker_names.clear()
        since = getattr(bot, '_source_worker_since', None)
        if since is not None:
            since.clear()


@pytest.fixture(autouse=True)
def _clean_pool():
    _reset_pool()
    yield
    _reset_pool()


@pytest.fixture
def hung():
    """Парсер, который игнорирует таймауты, как это делают живые сайты."""
    stop = threading.Event()
    yield lambda: stop.wait(30), stop
    stop.set()


class TestHungSourceCannotStarveThePool:
    async def test_same_source_is_not_started_twice(self, hung):
        collector, _stop = hung
        with pytest.raises(TimeoutError):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)
        assert bot._source_worker_active == 1

        # Второй цикл: тот же источник ещё висит, новый запуск запрещён.
        # Отказ здесь другого рода, чем в первый раз, и это важно: занятый
        # нами же поток — вина пула, а не источника, поэтому SourceWorkerBusy
        # не идёт источнику в счёт отказов и не отправляет его на паузу.
        with pytest.raises(bot.SourceWorkerBusy):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)
        assert bot._source_worker_active == 1, 'зависший источник занял второй слот'

    async def test_busy_is_not_charged_to_the_source(self, hung):
        """Отказ по вине пула — не отказ источника.

        Иначе один зависший сбор набирал источнику ошибок и отключал его
        автопаузой, хотя источник исправен.
        """
        collector, _stop = hung
        with pytest.raises(TimeoutError):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)
        with pytest.raises(bot.SourceWorkerUnavailable):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)

    async def test_healthy_sources_keep_working(self, hung):
        collector, _stop = hung
        with pytest.raises(TimeoutError):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)
        for _ in range(5):
            with pytest.raises(bot.SourceWorkerBusy):
                await bot._run_source_collector_bounded('ANN', collector, 0.6)

        assert bot._source_worker_active == 1
        result = await bot._run_source_collector_bounded(
            'Crunchyroll', lambda: ['news'], 2.0)
        assert result == ['news'], 'здоровый источник не смог собраться'

    async def test_pool_is_reported_for_diagnostics(self, hung, monkeypatch):
        collector, _stop = hung
        with pytest.raises(TimeoutError):
            await bot._run_source_collector_bounded('ANN', collector, 0.6)
        monkeypatch.setattr(bot, 'SOURCE_FETCH_WALL_TIMEOUT', 0)
        assert hasattr(bot, '_source_worker_stuck'), 'нет диагностики зависших сборов'
        stuck = bot._source_worker_stuck()
        assert [name for name, _sec in stuck] == ['ANN']


class TestNormalOperationIsUnaffected:
    async def test_sequential_runs_of_one_source(self):
        for i in range(4):
            result = await bot._run_source_collector_bounded(
                'ANN', lambda i=i: [f'item{i}'], 2.0)
            assert result == [f'item{i}']
        assert bot._source_worker_active == 0

    async def test_different_sources_run_in_parallel(self):
        results = await asyncio.gather(*(
            bot._run_source_collector_bounded(f'src{i}', lambda: ['ok'], 3.0)
            for i in range(bot.SOURCE_FETCH_CONCURRENCY)))
        assert all(r == ['ok'] for r in results)
        assert bot._source_worker_active == 0

    async def test_crashed_parser_frees_its_slot(self):
        def boom():
            raise ValueError('парсер сломался')

        with pytest.raises(ValueError):
            await bot._run_source_collector_bounded('broken', boom, 2.0)
        assert bot._source_worker_active == 0

        # И тот же источник можно запустить снова: падение не блокирует навсегда.
        assert await bot._run_source_collector_bounded(
            'broken', lambda: ['recovered'], 2.0) == ['recovered']

    async def test_release_is_idempotent(self):
        """Двойное освобождение не должно увести счётчик в минус."""
        bot._source_worker_try_acquire('x')
        bot._source_worker_release('x')
        bot._source_worker_release('x')
        assert bot._source_worker_active == 0
        assert 'x' not in bot._source_worker_names
