"""Правка файла .env из чата: /envfile.

Панель хостинга задаёт окружение следующему процессу, а файл рядом с кодом
заполняет только то, чего в панели НЕТ. Поэтому удаление переменной из панели
не убирает её из бота — оно передаёт ход файлу, и починить это через панель
нельзя в принципе. Доступа к диску у владельца может не быть; у бота он есть.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot


@pytest.fixture
def dotenv(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setattr(bot, 'DOTENV_PATH', path)
    monkeypatch.setattr(bot, 'DOTENV_BACKUP_PATH', path.with_name('.env.bak'))
    monkeypatch.setattr(bot, 'is_owner', lambda user: True)
    return path


async def _run(args):
    reply = AsyncMock()
    await bot.envfile_command(NS(message=NS(reply_text=reply), effective_user=NS(id=1)),
                              NS(args=args))
    return reply.await_args.args[0]


class TestTheReportNeverLeaksAKey:
    @pytest.mark.asyncio
    async def test_a_secret_value_is_never_printed(self, dotenv, monkeypatch):
        """Отчёт уходит в переписку. Ключ не должен попасть туда ни разу.

        Имя показать надо — по нему и чинят; значение — никогда.
        """
        dotenv.write_text('LLM_API_KEY=gsk_supersecret123\n'
                          'BOT_TOKEN=123:AAsecret\n'
                          'LLM_BASE_URL=https://api.orcarouter.ai/v1\n', encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV',
                            {'LLM_API_KEY', 'BOT_TOKEN', 'LLM_BASE_URL'})
        report = await _run([])
        assert 'gsk_supersecret123' not in report
        assert '123:AAsecret' not in report
        assert 'LLM_API_KEY' in report and 'BOT_TOKEN' in report
        # Адрес — не секрет, и именно его нужно увидеть, чтобы понять поломку.
        assert 'api.orcarouter.ai' in report


class TestItSeparatesWhatWorksFromWhatIsShadowed:
    @pytest.mark.asyncio
    async def test_a_line_overridden_by_the_panel_is_marked_as_useless(self, dotenv, monkeypatch):
        """Строка, перебитая панелью, ничего не делает — стирать её незачем.

        Смешать их в один список значит отправить владельца чинить не ту
        строку: та, из-за которой «правка не сработала», ровно одна.
        """
        dotenv.write_text('LLM_BASE_URL=https://api.orcarouter.ai/v1\n'
                          'LLM_MODEL=qwen\n', encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL'})
        report = await _run([])
        active, _, shadowed = report.partition('Перебиты панелью')
        assert 'LLM_BASE_URL' in active
        assert 'LLM_MODEL' in shadowed
        assert 'LLM_MODEL' not in active

    @pytest.mark.asyncio
    async def test_a_missing_file_sends_the_owner_to_the_panel(self, dotenv):
        report = await _run([])
        assert 'панел' in report.lower()


class TestRemovalIsReversible:
    @pytest.mark.asyncio
    async def test_the_line_is_commented_out_and_backed_up(self, dotenv, monkeypatch):
        """Стирать строку вслепую из чата нельзя: вернуть её будет нечем."""
        dotenv.write_text('LLM_BASE_URL=https://api.orcarouter.ai/v1\nLLM_API_KEY=k\n',
                          encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL'})
        report = await _run(['убрать', 'llm_base_url'])
        after = dotenv.read_text(encoding='utf-8')
        assert 'LLM_BASE_URL=https://api.orcarouter.ai/v1' not in [
            line.strip() for line in after.splitlines()]
        assert '# убрано ботом' in after
        # Остальные строки целы: убираем одну названную, а не чистим файл.
        assert 'LLM_API_KEY=k' in after
        assert bot.DOTENV_BACKUP_PATH.read_text(encoding='utf-8').startswith('LLM_BASE_URL=')
        assert 'Перезапустите' in report

    def test_the_backup_is_a_sibling_not_a_double_suffix(self):
        """with_suffix у «.env» дал бы «.env.env.bak»: суффикса у файла нет вовсе.

        Проверяем НАСТОЯЩИЙ путь модуля, а не подменённый фикстурой: иначе
        тест проверял бы аккуратность теста, а не имя, которое получит файл.
        """
        assert bot.DOTENV_BACKUP_PATH.name == '.env.bak'
        assert bot.DOTENV_BACKUP_PATH.parent == bot.DOTENV_PATH.parent

    @pytest.mark.asyncio
    async def test_an_unknown_name_changes_nothing(self, dotenv, monkeypatch):
        dotenv.write_text('LLM_API_KEY=k\n', encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_API_KEY'})
        report = await _run(['убрать', 'NOT_THERE'])
        assert dotenv.read_text(encoding='utf-8') == 'LLM_API_KEY=k\n'
        assert not bot.DOTENV_BACKUP_PATH.exists()
        assert '❌' in report

    @pytest.mark.asyncio
    async def test_removing_twice_changes_nothing_the_second_time(self, dotenv, monkeypatch):
        """Повтор команды не должен комментировать комментарий.

        Иначе каждая новая попытка наращивала бы префиксы, а копия файла
        затиралась бы уже испорченной версией — вернуть исходник стало бы
        нечем.
        """
        dotenv.write_text('LLM_BASE_URL=https://api.orcarouter.ai/v1\n', encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL'})
        await _run(['убрать', 'LLM_BASE_URL'])
        once = dotenv.read_text(encoding='utf-8')
        second = await _run(['убрать', 'LLM_BASE_URL'])
        assert dotenv.read_text(encoding='utf-8') == once
        assert '❌' in second
        assert bot.DOTENV_BACKUP_PATH.read_text(encoding='utf-8') == \
            'LLM_BASE_URL=https://api.orcarouter.ai/v1\n'

    @pytest.mark.asyncio
    async def test_the_listing_skips_comments(self, dotenv, monkeypatch):
        """Закомментированная строка не действует — в списке ей не место."""
        dotenv.write_text('# LLM_MODEL=old\nLLM_BASE_URL=https://api.orcarouter.ai/v1\n',
                          encoding='utf-8')
        monkeypatch.setattr(bot, 'ENV_FROM_DOTENV', {'LLM_BASE_URL'})
        report = await _run([])
        assert 'LLM_MODEL' not in report
        assert 'LLM_BASE_URL' in report


class TestOnlyTheOwnerMayEditTheConfig:
    @pytest.mark.asyncio
    async def test_a_stranger_is_refused(self, dotenv, monkeypatch):
        """Файл содержит ключи. Читать его список может только владелец."""
        monkeypatch.setattr(bot, 'is_owner', lambda user: False)
        denied = AsyncMock()
        monkeypatch.setattr(bot, 'deny_access', denied)
        dotenv.write_text('LLM_API_KEY=k\n', encoding='utf-8')
        await bot.envfile_command(NS(message=NS(reply_text=AsyncMock()),
                                     effective_user=NS(id=999)), NS(args=[]))
        assert denied.await_count == 1
