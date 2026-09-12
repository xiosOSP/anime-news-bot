"""Чужая речь, политика и мёртвый второй уровень — находки аудита.

Жалобы на модерацию сводились к одному: чат обсуждает сюжеты и цитирует
правила, а бот наказывает за процитированное. Рядом — два соседних изъяна:
«трамплин» считался политикой, а сомнительное сообщение при неработающей
модели молча пропадало в журнале.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import anime_news_bot as bot
import moderation_rules as rules


class TestQuotedSpeechIsNotPunished:
    """За чужие слова не наказываем, за свои — наказываем."""

    @pytest.mark.parametrize('text', [
        'В конце он говорит "я тебя убью" и уходит',
        'Он написал мне: "убью тебя", я пожаловался',
        'Там сцена, где он кричит "сдохни!" — мурашки',
        'Цитата из тайтла: «убейся об стену» — жесть конечно',
        'Правило 3: за "заткнись" будет предупреждение',
        'Не пишите "пошел нахуй", это бан',
        'Я не говорил "сдохни", это не я',
    ])
    def test_someone_elses_words_do_not_punish(self, text):
        """Вердикт остаётся, но перестаёт быть автоматическим.

        Убрать его совсем нельзя: кавычки стали бы лазейкой. Поэтому
        сообщение уходит на второй уровень, а наказание не выдаётся.
        """
        verdict = rules.check_text(text)
        assert verdict is not None, 'нарушение не должно исчезать совсем'
        assert verdict.confident is False, f'{text!r} наказано автоматически'

    @pytest.mark.parametrize('text, category', [
        ('Он написал: «привет», а я тебе: «сдохни»', 'aggression'),
        ('я тебе говорю: "сдохни"', 'aggression'),
        ('«Твоя мама шлюха»', 'family'),
        ('Мне написали «привет». Твоя мама шлюха', 'family'),
    ])
    def test_own_words_still_punish(self, text, category):
        """Автор назвал себя — слова его, кавычки ничего не меняют.

        И сообщение целиком в кавычках — это оформление своей реплики, а не
        пересказ: иначе достаточно было бы взять оскорбление в кавычки.
        """
        verdict = rules.check_text(text)
        assert verdict is not None and verdict.category == category
        assert verdict.confident is True, f'{text!r} перестало наказываться'

    def test_own_violation_beside_a_quote_is_judged_on_its_own(self):
        """Рядом с цитатой своё нарушение — судим по своему, а не по чужому.

        Цитата тут тяжелее собственной грубости (угроза против оскорбления),
        и по ней наказывать нельзя. Но и смягчать своё оскорбление она не
        должна: отвечает человек за то, что написал сам.
        """
        verdict = rules.check_text('цитата: "сдохни", а ты мразь')
        assert verdict is not None and verdict.category == 'toxic'
        assert verdict.confident is True

    def test_a_quote_does_not_lend_its_words_to_the_next_one(self):
        """«я» внутри чужой цитаты — чужое «я», присваивать по нему нельзя."""
        verdict = rules.check_text('цитата: "я приду", потом "сдохни"')
        assert verdict is not None and verdict.confident is False


class TestPoliticsDoesNotEatOrdinaryWords:
    @pytest.mark.parametrize('text', [
        'На трамплине он делает сальто',
        'Трамплин в этой серии нарисован шикарно',
    ])
    def test_trampoline_is_not_politics(self, text):
        """«трамп» с любым хвостом — это и «трамплин»: в спортивном аниме он
        встречается чаще, чем президент."""
        assert rules.check_text(text) is None

    @pytest.mark.parametrize('text', ['Трамп снова победил', 'Трампу это не понравится'])
    def test_the_politician_is_still_caught(self, text):
        verdict = rules.check_text(text)
        assert verdict is not None and verdict.category == 'politics'


class TestNobodyToJudgeIsNotSilence:
    """Модель недоступна — человека зовут, а не пишут в журнал и молчат."""

    @staticmethod
    def _message():
        return NS(chat_id=-100, message_id=7, link='https://t.me/c/1/7')

    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        bot._moderation_unjudged_reports.clear()
        monkeypatch.setattr(bot, '_all_admin_ids', lambda: [1])
        monkeypatch.setattr(bot, 'MODERATION_UNJUDGED_NOTICE_SEC', 300)

    @pytest.mark.asyncio
    async def test_admin_is_told_what_went_unchecked(self):
        send = AsyncMock()
        await bot._mod_unjudged_text(NS(send_message=send), self._message(),
                                     'aggression', 'Прямая угроза')
        assert send.await_count == 1
        text = send.await_args.args[1]
        assert 'aggression' in text and 'Прямая угроза' in text
        # Наказание не выдано — это обязано быть сказано, иначе админ решит,
        # что бот уже разобрался.
        assert 'не выдано' in text

    @pytest.mark.asyncio
    async def test_a_noisy_chat_does_not_flood_the_admin(self):
        """Десять одинаковых писем за минуту — и уведомления выключат совсем."""
        send = AsyncMock()
        for _ in range(5):
            await bot._mod_unjudged_text(NS(send_message=send), self._message(),
                                         'aggression', '')
        assert send.await_count == 1

    @pytest.mark.asyncio
    async def test_zero_turns_the_notice_off(self, monkeypatch):
        monkeypatch.setattr(bot, 'MODERATION_UNJUDGED_NOTICE_SEC', 0)
        send = AsyncMock()
        await bot._mod_unjudged_text(NS(send_message=send), self._message(), 'toxic', '')
        assert send.await_count == 0
