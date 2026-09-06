"""Общие фикстуры для тестов."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Делаем доступным импорт anime_news_bot из родительской папки
sys.path.insert(0, str(Path(__file__).parent.parent))

# Бот кладёт свои JSON-файлы в DATA_DIR, а без переменной — в текущую папку.
# Прогон тестов писал их прямо в репозиторий: хранилища одного файла тестов
# доставались другому, и часть падений зависела от порядка файлов. Уводим
# данные во временную папку ДО импорта: пути считаются один раз при импорте.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix='anime-bot-tests-')
os.environ.setdefault('DATA_DIR', _TEST_DATA_DIR)

import anime_news_bot as _bot


@pytest.fixture
def tmp_json(tmp_path):
    """Возвращает путь к новому несуществующему JSON-файлу.
    Каждому тесту — свой файл, чтобы тесты были изолированы."""
    return tmp_path / "test.json"


_MISSING = object()
_SKIP_PREFIXES = ('__',)


def _wipe_data_dir() -> None:
    """Убирает рабочие файлы бота между тестами.

    Хранилища переживают тест не только в памяти, но и на диске: реестр
    историй дописывался из теста в тест, и размер кластера приезжал в
    следующий тест уже вчетверо больше. В одиночку такой тест проходит —
    ломается только весь прогон целиком.
    """
    for path in Path(_TEST_DATA_DIR).glob('*'):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _isolate_bot_globals():
    """Возвращает состояние модуля бота после каждого теста.

    Часть тестов меняет глобальные объекты и константы напрямую
    (`bot.settings = MagicMock(...)`, `bot.SOURCE_WALL_TIMEOUT = 0`), минуя
    monkeypatch, и подмена переживала тест. Ловил это уже совсем другой файл:
    _init_globals пересоздаёт только то, что равно None, поэтому один
    MagicMock из середины прогона доезжал до конца прогона. Результат зависел
    от порядка файлов — худший вид падения, потому что в одиночку тест
    проходит и воспроизвести нечего.

    Снимок делается после фикстур более широкой области видимости, поэтому
    хранилища, созданные модульным _init_globals, в него уже входят и
    восстановление их не сбрасывает.
    """
    saved = {name: value for name, value in vars(_bot).items()
             if not name.startswith(_SKIP_PREFIXES)}
    # Отдельно — содержимое изменяемых контейнеров. Восстановления ссылки мало:
    # FEATURE_FLAGS и кеши правят по месту, ссылка при этом та же самая, и
    # флаг, выключенный в середине прогона, доезжал до конца. Именно так
    # ломался дедуп по теме: тест в одиночку проходил, в общем прогоне нет.
    contents = {name: value.copy() for name, value in saved.items()
                if isinstance(value, (dict, list, set))}
    _wipe_data_dir()
    yield
    _wipe_data_dir()
    current = vars(_bot)
    for name, value in saved.items():
        if current.get(name, _MISSING) is not value:
            setattr(_bot, name, value)
    for name, original in contents.items():
        live = getattr(_bot, name, None)
        if live is None or live == original:
            continue
        try:
            live.clear()
            live.update(original) if isinstance(live, (dict, set)) else live.extend(original)
        except (AttributeError, TypeError):
            setattr(_bot, name, original)
    for name in [n for n in current
                 if n not in saved and not n.startswith(_SKIP_PREFIXES)]:
        try:
            delattr(_bot, name)
        except AttributeError:
            pass


class FakeHTTPResponse:
    """Ответ, который умеет то же, что настоящий: потоковое чтение.

    Скачивание картинок перешло на потоковое чтение с лимитом по объёму, а
    тесты продолжали подсовывать MagicMock с готовым `content`. Такой мок
    молча отдавал пустое тело — тест падал так, будто сломался код, хотя
    устарел сам мок.
    """

    def __init__(self, content=b'', status_code=200, headers=None, text=''):
        self.content = content
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def json(self):
        import json
        return json.loads(self.text or self.content or b'{}')

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def http_response():
    """Фабрика потоковых ответов для тестов загрузки."""
    return FakeHTTPResponse
