"""Раздел «Модерация» в настройках.

Переключатель здесь не такой, как остальные: одно нажатие начинает
ограничивать живых людей. Тесты сторожат в первую очередь то, что делает эту
кнопку безопасной, — асимметрию подтверждения и невозможность выдать боту
право банить через меню.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


@pytest.fixture(autouse=True)
def _mod(monkeypatch, tmp_path):
    store = bot.ChatModerationStore(tmp_path / 'mod.json')
    store.set_chat(-100, True)
    monkeypatch.setattr(bot, 'chat_moderation', store)
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    monkeypatch.setattr(bot, 'is_admin', lambda _u: True)
    monkeypatch.setattr(bot, '_audit_update', lambda *a, **k: None)
    return store


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _query(data):
    query = MagicMock(data=data)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=query,
                           effective_user=SimpleNamespace(id=1, full_name='admin'))


async def _press(data):
    update = _query(data)
    await bot.moderation_settings_callback(update, SimpleNamespace(bot=None))
    return update.callback_query


# ---------- раздел на месте ----------

def test_section_is_in_the_root_menu():
    """Раздел должен открываться из настроек, а не только командой."""
    targets = [b.callback_data for row in bot.build_settings_menu().inline_keyboard
               for b in row]
    assert 'settings:sec:moderation' in targets


def test_section_says_what_the_bot_does_now(_mod):
    """Строка состояния отвечает «что сейчас», а не «как называется режим»."""
    assert 'только смотрит' in bot._section_view('moderation')[0]
    _mod.set_mode('active')
    assert 'наказывает' in bot._section_view('moderation')[0]


def test_button_names_the_present_not_the_future(_mod):
    """Кнопка-состояние с надписью о будущем читается наоборот.

    «Включить наказания» на кнопке, когда они уже включены, однажды окажется
    нажатой ради того, что и так работает.
    """
    assert any('Сейчас только смотрит' in x for x in _labels(bot._menu_moderation()))
    _mod.set_mode('active')
    assert any('Сейчас наказывает' in x for x in _labels(bot._menu_moderation()))


# ---------- асимметрия подтверждения ----------

@pytest.mark.asyncio
async def test_turning_punishment_on_asks_first(_mod):
    """Включение наказаний — не тот случай, где промах ничего не стоит."""
    assert _mod.mode == 'observe'
    await _press('mods:mode')
    assert _mod.mode == 'observe', 'режим сменился без подтверждения'


@pytest.mark.asyncio
async def test_confirmation_actually_switches(_mod):
    await _press('mods:mode')
    await _press('mods:mode_yes')
    assert _mod.mode == 'active'


@pytest.mark.asyncio
async def test_turning_punishment_off_is_instant(_mod):
    """Обратный переход подтверждения не требует: остановить наказания — всегда
    безопасно, и лишний экран здесь стоил бы времени в худший момент."""
    _mod.set_mode('active')
    await _press('mods:mode')
    assert _mod.mode == 'observe'


@pytest.mark.asyncio
async def test_failed_save_does_not_report_success(_mod, monkeypatch):
    """Настройка, не дожившая до диска, не должна выглядеть применённой."""
    monkeypatch.setattr(_mod, 'set_mode', lambda _m: False)
    query = await _press('mods:mode_yes')
    assert query.answer.await_args.kwargs.get('show_alert') is True


# ---------- меню не выдаёт прав, которых у бота нет ----------

def test_menu_never_offers_a_ban(_mod):
    """Бан остаётся только за человеком — в меню такой кнопки быть не может."""
    for markup in (bot._menu_moderation(), bot._menu_moderation_confirm()):
        for label in _labels(markup):
            assert 'бан' not in label.lower()
    # Там, где бот действительно что-то делает, ограничение должно быть
    # названо прямо: иначе «режим карателя» читается как «может всё».
    _mod.set_mode('active')
    assert 'банить' in bot._moderation_mode_text().lower()
    _mod.set_mode('observe')
    assert 'банить' in bot._moderation_arm_text().lower()


@pytest.mark.asyncio
async def test_non_admin_changes_nothing(_mod, monkeypatch):
    monkeypatch.setattr(bot, 'is_admin', lambda _u: False)
    await _press('mods:mode_yes')
    assert _mod.mode == 'observe'


# ---------- один отчёт на команду и на меню ----------

def test_stats_text_is_shared_with_the_command(_mod):
    """Два разных отчёта об одном и том же рано или поздно разойдутся."""
    text = bot._moderation_stats_text()
    assert 'Решений пока не было' in text
    _mod.record_decision('warn', 'toxic')
    assert 'Решений всего' in bot._moderation_stats_text()


def test_moderation_ui_never_names_a_command_that_does_not_exist():
    """Подсказка с несуществующей командой хуже отсутствия подсказки.

    Живой случай: меню звало /modon, а зарегистрирован /modhere. Человек по
    такой подсказке решает, что сломан бот, и ищет поломку не там.

    Проверяем именно тексты модерации, а не весь файл: по всему модулю
    «/слово» — это ещё и закрывающие HTML-теги, и маршруты health-сервера,
    и куски адресов, а они командами не являются.
    """
    import ast
    import re
    from pathlib import Path

    source = Path(bot.__file__).read_text(encoding='utf-8')
    registered = set(re.findall(r'CommandHandler\(\s*["\'](\w+)["\']', source))
    assert registered, 'не нашли ни одной зарегистрированной команды'

    watched = {'_menu_moderation', '_menu_moderation_confirm', '_section_state',
               'moderation_settings_callback', '_moderation_mode_text',
               '_moderation_arm_text', '_moderation_stats_text',
               '_moderation_log_text'}
    pattern = re.compile(r'(?<![\w./<])/([a-z][a-z0-9_]{2,20})(?![\w/])')
    mentioned: set = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in watched:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                mentioned.update(pattern.findall(inner.value))

    assert mentioned, 'тексты модерации перестали ссылаться на команды — проверка ослепла'
    unknown = sorted(mentioned - registered)
    assert not unknown, f'в текстах модерации названы несуществующие команды: {unknown}'
