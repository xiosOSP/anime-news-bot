"""Оскорбление группы без адресата — не тишина, даже когда модели нет.

Локальный слой видел в «все негры тупые» лишь безымянное подозрение. Такое
подозрение при недоступной модели только пишется в журнал: человеку бот
сообщает о НАЗВАННЫХ подозрениях, иначе письмо приходило бы на каждое
упоминание страны или религии. В итоге оскорбление группы не видел никто.
"""
import pytest

import anime_news_bot as bot


def _check(text):
    bot._moderation_recent.clear()
    bot._moderation_windows.clear()
    return bot._mod_local_check(-100, 7, text)


class TestGroupPlusInsultIsNamed:
    @pytest.mark.parametrize('text', [
        'все негры тупые', 'цыгане грязные воры', 'хохлы тупые', 'москали вонючие',
    ])
    def test_it_is_named_hate_but_not_punished(self, text):
        """Имя есть — человек узнает. Уверенности нет — бот не наказывает.

        «почему "негры тупые" — это расизм» тоже содержит оба слова, так что
        решать должна модель или человек, а не правило.
        """
        verdict = _check(text)
        assert verdict is not None and verdict['category'] == 'hate', verdict
        assert verdict['confident'] is False

    def test_an_ambiguous_slur_is_disambiguated_by_the_insult(self):
        """«чурка» вырезается как полено, если к ней не обращаются.

        Рядом с оскорбительным словом неоднозначности уже нет.
        """
        verdict = _check('чурка ебаная')
        assert verdict is not None and verdict['category'] == 'hate'
        assert verdict['confident'] is False


class TestOrdinarySpeechStaysQuiet:
    @pytest.mark.parametrize('text', [
        # Упоминание группы без оскорбления — обсуждать культуру можно.
        'Негритянский джаз в саундтреке Cowboy Bebop огонь',
        'Цыганский табор в «Хеллсинге»',
        # Оскорбление без группы — личное или о сюжете, не групповое.
        'Тупой сюжет, но рисовка огонь',
        'Какой же ты тупой, ну',
        # Слова, начинающиеся как неоднозначные оскорбления.
        'Жидкий сюжет, тупые диалоги',
    ])
    def test_no_hate_name_without_both_parts(self, text):
        """Название 'hate' — это письмо админу. Письмо на каждую критику
        сюжета админ выключит в первый же день."""
        verdict = _check(text)
        assert verdict is None or verdict.get('category') != 'hate', verdict

    @pytest.mark.parametrize('text', [
        'Цыганский табор в «Хеллсинге», а финал в тупике',
        'Армянский коньяк и грязный снег в кадре',
    ])
    def test_a_word_sharing_the_stem_is_not_an_insult(self, text):
        """Группа рядом со словом, которое только НАЧИНАЕТСЯ как оскорбление.

        «тупик» — не «тупые», «грязный снег» — не «грязные» про людей.
        Обобщение о группе говорится во множественном числе, поэтому окончания
        перечислены закрыто.
        """
        verdict = _check(text)
        assert verdict is None or verdict.get('category') != 'hate', verdict

    @pytest.mark.parametrize('text', [
        'Главный герой — чурка деревянная, буквально Пиноккио', 'Грязные трюки в финале',
    ])
    def test_the_wooden_block_and_dirty_tricks_stay_clean(self, text):
        assert _check(text) is None


class TestItReachesAHuman:
    """Смысл имени — ровно в этом: при мёртвой модели зовётся человек.

    Проверяем настоящим обработчиком, а не повтором его развилки в тесте:
    иначе тест проверял бы сам себя.
    """

    @pytest.fixture
    def chat(self, tmp_path, monkeypatch):
        from test_moderation_offline import state
        store = state.__wrapped__(tmp_path, monkeypatch)
        monkeypatch.setattr(bot, 'MODERATION_LLM_ENABLED', False)
        monkeypatch.setattr(bot, 'MODERATION_UNJUDGED_NOTICE_SEC', 300)
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: [1])
        bot._moderation_unjudged_reports.clear()
        return store

    @pytest.mark.asyncio
    async def test_group_insult_calls_the_admin(self, chat):
        from test_moderation_offline import handle, message, telegram_bot
        tg = telegram_bot()
        await handle(message('все негры тупые', number=31), tg)
        # Автоматически не наказано: ни удаления, ни мута.
        tg.delete_message.assert_not_awaited()
        tg.restrict_chat_member.assert_not_awaited()
        # Но админ узнал — и узнал, о чём именно.
        assert tg.send_message.await_count == 1
        assert 'hate' in tg.send_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_a_mere_mention_does_not_write_to_the_admin(self, chat):
        """Упоминание группы без оскорбления — не повод для письма."""
        from test_moderation_offline import handle, message, telegram_bot
        tg = telegram_bot()
        await handle(message('Цыганский табор в «Хеллсинге»', number=32), tg)
        tg.send_message.assert_not_awaited()
