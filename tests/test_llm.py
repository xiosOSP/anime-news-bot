"""Тесты языковой модели: перевод/текст, отсев не по теме, теги,
защита от выдумок и полный откат к DeepL/Google при любых сбоях."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot


def _reply(payload, status=200):
    return MagicMock(status_code=status, text='',
                     json=lambda: {'choices': [{'message': {
                         'content': json.dumps(payload, ensure_ascii=False)}}]})


@pytest.fixture
def llm(tmp_path, monkeypatch):
    """Настроенная модель + чистые счётчики."""
    monkeypatch.setattr(anime_news_bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(anime_news_bot, 'LLM_BASE_URL', 'https://x/v1')
    monkeypatch.setattr(anime_news_bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(anime_news_bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(anime_news_bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(anime_news_bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(anime_news_bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(anime_news_bot, 'settings',
                        anime_news_bot.BotSettings(tmp_path / 's.json'))
    anime_news_bot._pending_admin_alerts.clear()
    return anime_news_bot


OK_ANSWER = {
    'relevant': True, 'topic': 'игры',
    'title': 'Target меняет правила продажи карт Pokémon',
    'summary': 'Ритейлер ограничит количество карт в одни руки.',
    'tags': ['#покемоны', '#новость'],
}
NEWS = {'title': "Target's Pokémon card changes are good news",
        'summary': 'The retailer will limit purchases.', 'source': 'Polygon', 'lang': 'en'}


class TestConfiguration:
    def test_not_configured_by_default(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'LLM_API_KEY', '')
        assert anime_news_bot._llm_configured() is False
        assert anime_news_bot._llm_active() is False

    def test_presets_have_url_and_model(self):
        for name, (url, model) in anime_news_bot.LLM_PRESETS.items():
            assert url.startswith('https://') and model, name

    def test_configured_with_env(self, llm):
        assert llm._llm_configured() is True
        assert llm._llm_active() is True

    def test_master_switch_off(self, llm):
        llm.settings.llm_enabled = False
        assert llm._llm_active() is False


class TestEnrichment:
    def test_keeps_proper_nouns(self, llm):
        news = dict(NEWS)
        with patch.object(llm.requests, 'post', return_value=_reply(OK_ANSWER)):
            assert asyncio.run(llm._llm_enrich(news)) == 'ok'
        assert 'Target' in news['_llm_text']
        assert 'Pokémon' in news['_llm_text']
        assert news['_llm_tags'] == '#покемоны #новость'

    def test_filters_off_topic(self, llm):
        news = {'title': 'Нефть подорожала', 'summary': '', 'source': 'X'}
        with patch.object(llm.requests, 'post',
                          return_value=_reply({'relevant': False, 'topic': 'прочее'})):
            assert asyncio.run(llm._llm_enrich(news)) == 'skip'

    def test_filter_can_be_disabled(self, llm):
        llm.settings.llm_filter = False
        news = {'title': 'Нефть', 'summary': '', 'source': 'X'}
        with patch.object(llm.requests, 'post',
                          return_value=_reply({'relevant': False, 'title': 'Нефть'})):
            assert asyncio.run(llm._llm_enrich(news)) == 'ok'

    def test_rewrite_can_be_disabled(self, llm):
        llm.settings.llm_rewrite = False
        news = dict(NEWS)
        with patch.object(llm.requests, 'post', return_value=_reply(OK_ANSWER)):
            asyncio.run(llm._llm_enrich(news))
        assert '_llm_text' not in news
        assert news.get('_llm_tags')          # теги независимы

    def test_tags_can_be_disabled(self, llm):
        llm.settings.llm_tags = False
        news = dict(NEWS)
        with patch.object(llm.requests, 'post', return_value=_reply(OK_ANSWER)):
            asyncio.run(llm._llm_enrich(news))
        assert '_llm_tags' not in news

    def test_long_answer_trimmed(self, llm):
        """Простыня обрезается до лимита, а совсем гигантская — отбрасывается."""
        news = {'title': 'Короткий', 'summary': 'Два слова.', 'source': 'X'}
        fake = {'topic': 'аниме', 'title': 'Норм', 'summary': 'Выдумка. ' * 90}
        with patch.object(llm.requests, 'post', return_value=_reply(fake)):
            asyncio.run(llm._llm_enrich(news))
        assert len(news['_llm_text']) <= anime_news_bot.LLM_SUMMARY_MAX + 250

    def test_absurdly_long_rejected(self, llm):
        news = {'title': 'Короткий', 'summary': 'Два слова.', 'source': 'X'}
        fake = {'topic': 'аниме', 'title': 'Норм', 'summary': 'Мусор. ' * 400}
        with patch.object(llm.requests, 'post', return_value=_reply(fake)):
            asyncio.run(llm._llm_enrich(news))
        assert '_llm_text' not in news

    def test_overlong_title_rejected(self, llm):
        news = {'title': 'T', 'summary': '', 'source': 'X'}
        fake = {'topic': 'аниме', 'title': 'Очень длинный заголовок. ' * 20, 'summary': ''}
        with patch.object(llm.requests, 'post', return_value=_reply(fake)):
            asyncio.run(llm._llm_enrich(news))
        assert '_llm_text' not in news

    def test_bad_tags_sanitised(self, llm):
        news = dict(NEWS)
        answer = dict(OK_ANSWER, tags=['#ок', 'бeз_решётки', '#a', '#' + 'я' * 40, 42])
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        tags = news.get('_llm_tags', '').split()
        assert all(t.startswith('#') and 3 <= len(t) <= 25 for t in tags)

    def test_empty_title_skips_call(self, llm):
        with patch.object(llm.requests, 'post', side_effect=AssertionError('не вызывать')):
            assert asyncio.run(llm._llm_enrich({'title': '', 'summary': 'x'})) == 'off'


class TestFailureFallback:
    """При любой проблеме бот обязан работать как раньше."""

    def test_network_error(self, llm):
        news = dict(NEWS)
        with patch.object(llm.requests, 'post', side_effect=OSError('down')):
            assert asyncio.run(llm._llm_enrich(news)) == 'off'
        assert '_llm_text' not in news

    def test_http_500(self, llm):
        with patch.object(llm.requests, 'post',
                          return_value=MagicMock(status_code=500, text='oops')):
            assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'off'

    def test_rate_limit_429(self, llm):
        with patch.object(llm.requests, 'post',
                          return_value=MagicMock(status_code=429, text='slow down')):
            assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'off'

    def test_bad_key_disables_and_alerts(self, llm):
        with patch.object(llm.requests, 'post',
                          return_value=MagicMock(status_code=401, text='bad')):
            asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert llm._llm_disabled_runtime is True
        assert any('не принял ключ' in a for a in llm._pending_admin_alerts)

    def test_garbage_response(self, llm):
        bad = MagicMock(status_code=200, text='',
                        json=lambda: {'choices': [{'message': {'content': 'привет!'}}]})
        with patch.object(llm.requests, 'post', return_value=bad):
            assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'off'

    def test_fail_streak_pauses(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_llm_fail_streak',
                            anime_news_bot.LLM_FAIL_PAUSE_AFTER)
        with patch.object(llm.requests, 'post', side_effect=AssertionError('не вызывать')):
            assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'off'
        assert llm._llm_disabled_runtime is True


class TestQuota:
    def test_daily_limit_blocks_calls(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'LLM_DAILY_LIMIT', 2)
        llm.settings.llm_day = llm._local_now().strftime('%Y-%m-%d')
        llm.settings.llm_calls_today = 2
        with patch.object(llm.requests, 'post', side_effect=AssertionError('не вызывать')):
            assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'off'

    def test_counter_increments(self, llm):
        with patch.object(llm.requests, 'post', return_value=_reply(OK_ANSWER)):
            asyncio.run(llm._llm_enrich(dict(NEWS)))
            asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert llm.settings.llm_calls_today == 2

    def test_counter_resets_next_day(self, llm):
        llm.settings.llm_day = '2020-01-01'
        llm.settings.llm_calls_today = 999
        with patch.object(llm.requests, 'post', return_value=_reply(OK_ANSWER)):
            asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert llm.settings.llm_calls_today == 1


class TestJsonParsing:
    def test_plain(self):
        assert anime_news_bot._llm_parse_json('{"a": 1}') == {'a': 1}

    def test_markdown_fence(self):
        assert anime_news_bot._llm_parse_json('```json\n{"a": 1}\n```') == {'a': 1}

    def test_surrounded_by_text(self):
        assert anime_news_bot._llm_parse_json('вот: {"a": 1} готово')['a'] == 1

    @pytest.mark.parametrize('raw', ['', 'просто текст', '[1,2,3]', None])
    def test_garbage(self, raw):
        assert anime_news_bot._llm_parse_json(raw or '') is None


class TestFormattingPriority:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        class FakeTr:
            def translate(self, t, input_limit=None):
                return t
        monkeypatch.setattr(anime_news_bot, 'translator', FakeTr())
        monkeypatch.setattr(anime_news_bot, 'anilist', MagicMock(lookup=lambda q: None))
        monkeypatch.setattr(anime_news_bot, '_translation_cache', {})
        monkeypatch.setattr(anime_news_bot, 'DEEPL_API_KEY', '')
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(translator_engine='google', llm_tags=True))

    def _news(self, **extra):
        return dict({'title': 'Anime Announced', 'summary': 'It premieres in fall.',
                     'published_parsed': None, 'lang': 'en'}, **extra)

    def test_without_llm_unchanged(self):
        assert 'Anime Announced' in anime_news_bot.format_news_short(self._news())

    def test_llm_text_used(self):
        out = anime_news_bot.format_news_short(self._news(_llm_text='Аниме анонсировано.'))
        assert out == 'Аниме анонсировано.'

    def test_tags_appended(self):
        out = anime_news_bot.format_news_short(
            self._news(_llm_text='Аниме.', _llm_tags='#аниме #анонс'))
        assert out.endswith('#аниме #анонс')

    def test_tags_on_plain_path(self):
        out = anime_news_bot.format_news_short(self._news(_llm_tags='#аниме'))
        assert out.endswith('#аниме')

    def test_manual_edit_wins(self):
        out = anime_news_bot.format_news_short(
            self._news(_llm_text='Модель.', _edited_text='Руками.', _llm_tags='#а'))
        assert out == 'Руками.'

    def test_tags_disabled(self, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(translator_engine='google', llm_tags=False))
        out = anime_news_bot.format_news_short(
            self._news(_llm_text='Аниме.', _llm_tags='#аниме'))
        assert '#' not in out

    def test_no_duplicate_tags(self):
        out = anime_news_bot.format_news_short(
            self._news(_llm_text='Аниме. #аниме', _llm_tags='#аниме'))
        assert out.count('#аниме') == 1


class TestExtraParams:
    """Доп. параметры запроса — для моделей с режимом рассуждений."""

    def test_empty_by_default(self, monkeypatch):
        monkeypatch.delenv('LLM_EXTRA_PARAMS', raising=False)
        assert anime_news_bot._llm_extra_params() == {}

    def test_parses_json(self, monkeypatch):
        monkeypatch.setenv('LLM_EXTRA_PARAMS', '{"reasoning_effort": "none"}')
        assert anime_news_bot._llm_extra_params() == {'reasoning_effort': 'none'}

    def test_garbage_ignored(self, monkeypatch):
        monkeypatch.setenv('LLM_EXTRA_PARAMS', 'не json')
        assert anime_news_bot._llm_extra_params() == {}

    def test_non_dict_ignored(self, monkeypatch):
        monkeypatch.setenv('LLM_EXTRA_PARAMS', '[1, 2]')
        assert anime_news_bot._llm_extra_params() == {}

    def test_merged_into_request(self, llm, monkeypatch):
        monkeypatch.setenv('LLM_EXTRA_PARAMS', '{"reasoning_effort": "none"}')
        captured = {}

        def fake_post(url, **kw):
            captured.update(kw.get('json') or {})
            return _reply(OK_ANSWER)
        monkeypatch.setattr(llm.requests, 'post', fake_post)
        asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert captured['reasoning_effort'] == 'none'
        assert captured['model'] == 'm'          # свои поля не потерялись


class TestTopicOverRelevantFlag:
    """Модель противоречила себе: topic='игры' (разрешено) + relevant=false.
    Решение принимает код по теме, а не по флагу."""

    def test_allowed_topic_wins_over_false_flag(self, llm):
        news = {'title': "Steam's best Animal Crossing clone", 'summary': '', 'source': 'X'}
        answer = {'relevant': False, 'topic': 'игры', 'title': 'Клон Animal Crossing',
                  'summary': '', 'tags': ['#игры']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            assert asyncio.run(llm._llm_enrich(news)) == 'ok'
        assert news['_llm_topic'] == 'игры'

    @pytest.mark.parametrize('topic', list(anime_news_bot.LLM_TOPICS_OK))
    def test_every_allowed_topic_passes(self, llm, topic):
        news = {'title': 'T', 'summary': '', 'source': 'X'}
        answer = {'relevant': False, 'topic': topic, 'title': 'Заголовок', 'summary': ''}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            assert asyncio.run(llm._llm_enrich(news)) == 'ok'

    def test_other_topic_filtered(self, llm):
        news = {'title': 'State Farm реклама', 'summary': '', 'source': 'X'}
        answer = {'relevant': True, 'topic': 'прочее', 'title': 'Реклама', 'summary': ''}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            assert asyncio.run(llm._llm_enrich(news)) == 'skip'

    def test_unknown_topic_with_false_flag_filtered(self, llm):
        news = {'title': 'T', 'summary': '', 'source': 'X'}
        answer = {'relevant': False, 'topic': 'непонятно', 'title': 'X', 'summary': ''}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            assert asyncio.run(llm._llm_enrich(news)) == 'skip'

    def test_unknown_topic_without_flag_passes(self, llm):
        news = {'title': 'T', 'summary': '', 'source': 'X'}
        answer = {'topic': 'непонятно', 'title': 'Заголовок', 'summary': ''}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            assert asyncio.run(llm._llm_enrich(news)) == 'ok'


class TestSummaryRedundancy:
    """Текст должен дополнять заголовок, а не пересказывать его."""

    @pytest.mark.parametrize('title,summary,dup', [
        ('Последний опенинг Bleach от jo0ji',
         'Последний опенинг аниме Bleach с треком от jo0ji выйдет сегодня', True),
        ('Target меняет правила продажи карт',
         'Target изменил правила продажи карточек', True),
        ('Анонсирован третий сезон Атаки титанов',
         'Третий сезон Атаки титанов анонсировали', True),
        ('Опубликован опенинг к 3 сезону Агента времени',
         'Первая часть 3 сезона выйдет 14 августа 2026, вторая — в 2027', False),
        ('Chainsaw Man получит второй сезон',
         'Студия MAPPA подтвердила производство, показ начнётся весной 2027', False),
        ('Netflix показал трейлер Ведьмака',
         'В ролике впервые появился актёр в роли Геральта, премьера в декабре', False),
    ])
    def test_detector(self, title, summary, dup):
        assert anime_news_bot._too_similar(title, summary) is dup

    def test_empty_strings(self):
        assert anime_news_bot._too_similar('', 'что-то') is False
        assert anime_news_bot._too_similar('что-то', '') is False

    def test_redundant_summary_dropped(self, llm):
        news = {'title': 'Bleach opening', 'summary': 'Out today.', 'source': 'X'}
        answer = {'topic': 'аниме',
                  'title': 'Последний опенинг Bleach: Thousand-Year Blood War от jo0ji',
                  'summary': 'Последний опенинг аниме Bleach: Thousand-Year Blood War '
                             'с треком от jo0ji выйдет сегодня',
                  'tags': ['#аниме']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        assert '\n\n' not in news['_llm_text']      # остался только заголовок

    def test_useful_summary_kept(self, llm):
        news = {'title': 'Anime announced', 'summary': 'Netflix premiere is planned for January.', 'source': 'X'}
        answer = {'topic': 'аниме', 'title': 'Анонсировано новое аниме от MAPPA',
                  'summary': 'Премьера состоится в январе на Netflix.', 'tags': []}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        assert 'Netflix' in news['_llm_text']


class TestTagCleanup:
    @pytest.mark.parametrize('raw,expect', [
        (['#3сезон'], '#сезон'),                 # Telegram не любит цифру после #
        (['#Аниме'], '#аниме'),                  # приводим к строчным
        (['#опенинг', '#опенинг', '#аниме'], '#опенинг #аниме'),   # без дублей
        (['#a'], None),                          # слишком короткий
        (['#' + 'я' * 40], None),                # слишком длинный
        (['аниме'], '#аниме'),                   # решётку добавим сами
        ([123, '#игры'], '#игры'),               # мусор отбрасываем
    ])
    def test_tags(self, llm, raw, expect):
        news = {'title': 'Тест', 'summary': '', 'source': 'X'}
        answer = {'topic': 'аниме', 'title': 'Тест', 'summary': '', 'tags': raw}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        assert news.get('_llm_tags') == expect

    def test_max_three(self, llm):
        news = {'title': 'Тест', 'summary': '', 'source': 'X'}
        answer = {'topic': 'аниме', 'title': 'Тест', 'summary': '',
                  'tags': ['#один', '#два', '#три', '#четыре', '#пять']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        assert len(news['_llm_tags'].split()) == 3


class TestJsonMode:
    def test_requested_by_default(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_llm_json_mode', True)
        sent = []

        def post(url, **kw):
            sent.append(kw['json'])
            return _reply(OK_ANSWER)
        monkeypatch.setattr(llm.requests, 'post', post)
        asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert sent[0]['response_format'] == {'type': 'json_object'}

    def test_retries_without_it_on_400(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_llm_json_mode', True)
        sent = []

        def post(url, **kw):
            sent.append(kw['json'])
            if 'response_format' in kw['json']:
                return MagicMock(status_code=400, text='unknown field')
            return _reply(OK_ANSWER)
        monkeypatch.setattr(llm.requests, 'post', post)
        assert asyncio.run(llm._llm_enrich(dict(NEWS))) == 'ok'
        assert len(sent) == 2
        assert 'response_format' not in sent[1]
        assert anime_news_bot._llm_json_mode is False

    def test_not_requested_after_rejection(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, '_llm_json_mode', False)
        sent = []

        def post(url, **kw):
            sent.append(kw['json'])
            return _reply(OK_ANSWER)
        monkeypatch.setattr(llm.requests, 'post', post)
        asyncio.run(llm._llm_enrich(dict(NEWS)))
        assert 'response_format' not in sent[0]


class TestPromptQuality:
    def test_prompt_covers_key_rules(self):
        p = anime_news_bot.LLM_SYSTEM_PROMPT
        for needle in ('НЕ переводи', 'ёлочк', 'НЕ ПОВТОРЯЙСЯ',
                       'сегодня', 'только буква', 'прочее',
                       'читаться сам по себе', 'абзац',
                       'ПЛОХОГО ответа'):
            assert needle in p, needle

    def test_prompt_has_example(self):
        assert 'Пример' in anime_news_bot.LLM_SYSTEM_PROMPT
        assert '{"topic"' in anime_news_bot.LLM_SYSTEM_PROMPT

    def test_topics_consistent(self):
        for t in anime_news_bot.LLM_TOPICS_OK:
            assert t in anime_news_bot.LLM_SYSTEM_PROMPT
        assert 'прочее' in anime_news_bot.LLM_TOPIC_ANY


class TestParagraphStructure:
    """Пост должен читаться сам по себе: суть → детали → контекст,
    не больше трёх абзацев."""

    def test_paragraphs_preserved(self, llm):
        news = {'title': 'Bleach opening', 'summary': 'Out Oct 4.', 'source': 'ANN'}
        answer = {'topic': 'аниме',
                  'title': 'Опенинг финальной части Bleach записал jo0ji',
                  'summary': 'Заключительный кур выходит 4 октября на Disney+.\n\n'
                             'Это экранизация последней арки манги Тайто Кубо.',
                  'tags': ['#аниме']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        blocks = news['_llm_text'].split('\n\n')
        assert 'jo0ji' in blocks[0]                      # заголовок
        assert 'Disney+' in blocks[1]                    # детали
        assert 'Тайто Кубо' in blocks[2]                 # контекст

    def test_single_paragraph_when_few_facts(self, llm):
        news = {'title': 'New chapter', 'summary': '', 'source': 'X'}
        answer = {'topic': 'манга', 'title': 'Вышла новая глава «Ванпанчмена»',
                  'summary': '', 'tags': ['#манга']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        assert '\n\n' not in news['_llm_text']

    def test_fits_caption_limit(self, llm, monkeypatch):
        monkeypatch.setattr(anime_news_bot, 'settings',
                            MagicMock(llm_enabled=True, llm_rewrite=True,
                                      llm_filter=True, llm_tags=True,
                                      llm_day='', llm_calls_today=0, tz_offset=3,
                                      translator_engine='google'))
        news = {'title': 'T', 'summary': 'S', 'source': 'X'}
        answer = {'topic': 'аниме', 'title': 'З' * anime_news_bot.LLM_TITLE_MAX,
                  'summary': 'Текст. ' * 200, 'tags': ['#аниме', '#новость', '#анонс']}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)):
            asyncio.run(llm._llm_enrich(news))
        full = anime_news_bot.format_news_short(
            dict(news, published_parsed=None, lang='ru'))
        assert len(full) < anime_news_bot.TG_CAPTION_LIMIT


class TestTrimParagraphs:
    def test_keeps_three(self):
        text = '\n\n'.join(f'Абзац {i} с содержанием.' for i in range(1, 6))
        assert len(anime_news_bot._trim_paragraphs(text).split('\n\n')) == 3

    def test_keeps_fewer_if_fewer(self):
        assert anime_news_bot._trim_paragraphs('Один абзац.') == 'Один абзац.'

    def test_respects_length_limit(self):
        text = '\n\n'.join(['С' * 300] * 4)
        assert len(anime_news_bot._trim_paragraphs(text)) <= anime_news_bot.LLM_SUMMARY_MAX

    def test_drops_tiny_leftover(self):
        # Если на последний абзац остаётся меньше 150 символов — не начинаем его
        text = 'А' * (anime_news_bot.LLM_SUMMARY_MAX - 60) + '\n\n' + 'Б' * 300
        out = anime_news_bot._trim_paragraphs(text)
        assert out.count('\n\n') == 0

    def test_empty(self):
        assert anime_news_bot._trim_paragraphs('') == ''
        assert anime_news_bot._trim_paragraphs(None) == ''

    def test_normalises_extra_blank_lines(self):
        assert anime_news_bot._trim_paragraphs('А.\n\n\n\nБ.') == 'А.\n\nБ.'


class TestSanityLimits:
    def test_context_rich_post_allowed(self):
        """Старая защита резала посты с вводными — новая пропускает."""
        rich = ('Заключительная часть выходит 4 октября на Disney+.\n\n'
                'Опенинг записал jo0ji, анимацией занимается Pierrot.\n\n'
                'Это экранизация финальной арки манги Тайто Кубо.')
        assert anime_news_bot._sanity_ok(rich, anime_news_bot.LLM_SUMMARY_MAX * 2)

    def test_wall_of_text_blocked(self):
        assert not anime_news_bot._sanity_ok(
            'Мусор. ' * 400, anime_news_bot.LLM_SUMMARY_MAX * 2)

    def test_limits_fit_caption(self):
        # заголовок + текст + дата + теги должны влезать в подпись Telegram
        worst = (anime_news_bot.LLM_TITLE_MAX + anime_news_bot.LLM_SUMMARY_MAX
                 + 40 + 60)
        assert worst < anime_news_bot.TG_CAPTION_LIMIT


class TestTautology:
    """Модель склонна пересказывать заголовок в первом абзаце и повторять
    названия. Промпт это запрещает, а код подстраховывает."""

    @pytest.mark.parametrize('title,para', [
        ('Аниме по манге «FX Воин Куруми» выйдет в октябре',
         'Премьера аниме по манге «FX Senshi Kurumi-chan» состоится в октябре этого года.'),
        ('Последний опенинг Bleach от jo0ji',
         'Последний опенинг аниме Bleach с треком от jo0ji выйдет сегодня.'),
        ('Chainsaw Man получит второй сезон',
         'Второй сезон Chainsaw Man был анонсирован.'),
        ('Вышел трейлер «Атаки титанов»',
         'Трейлер «Атаки титанов» опубликован.'),
    ])
    def test_repetition_dropped(self, title, para):
        assert anime_news_bot._drop_repetitive_paragraphs(title, [para]) == []

    @pytest.mark.parametrize('title,para', [
        ('Аниме по манге «FX Воин Куруми» выйдет в октябре',
         'Премьеру покажет Netflix, всего запланировано 12 эпизодов.'),
        ('Вышел трейлер «Героя ленты»', 'Премьера 8 августа.'),
        ('Опенинг финальной части Bleach записал jo0ji',
         'Заключительный кур выходит 4 октября на Disney+, анимацией занимается Pierrot.'),
        ('Chainsaw Man получит второй сезон',
         'Студия MAPPA подтвердила производство, показ начнётся весной 2027 года.'),
        ('Ranma ½ возвращается с третьим сезоном',
         'Это экранизация манги Румико Такахаси, выходившей с 1987 года.'),
    ])
    def test_useful_paragraph_kept(self, title, para):
        assert anime_news_bot._drop_repetitive_paragraphs(title, [para]) == [para]

    def test_transliteration_counts(self):
        """«Куруми» и «Kurumi» — одно название, повтор должен видеться."""
        stems = anime_news_bot._content_stems('Куруми')
        assert stems & anime_news_bot._content_stems('Kurumi')

    def test_second_paragraph_checked_against_first(self):
        title = 'Вышел трейлер нового сериала'
        paras = ['Показ начнётся 4 октября на Disney+, снимает студия Pierrot.',
                 'Сериал будет выходить на Disney+ с 4 октября, работает Pierrot.']
        kept = anime_news_bot._drop_repetitive_paragraphs(title, paras)
        assert len(kept) == 1                 # второй повторяет первый

    def test_empty_and_blank(self):
        assert anime_news_bot._drop_repetitive_paragraphs('Заголовок', []) == []
        assert anime_news_bot._drop_repetitive_paragraphs('Заголовок', ['   ']) == []

    def test_prompt_forbids_repetition(self):
        prompt = anime_news_bot.LLM_SYSTEM_PROMPT
        assert 'НЕ ПОВТОРЯЙСЯ' in prompt
        assert 'ПЛОХОГО ответа' in prompt      # антипример на месте
        assert 'один раз' in prompt

    def test_pipeline_strips_repetition(self, llm):
        answer = {'topic': 'аниме', 'kind': 'новость', 'subject': 'X',
                  'title': 'Аниме по манге «FX Воин Куруми» выйдет в октябре',
                  'summary': 'Премьера аниме по манге «FX Senshi Kurumi-chan» '
                             'состоится в октябре этого года.\n\n'
                             'Манга рассказывает о девушке-трейдере.',
                  'tags': ['#аниме']}
        news = {'title': 'T', 'summary': ('Premiere in October. ' + 'x' * 300), 'link': 'https://a/1', 'source': 'X'}
        with patch.object(llm.requests, 'post', return_value=_reply(answer)), \
             patch.object(llm, 'fetch_article', return_value={'text': '', 'video': None}):
            asyncio.run(llm._llm_enrich(news))
        text = news['_llm_text']
        assert 'состоится в октябре этого года' not in text
        assert 'девушке-трейдере' in text
