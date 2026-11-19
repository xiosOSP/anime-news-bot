"""Окружение дочерних процессов: только то, что им нужно для работы.

Загрузчик видео и воркер модерации медиа разбирают чужие данные — ролики со
сторонних сайтов и картинки участников чата. Раньше они получали окружение
бота целиком или почти целиком: загрузчик — вместе с BOT_TOKEN и ключами
моделей, воркер — со строкой сессии Telegram-аккаунта (TG_ACCOUNT_SESSION,
это полный доступ к аккаунту), потому что фильтр по словам «TOKEN», «KEY»
её не узнавал (внешний аудит, ноябрь 2026). Уязвимость в разборе файла
отдавала бы эти секреты. Теперь список разрешённых имён, а не запрещённых:
новая переменная с секретом не утечёт, даже если её никто не догадался
запретить.
"""
import os

# Без них процесс не запустится или не найдёт свои файлы: поиск программ,
# домашний каталог (кеш yt-dlp), язык и кодировка, временные файлы, системные
# переменные Windows.
_NAMES = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LANGUAGE', 'TZ',
    'TMPDIR', 'TEMP', 'TMP', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH',
    'VIRTUAL_ENV', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'PROGRAMDATA',
    # Кеш движка Deno, на котором yt-dlp решает проверки YouTube. Всё семейство
    # DENO_ не пропускаем: в DENO_AUTH_TOKENS лежат токены.
    'DENO_DIR', 'DENO_INSTALL',
})
# Целые семейства: локаль, Python, каталоги XDG, потоки и память библиотек
# разбора (их вызывающий задаёт и сам), путь к ffmpeg из imageio-ffmpeg.
_PREFIXES = ('LC_', 'PYTHON', 'XDG_', 'OMP_', 'OPENBLAS_', 'MKL_',
             'OPENCV_', 'MALLOC_', 'IMAGEIO_')
# Сеть: прокси хостинга и сертификаты. Только тем, кто ходит в сеть.
_NETWORK_NAMES = frozenset({
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
})


def child_env(*, network: bool, extra=None, keep=()) -> dict:
    """Окружение для дочернего процесса из разрешённых переменных родителя.

    network — нужны ли прокси и сертификаты; extra — переменные, которые
    вызывающий задаёт сам; keep — ещё имена, которые нужно пропустить как есть
    (их передают тесты своему поддельному модулю).
    """
    keep = {str(name) for name in keep}
    out = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if (upper in _NAMES or upper.startswith(_PREFIXES) or name in keep
                or (network and upper in _NETWORK_NAMES)):
            out[name] = value
    out.update({str(k): str(v) for k, v in (extra or {}).items()})
    return out
