#!/usr/bin/env python3
"""Строка входа аккаунта Telegram для чтения каналов (TG_ACCOUNT_SESSION).

Запускать на своём компьютере, не на хостинге: скрипт спросит номер телефона
и код из Telegram, а в конце напечатает строку входа. Её вместе с api_id и
api_hash вставьте в переменные хостинга. В чат с ботом ничего из этого не
отправляйте: строка входа — это полный доступ к аккаунту.

    pip install telethon
    python tools/tg_account_login.py

api_id и api_hash выдаёт https://my.telegram.org → API development tools.
Подробно — docs/tg-account.md.
"""
from __future__ import annotations

import asyncio
import getpass
import sys


def _ask(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        raise SystemExit('Пустой ответ — выход.')
    return value


async def _login() -> str:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise SystemExit('Сначала поставьте библиотеку: pip install telethon') from None
    raw_id = _ask('api_id (число с my.telegram.org): ')
    if not raw_id.isdigit():
        raise SystemExit('api_id — это число, без букв.')
    api_hash = _ask('api_hash (строка с my.telegram.org): ')
    client = TelegramClient(StringSession(), int(raw_id), api_hash)
    # Telethon сам спросит номер и код; пароль — только если включена
    # двухэтапная проверка. getpass не показывает пароль на экране.
    await client.start(phone=lambda: _ask('Номер телефона аккаунта (+7…): '),
                       code_callback=lambda: _ask('Код из Telegram: '),
                       password=lambda: getpass.getpass('Облачный пароль (если есть): '))
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    name = ' '.join(x for x in (getattr(me, 'first_name', ''), getattr(me, 'last_name', '')) if x)
    print(f'\nВход выполнен: {name or getattr(me, "username", "") or "аккаунт"}')
    return session


def main() -> int:
    session = asyncio.run(_login())
    print('\nСкопируйте строку ниже ЦЕЛИКОМ в переменную TG_ACCOUNT_SESSION на хостинге.')
    print('Никому её не показывайте и не отправляйте в чаты.\n')
    print(session)
    print('\nЕщё две переменные: TG_ACCOUNT_API_ID и TG_ACCOUNT_API_HASH — те же, что вы ввели.')
    print('После этого перезапустите бота и посмотрите строку «📡 Каналы» в /status.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
