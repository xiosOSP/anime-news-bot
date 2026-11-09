"""Аудит 9 ноября 2026: названия на ромадзи, повторы «по истечении окна»,
ложные обновления, юбилейные картинки, текст постов.

За три дня в канал ушло семь заголовков на ромадзи, три повтора о тайтлах, о
которых канал уже писал, и юбилейная иллюстрация. Примеры ниже пересказаны.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import anime_news_bot as bot


BARRIER = {'id': '62311', 'name': 'Kekkaishi no Ichirinka', 'russian': 'Цветочный круг мастера барьеров',
           'english': None, 'japanese': None, 'synonyms': []}


@pytest.fixture
def shikimori(monkeypatch, tmp_path):
    """Подставной Shikimori: отвечает по очереди из списка и считает вызовы."""
    calls = []

    def serve(answers):
        answers = list(answers)

        def post(url, json=None, **kwargs):
            calls.append(json['variables']['s'])
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return MagicMock(status_code=200, json=lambda: {'data': {'animes': answer}})
        monkeypatch.setattr(bot.requests, 'post', post)
        resolver = bot.WorkTitleResolver(tmp_path / 'work_titles.json')
        resolver.MIN_INTERVAL = 0
        monkeypatch.setattr(bot, 'work_titles', resolver)
        monkeypatch.setitem(bot.FEATURE_FLAGS, 'work_identity', True)
        monkeypatch.setattr(bot, 'settings', None)
        return resolver
    serve.calls = calls
    return serve


def _teaser_news(topic='аниме'):
    return {'_llm_text': 'Опубликованы тизеры к аниме «Kekkaishi no Ichirinka».\n\nЗапуск в январе 2027.',
            '_llm_subject': 'Kekkaishi no Ichirinka', '_llm_topic': topic}


# ---------- Shikimori не ответил: пост ждёт, а не выходит с ромадзи ----------

def test_unanswered_title_is_flagged_and_the_flag_clears_on_answer(shikimori):
    import requests
    shikimori([requests.Timeout('slow'), [BARRIER]])
    news = _teaser_news()
    bot._annotate_llm_titles(news)
    assert news.get('_title_lookup_failed') is True
    assert '_work_titles_ru' not in news
    bot._annotate_llm_titles(news)               # следующий цикл: Shikimori ответил
    assert '_title_lookup_failed' not in news
    assert news['_work_titles_ru'] == {'Kekkaishi no Ichirinka': 'Цветочный круг мастера барьеров'}


def test_title_shikimori_does_not_know_is_not_a_failure(shikimori):
    shikimori([[]])                              # ответ есть: такого тайтла в базе нет
    news = _teaser_news()
    bot._annotate_llm_titles(news)
    assert '_title_lookup_failed' not in news


def test_topics_shikimori_is_not_asked_about_are_not_flagged(shikimori):
    import requests
    shikimori([requests.Timeout('slow')])
    news = _teaser_news(topic='кино')
    bot._annotate_llm_titles(news)
    assert '_title_lookup_failed' not in news and shikimori.calls == []


@pytest.fixture
def prepare_env(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', MagicMock(local_topic_filter=False, llm_enabled=True,
                                                   llm_rewrite=True, dedup_final_text=False,
                                                   local_noise_filter=True))
    monkeypatch.setattr(bot, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bot, '_title_deferral_store', None)
    for name in ('_improve_thumb', '_discover_article_video', '_ensure_youtube_cover',
                 '_optimize_news_media', '_image_duplicate', '_video_duplicate'):
        monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
    monkeypatch.setattr(bot, '_assign_format_variant', lambda news: None)
    monkeypatch.setattr(bot, 'stats', MagicMock(record_skipped=AsyncMock()))
    monkeypatch.setattr(bot, '_left_untranslated', lambda news: False)
    monkeypatch.setattr(bot, '_tg_copy_without_rewrite', lambda news: False)
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    monkeypatch.setattr(bot, '_work_identity_keys', lambda news, lookups=0: [])
    monkeypatch.setattr(bot, 'published_texts', None)
    monkeypatch.setattr(bot, 'work_memory', None)
    monkeypatch.setattr(bot, 'work_titles', bot.WorkTitleResolver(tmp_path / 'wt.json'))
    monkeypatch.setattr(bot, '_llm_enrich', AsyncMock(return_value='ok'))
    return bot


def _prepare(news, **kwargs):
    return asyncio.run(bot._prepare_news_for_send(news, 'TG: A', **kwargs))


def test_post_waits_for_shikimori_then_goes_out_as_is(prepare_env):
    news = dict(_teaser_news(), title='Teasers', link='https://x/1', source='TG: A',
                _title_lookup_failed=True)
    for _ in range(4):                           # четыре цикла сбора, не больше
        assert _prepare(news) == 'deferred'
    assert _prepare(news) is None                # ждать дальше незачем: новость стареет


def test_long_outage_does_not_hold_every_post(prepare_env):
    """Shikimori лежит полчаса и дольше: ждать его каждым постом — задержать
    все новости об аниме. Заминка в несколько минут — ждём."""
    news = dict(_teaser_news(), title='Teasers', source='TG: A', _title_lookup_failed=True)
    resolver = prepare_env.work_titles
    resolver._went_down('Timeout')
    resolver._went_down('Timeout')
    assert resolver.status()['failing_sec'] < 5          # отсчёт — с первого сбоя, а не с последнего
    resolver._fail_since = time.time() - bot.TITLE_DEFER_OUTAGE_SEC - 5
    resolver._went_down('Timeout')                        # новый сбой отсчёт не сбрасывает
    assert _prepare(dict(news, link='https://x/5')) is None
    resolver._fail_since = time.time() - 300
    assert _prepare(dict(news, link='https://x/6')) == 'deferred'
    resolver._came_up()
    assert resolver.status()['failing_sec'] == 0


def test_answered_post_and_admin_choice_do_not_wait(prepare_env):
    answered = dict(_teaser_news(), title='Teasers', link='https://x/2', source='TG: A')
    assert _prepare(answered) is None
    picked = dict(answered, link='https://x/3', _title_lookup_failed=True)
    assert _prepare(picked, apply_dedup=False) is None


@pytest.mark.parametrize('response, error', [
    (MagicMock(status_code=301), 'HTTP 301'),                 # сайт переехал
    (MagicMock(status_code=403), 'HTTP 403'),                 # хостингу отказано
    (MagicMock(status_code=200, json=MagicMock(side_effect=ValueError('html'))), 'ответ не JSON'),
    (MagicMock(status_code=200, json=lambda: {'errors': [{'message': 'x'}]}), 'в ответе нет списка'),
])
def test_any_non_answer_counts_as_a_failure(monkeypatch, tmp_path, response, error):
    """Без счёта таких ответов пост ждал бы названия при каждом сборе: отказ
    «надолго» виден только по счёту сбоев."""
    monkeypatch.setattr(bot.requests, 'post', lambda *a, **k: response)
    resolver = bot.WorkTitleResolver(tmp_path / 'wt.json')
    resolver.MIN_INTERVAL = 0
    assert resolver.lookup('Grand Blue') is None
    state = resolver.status()
    assert state['fail_streak'] == 1 and state['last_error'] == error
    assert resolver.cached('Grand Blue') is None             # в кеш неответ не пишется


def test_health_line_names_the_failure(shikimori):
    import requests
    resolver = shikimori([requests.ConnectionError('down')])
    assert 'ответов после запуска не было' in bot._work_titles_health_line()
    for _ in range(resolver.DOWN_AFTER):
        resolver.lookup('Grand Blue')
    line = bot._work_titles_health_line()
    assert f'сбоев подряд {resolver.DOWN_AFTER}' in line and 'ConnectionError' in line
    assert 'пауза ещё' in line


# ---------- повтор не становится новостью, пока ждёт в ветке ----------

@pytest.fixture
def memory(monkeypatch, tmp_path):
    store = bot.WorkMemory(tmp_path / 'works.json')
    monkeypatch.setattr(bot, 'work_memory', store)
    monkeypatch.setattr(bot, 'work_aliases', None)
    monkeypatch.setattr(bot, 'WORK_REPEAT_HOURS', 36)
    return store


def test_package_window_counts_from_arrival_in_the_thread(memory):
    """Первый пост — «постер и трейлер», второй — «премьера в апреле» — пришёл
    в ветку через 4 часа и ждал, пока суточное окно истечёт: вышел через
    24 ч 02 мин после первого."""
    memory.remember(['shiki:64877'], 'Постер и трейлер', 'bot', events={'trailer', 'visual'}, numbers=())
    memory._items[-1]['at'] = time.time() - 25 * 3600
    born = time.time() - 21 * 3600
    assert memory.seen(['shiki:64877'], 36, {'date'}, (), born=born)
    assert memory.seen(['shiki:64877'], 36, {'date'}, ()) is None      # пришёл только что — уже новость


def test_repeat_window_counts_from_arrival_too(memory):
    """«Постер» о том же тайтле вышел через 38 часов после первого: пост ждал
    в ветке, пока истекут 36 часов."""
    memory.remember(['shiki:64600'], 'Постер и трейлер', 'bot', events={'trailer', 'visual'}, numbers=())
    memory._items[-1]['at'] = time.time() - 38 * 3600
    assert memory.seen(['shiki:64600'], 36, {'visual'}, (), born=time.time() - 30 * 3600)
    assert memory.seen(['shiki:64600'], 36, {'visual'}, ()) is None
    assert memory.seen(['shiki:64600'], 36, {'visual'}, (), born='мусор') is None   # кривое поле — как «сейчас»


def test_autopost_checks_the_repeat_at_the_time_the_post_arrived(memory, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_western_quota_blocks', lambda news: False)
    monkeypatch.setattr(bot, '_tg_account_media_waits', lambda news: False)
    monkeypatch.setattr(bot, 'settings', MagicMock(post_max_age_hours=72))
    memory.remember(['shiki:64877'], 'Постер и трейлер', 'bot', events={'trailer', 'visual'}, numbers=())
    memory._items[-1]['at'] = time.time() - 25 * 3600
    pending = bot.PendingPosts(tmp_path / 'pending.json')
    key = pending.add({'title': 'Премьера аниме запланирована на апрель 2027 года', 'link': 'https://x/a',
                       '_llm_text': 'Премьера аниме запланирована на апрель 2027 года.',
                       '_work_keys': ['shiki:64877']})
    pending._items[key]['ts'] = time.time() - 21 * 3600
    reasons = {}
    assert pending.next_for_autopost(skip=bot._autopost_skip_reason, reasons=reasons) is None
    assert reasons == {'про тайтл уже был пост': 1}
    assert '_thread_ts' not in pending._items[key]['news']       # в сохранённый пост поле не пишется
    pending._items[key]['ts'] = time.time()                       # пришёл сейчас, через 25 ч — новость
    assert pending.next_for_autopost(skip=bot._autopost_skip_reason)[0] == key


# ---------- «в апреле» и «апрель 2027» — одна дата ----------

@pytest.mark.parametrize('new_date, old_date, old_text, known', [
    ('апрель 2027', '', 'Poster and trailer revealed. The anime premieres in April.', True),
    ('апрель 2027', '', 'Студия объявила премьеру в апреле.', True),
    ('апрель 2027', '', '4月より放送開始', True),
    ('апрель 2027', '', 'Poster and trailer revealed. The anime premieres in July.', False),
    ('апрель 2027', 'апрель 2026', 'Premieres in April 2026.', False),          # перенос на год
    ('12 августа 2026', '12 августа', 'Premieres August 12.', True),
    ('12 августа', '12 августа 2026', 'Premieres August 12, 2026.', True),
    ('12 августа 2026', '', 'Premieres in August.', False),                    # назван день — новость
    ('май 2027', '', 'The series may get a sequel.', False),                   # «may» — не месяц
    ('май 2027', '', 'The series premieres in May.', True),
    ('весна 2027', '', 'The anime is slated for spring.', True),
    ('весна 2027', '', 'The anime is slated for winter.', False),
    ('2027', '', 'Premieres in April.', False),
])
def test_date_already_reported(new_date, old_date, old_text, known):
    assert bot._date_already_reported(new_date, old_date, old_text) is known


def test_year_added_to_a_known_month_is_not_a_story_update(tmp_path, monkeypatch):
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'story_updates', True)
    store = bot.PublishedStoryStore(tmp_path / 'stories.json')
    store.record({'title': 'Boku no Suki na Hito anime reveals poster, trailer',
                  'summary': 'The anime by studio Project No.9 will premiere in April.',
                  'link': 'https://a/1', '_llm_subject': 'Boku no Suki na Hito ga Suki na Hito'})
    same = {'title': 'Boku no Suki na Hito anime reveals poster, trailer, April 2027 premiere',
            'summary': '', 'link': 'https://b/2', '_llm_subject': 'Boku no Suki na Hito ga Suki na Hito'}
    assert store.classify_update(same) is None
    moved = dict(same, title='Boku no Suki na Hito anime reveals poster, trailer, July 2027 premiere')
    assert store.classify_update(moved) is not None


# ---------- юбилейная картинка — не новость ----------

@pytest.mark.parametrize('title', [
    'Новая иллюстрация к 20-летию аниме «Код Гиас».',
    'К 10-летию аниме представили юбилейный постер',
    'К 25-летию «Наруто» студия показала новый арт',
    'Code Geass 20th Anniversary Illustration Revealed',
])
def test_anniversary_picture_is_noise(title):
    assert bot.noise_reason({'title': title}) == 'годовщина и ностальгия'


@pytest.mark.parametrize('title', [
    'К 10-летию аниме выйдет фильм',
    'Юбилейный постер к 20-летию и анонс фильма',
    '20th Anniversary Visual Revealed for New Project',
    'Постер выставки к 30-летию «Евангелиона»',
])
def test_anniversary_event_with_a_picture_is_still_news(title):
    assert bot.noise_reason({'title': title}) == ''


def test_anniversary_in_the_rewritten_headline_is_caught(prepare_env):
    """Источник пишет по-японски, и фильтр сбора юбилея не видит; в пересказе
    модели он уже по-русски."""
    news = {'title': '「コードギアス」20周年記念イラスト公開', 'link': 'https://x/9', 'source': 'TG: A',
            '_llm_text': 'Новая иллюстрация к 20-летию аниме «Код Гиас».'}
    assert _prepare(news) == 'skipped_filter'
    assert _prepare(dict(news, link='https://x/10'), apply_dedup=False) is None     # выбор админа
    prepare_env.settings.local_noise_filter = False
    assert _prepare(dict(news, link='https://x/11')) is None


# ---------- подстановка русского названия ----------

CAMP = 'Лагерь на свежем воздухе'


@pytest.mark.parametrize('text, pairs, expected', [
    # Два имени одного тайтла: второе находило себя в только что поставленных скобках.
    ('Вышел трейлер 4 сезона аниме Yuru Camp△.', {'Yuru Camp△': CAMP, 'Yuru Camp': CAMP},
     f'Вышел трейлер 4 сезона аниме «{CAMP}» (Yuru Camp△).'),
    # Значок после имени — часть названия.
    ('Вышел трейлер 4 сезона аниме Yuru Camp△.', {'Yuru Camp': CAMP},
     f'Вышел трейлер 4 сезона аниме «{CAMP}» (Yuru Camp△).'),
    # Название кончается точкой: вместе с ним пропадала точка заголовка.
    ('Анонсирован второй сезон аниме Kokuhaku shitemita.\n\nПодробностей нет.',
     {'Kokuhaku shitemita.': 'Я признался ей', 'Kokuhaku shitemita': 'Я признался ей'},
     'Анонсирован второй сезон аниме «Я признался ей» (Kokuhaku shitemita).\n\nПодробностей нет.'),
    # Посреди фразы лишняя точка не появляется.
    ('Аниме Kokuhaku shitemita. получит второй сезон.', {'Kokuhaku shitemita.': 'Я признался ей'},
     'Аниме «Я признался ей» (Kokuhaku shitemita) получит второй сезон.'),
    # Короткое имя внутри скобок с полным, которые модель поставила сама.
    ('Трейлер «Провожающая в последний путь Фрирен» (Sousou no Frieren) вышел. Frieren вернётся в январе.',
     {'Sousou no Frieren': 'Провожающая в последний путь Фрирен',
      'Frieren': 'Провожающая в последний путь Фрирен'},
     'Трейлер «Провожающая в последний путь Фрирен» (Sousou no Frieren) вышел. '
     '«Провожающая в последний путь Фрирен» вернётся в январе.'),
])
def test_russian_title_substitution(monkeypatch, text, pairs, expected):
    monkeypatch.setattr(bot, 'settings', None)
    monkeypatch.setattr(bot, 'work_titles', None)
    assert bot._russify_titles(text, {'_work_titles_ru': pairs}) == expected


def test_two_names_of_one_title_give_the_original_in_brackets_once(monkeypatch):
    monkeypatch.setattr(bot, 'settings', None)
    monkeypatch.setattr(bot, 'work_titles', None)
    out = bot._russify_titles('Sousou no Frieren продлён. Frieren вернётся в январе.',
                              {'_work_titles_ru': {'Sousou no Frieren': 'Фрирен', 'Frieren': 'Фрирен'}})
    assert out == '«Фрирен» (Sousou no Frieren) продлён. «Фрирен» вернётся в январе.'


# ---------- вторая строка, которая пересказывает заголовок ----------

def test_generic_noun_instead_of_the_title_is_an_echo():
    title = 'Анонсирован второй сезон аниме «В команде героя была милая девушка».'
    kept = bot._drop_repetitive_paragraphs(title, ['Анонсирован второй сезон аниме-сериала.',
                                                   'Сериал расскажет о герое и его команде.'])
    assert kept == ['Сериал расскажет о герое и его команде.']


def test_ordinal_in_words_and_another_verb_of_delay_is_an_echo():
    title = '7 эпизод «Я перевоплотился в гоблина» перенесён на 16 ноября.'
    assert bot._drop_repetitive_paragraphs(title, ['Премьера седьмого эпизода отложена до 16 ноября.']) == []
    # Другой номер словом — уже не пересказ.
    other = 'Премьера восьмого эпизода отложена до 16 ноября.'
    assert bot._drop_repetitive_paragraphs(title, [other]) == [other]
    # «Отложен» новое слово, если заголовок не о переносе.
    plain = '7 эпизод «Я перевоплотился в гоблина» выйдет 16 ноября.'
    late = 'Седьмой эпизод отложен: выйдет 16 ноября.'
    assert bot._drop_repetitive_paragraphs(plain, [late]) == [late]


@pytest.mark.parametrize('title, sentence', [
    # Причастный оборот после подлежащего: выходило «Выпустившая первый сезон, получила…».
    ('Студия MAPPA снимет продолжение «Ледяной стены».',
     'Студия MAPPA, выпустившая первый сезон, получила заказ на 12 серий.'),
    # Перечисление после подлежащего из одного слова: режиссёр пропадал из списка.
    ('Режиссёр покинул аниме «Ледяная стена».',
     'Режиссёр, сценарист и композитор покинули проект из-за разногласий со студией.'),
])
def test_subject_before_a_comma_is_not_cut_off(title, sentence):
    assert bot._drop_repetitive_paragraphs(title, [sentence]) == [sentence]


def test_echo_lead_clause_is_still_cut():
    title = 'Вышел новый трейлер аниме «Ледяная стена».'
    assert bot._drop_repetitive_paragraphs(
        title, ['Показан новый трейлер, сериал выйдет в январе 2027 года.']) == [
            'Сериал выйдет в январе 2027 года.']


# ---------- мелочи текста ----------

@pytest.mark.parametrize('paragraph, expected', [
    ('Продолжение сериала выйдет в апреле 2027 года', 'Продолжение сериала выйдет в апреле 2027 года.'),
    ('Сериал снимает студия «Мадхаус»', 'Сериал снимает студия «Мадхаус».'),
    ('Премьера в январе.', 'Премьера в январе.'),
    ('Что дальше?', 'Что дальше?'),
    ('#аниме #трейлер', '#аниме #трейлер'),
    ('Трейлер: https://youtu.be/abc', 'Трейлер: https://youtu.be/abc'),
    ('Премьера в январе 🎬', 'Премьера в январе 🎬'),
    ('', ''),
])
def test_paragraph_gets_its_final_period(paragraph, expected):
    assert bot._close_sentence(paragraph) == expected


def test_rewrite_without_the_final_period_gets_one(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 'settings.json'))
    bot.settings.llm_read_article = False
    monkeypatch.setattr(bot, '_llm_active', lambda: True)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, 'recent_subjects', None)
    monkeypatch.setattr(bot, 'entity_memory', None)
    monkeypatch.setattr(bot, 'work_titles', None)
    monkeypatch.setitem(bot.FEATURE_FLAGS, 'llm_judge', False)
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=json.dumps({
        'topic': 'аниме', 'kind': 'трейлер', 'title': 'Вышел трейлер четвёртого сезона «Лагеря»',
        'summary': 'Продолжение снимает студия Furyu Pictures', 'tags': []}, ensure_ascii=False)))
    news = {'title': 'Camp season 4 trailer revealed',
            'summary': 'The fourth season trailer is out. Furyu Pictures animates the sequel.'}
    assert asyncio.run(bot._llm_enrich(news, side_effects=False)) == 'ok'
    assert news['_llm_text'] == ('Вышел трейлер четвёртого сезона «Лагеря».\n\n'
                                 'Продолжение снимает студия Furyu Pictures.')


def test_ai_art_is_written_in_russian():
    assert bot._polish_post_text('Мангака осудил AI-арт с героем.') == 'Мангака осудил ИИ-арт с героем.'
    assert bot._polish_post_text('Студия AI-generated ролик не комментирует.') == \
        'Студия AI-generated ролик не комментирует.'


# ---------- пачка для модели переживает одну битую статью ----------

def test_one_broken_article_does_not_drop_the_batch(monkeypatch):
    async def source_text(news):
        if news['title'] == 'битая':
            raise RuntimeError('страница не разобрана')
        return 'текст статьи'
    seen = {}

    def payload(chunk, texts):
        seen['texts'] = texts
        return 'payload'
    monkeypatch.setattr(bot, '_llm_source_text', source_text)
    monkeypatch.setattr(bot, '_attach_work_about', AsyncMock())
    monkeypatch.setattr(bot, '_llm_batch_payload', payload)
    monkeypatch.setattr(bot, '_llm_call', AsyncMock(return_value=None))
    assert asyncio.run(bot._llm_enrich_chunk(
        [{'title': 'битая', 'summary': 'описание из ленты'}, {'title': 'целая'}])) == 0
    assert seen['texts'] == ['описание из ленты', 'текст статьи']
